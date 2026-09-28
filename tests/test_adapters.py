"""Adapter and scoring tests.

The central claim under test is that a control cannot look good by blocking indiscriminately.
That is asserted directly, against a control that really does block everything, because the
failure mode is subtle: such a control achieves 100% detection, which looks like a perfect
score in any report that omits the false-positive column.
"""

from __future__ import annotations

import pytest

from trueplumb.adapters import (
    ControlAdapter,
    ControlRun,
    Decision,
    StepOutcome,
    compare_controls,
    score_control,
)
from trueplumb.baseline import AllowlistedTools, BlockEverything, NullAdapter
from trueplumb.corpus import Case, load_corpus
from trueplumb.stats import Interval

CORPUS = load_corpus("corpus/agent_safety_baseline.json")
CASES = list(CORPUS.cases)


def make_case(id: str, expect: str, steps: int = 2) -> Case:
    return Case(
        id=id,
        title="t",
        attack="a",
        category="baseline",
        policy="G(a)",
        steps=tuple({"atoms": ["a"]} for _ in range(steps)),
        expect=expect,  # type: ignore[arg-type]
    )


class TestDecision:
    def test_not_reached_is_not_a_permission(self) -> None:
        # The distinction that keeps a partial run from being read as evidence of
        # permission. A control that only saw the first three steps of a trace has not
        # "allowed" the fourth; it has never been exposed to it.
        run = ControlRun(
            name="x",
            version="1",
            outcomes={"c1": [StepOutcome("c1", 0, Decision.ALLOWED)]},
        )
        assert run.decision_for("c1", 0) is Decision.ALLOWED
        assert run.decision_for("c1", 5) is Decision.NOT_REACHED
        assert run.decision_for("unknown", 0) is Decision.NOT_REACHED

    def test_only_blocked_counts_as_a_denial(self) -> None:
        assert Decision.BLOCKED.is_denial
        assert not Decision.ALLOWED.is_denial
        assert not Decision.NOT_REACHED.is_denial

    def test_caught_case_rejects_a_compliant_case(self) -> None:
        run = ControlRun(name="x", version="1", outcomes={})
        with pytest.raises(ValueError, match="false_positive"):
            run.caught_case(make_case("c", "compliant"))

    def test_false_positive_rejects_a_violation_case(self) -> None:
        run = ControlRun(name="x", version="1", outcomes={})
        with pytest.raises(ValueError, match="caught_case"):
            run.false_positive(make_case("c", "violation"))


class TestBlockEverythingCannotLookGood:
    """The core product claim, asserted against a control that really does block all."""

    def test_it_achieves_perfect_detection(self) -> None:
        score = score_control(BlockEverything(), CASES)
        assert score.detection.point == 1.0
        assert score.detection.successes == score.detection.trials

    def test_its_false_positive_rate_is_equally_perfect(self) -> None:
        # This is the point. Without this number, "100% detection" reads as a triumph.
        score = score_control(BlockEverything(), CASES)
        assert score.false_positive.point == 1.0
        assert score.false_positive.successes == score.false_positive.trials

    def test_its_false_positive_interval_excludes_zero(self) -> None:
        # A report that shows detection alone would let this pass as a safe control. The
        # false-positive lower bound is the number that disqualifies it.
        score = score_control(BlockEverything(), CASES)
        assert not score.false_positive.contains(0.0)

    def test_no_single_accuracy_figure_is_produced(self) -> None:
        # Guard against someone later "simplifying" two rates into one. The report shape
        # itself is the defence.
        payload = score_control(BlockEverything(), CASES).as_dict()
        assert "detection" in payload
        assert "false_positive" in payload
        assert "accuracy" not in payload
        assert "score" not in payload


class TestNullAdapter:
    def test_detects_nothing(self) -> None:
        score = score_control(NullAdapter(), CASES)
        assert score.detection.point == 0.0
        assert score.detection.successes == 0

    def test_raises_no_false_positives(self) -> None:
        score = score_control(NullAdapter(), CASES)
        assert score.false_positive.point == 0.0

    def test_is_the_floor_every_control_must_beat(self) -> None:
        # A control that cannot beat doing nothing has demonstrated nothing. Stated as a
        # test so the requirement cannot be quietly dropped.
        allowlist = score_control(AllowlistedTools(), CASES)
        null = score_control(NullAdapter(), CASES)
        assert allowlist.detection.point > null.detection.point


class TestAllowlistControl:
    def test_detects_the_obvious_keyword_violations(self) -> None:
        score = score_control(AllowlistedTools(), CASES)
        assert score.detection.point > 0.5

    def test_misses_the_sequence_dependent_violations(self) -> None:
        # The interesting result. This control matches on single atoms and has no memory,
        # so it cannot catch a lapsed approval, a latched permission, or an abandoned
        # obligation -- all of which violate a policy without containing a banned token.
        control = AllowlistedTools()
        run = ControlRun(
            name=control.name,
            version=control.version,
            outcomes={c.id: control.evaluate(c) for c in CASES},
        )
        missed = [
            case.id for case in CASES if case.expect == "violation" and not run.caught_case(case)
        ]
        assert missed, "expected at least one sequence-dependent violation to be missed"
        # These four carry no banned atom anywhere in the trace. Every one is a violation of
        # a *temporal* obligation rather than of a keyword rule: an action taken with no
        # approval, an approval that had already expired, and runs that stalled before
        # discharging their obligation. A control that matches tokens cannot see any of them.
        assert set(missed) == {
            "refund-002-unapproved",
            "refund-003-prior-approval-insufficient",
            "refund-004-stalled-before-approval",
            "terminated-002-stalled",
        }

    def test_raises_false_positives_on_legitimate_traffic(self) -> None:
        # A keyword control blocks any step mentioning a dangerous atom, even when the
        # surrounding policy is satisfied. That is the real-world cost, and the corpus
        # makes it visible instead of leaving it to a vendor's marketing.
        score = score_control(AllowlistedTools(), CASES)
        assert score.false_positive.point > 0.0


class TestScoringShape:
    def test_counts_split_by_expectation(self) -> None:
        score = score_control(NullAdapter(), CASES)
        assert score.n_violation_cases + score.n_compliant_cases == len(CASES)
        assert score.detection.trials == score.n_violation_cases
        assert score.false_positive.trials == score.n_compliant_cases

    def test_scores_are_reproducible(self) -> None:
        # The design rule again: same corpus, same control, same numbers. A score that
        # drifts cannot be compared against another control's score.
        first = score_control(AllowlistedTools(), CASES).as_dict()
        second = score_control(AllowlistedTools(), CASES).as_dict()
        assert first == second

    def test_render_includes_both_rates(self) -> None:
        text = score_control(AllowlistedTools(), CASES).render()
        assert "detection" in text
        assert "false positives" in text
        assert "allowlist" in text

    def test_empty_corpus_yields_n_a_rather_than_zero_percent(self) -> None:
        # Reporting 0% for a corpus with no cases would read as "detected nothing", which
        # is a very different claim from "measured nothing".
        score = score_control(NullAdapter(), [])
        assert score.detection.trials == 0
        assert "n/a" in score.detection.render()


class TestCompareControls:
    def test_identical_controls_show_no_discordance(self) -> None:
        result = compare_controls(NullAdapter(), NullAdapter(), CASES)
        assert result.n_discordant == 0
        assert not result.significant
        assert "identical" in result.verdict()

    def test_a_strictly_better_control_is_detected(self) -> None:
        result = compare_controls(AllowlistedTools(), NullAdapter(), CASES)
        assert result.significant
        assert result.b > result.c

    def test_comparison_ignores_compliant_cases(self) -> None:
        # Detection is what the paired test measures. A control that blocks more
        # legitimate traffic is not "better", and mixing that in would answer a question
        # nobody asked.
        result = compare_controls(BlockEverything(), NullAdapter(), CASES)
        assert result.n_total == result.n_discordant
        assert result.n_total == sum(1 for c in CASES if c.expect == "violation")

    def test_result_is_reproducible(self) -> None:
        first = compare_controls(AllowlistedTools(), NullAdapter(), CASES).as_dict()
        second = compare_controls(AllowlistedTools(), NullAdapter(), CASES).as_dict()
        assert first == second


class TestAdapterContract:
    def test_a_custom_adapter_plugs_in(self) -> None:
        # The interface has to be implementable by a third party without reading our
        # internals, so this stands in for a real guardrail wrapper.
        class OnlyLeaks(ControlAdapter):
            name = "only-leaks"
            version = "9.9"

            def evaluate(self, case: Case) -> list[StepOutcome]:
                return [
                    StepOutcome(
                        case_id=case.id,
                        step=i,
                        decision=(
                            Decision.BLOCKED if "export_pii" in step["atoms"] else Decision.ALLOWED
                        ),
                    )
                    for i, step in enumerate(case.steps)
                ]

        score = score_control(OnlyLeaks(), CASES)
        assert isinstance(score, type(score))
        assert score.name == "only-leaks"
        assert score.version == "9.9"
        assert score.detection.trials > 0

    def test_version_is_part_of_the_claim(self) -> None:
        # A conformance claim without a version is not reproducible: the thing being
        # measured can change underneath the report.
        assert AllowlistedTools().describe() == "allowlist 1.0"
        assert NullAdapter().describe() == "none 1.0"
        assert BlockEverything().describe() == "block-everything 1.0"

    def test_describe_always_carries_a_version(self) -> None:
        for adapter in (NullAdapter(), BlockEverything(), AllowlistedTools()):
            assert adapter.version
            assert adapter.describe().endswith(adapter.version)


def test_interval_type_is_exported() -> None:
    # Guards the public surface used by reports.
    assert Interval(0.5, 0.1, 0.9, 5, 10, 0.95).point == 0.5
