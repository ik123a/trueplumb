"""Statistics tests.

The numbers here are checked against published reference values rather than against
themselves. A statistics module whose tests only assert that its own output is consistent
proves nothing -- an implementation that is confidently wrong passes every such test.

Reference values come from the standard binomial tables used in Newcombe (1998) and from
the exact McNemar distribution. Each case names where the expected number comes from.
"""

from __future__ import annotations

import pytest

from trueplumb.stats import (
    StatsError,
    mcnemar_exact,
    rate,
    wilson_interval,
)


class TestWilsonInterval:
    @pytest.mark.parametrize(
        ("successes", "trials", "low", "high"),
        [
            # Standard reference values for 95% Wilson intervals.
            (10, 10, 0.7225, 1.0),
            (0, 10, 0.0, 0.2775),
            (1, 10, 0.0179, 0.4042),
            (5, 10, 0.2366, 0.7634),
            (9, 10, 0.5958, 0.9821),
            (20, 20, 0.8389, 1.0),
        ],
    )
    def test_matches_published_values(
        self, successes: int, trials: int, low: float, high: float
    ) -> None:
        interval = wilson_interval(successes, trials, 0.95)
        assert interval.low == pytest.approx(low, abs=5e-5)
        assert interval.high == pytest.approx(high, abs=5e-5)

    def test_point_estimate_is_the_raw_proportion(self) -> None:
        # The point estimate must not be shrunk toward the centre. Wilson shifts the
        # *interval*, not the estimate; a report quoting a shrunken point estimate would be
        # quietly pessimistic about a good control.
        assert wilson_interval(9, 10).point == pytest.approx(0.9)
        assert wilson_interval(3, 4).point == pytest.approx(0.75)

    def test_stays_inside_the_unit_interval(self) -> None:
        # This is the reason for preferring Wilson over Wald. A Wald interval on 20/20
        # produces a lower bound above 1 or a negative upper bound at 0/20, which is the
        # exact regime conformance results live in.
        for successes, trials in [(0, 5), (5, 5), (20, 20), (1, 3), (2, 3)]:
            interval = wilson_interval(successes, trials)
            assert 0.0 <= interval.low <= interval.high <= 1.0

    def test_wider_at_smaller_n(self) -> None:
        # Same rate, less data, wider interval. If this fails, the n-dependence is broken.
        assert wilson_interval(5, 10).high - wilson_interval(5, 10).low > (
            wilson_interval(50, 100).high - wilson_interval(50, 100).low
        )

    def test_wider_at_higher_confidence(self) -> None:
        assert wilson_interval(7, 10, 0.99).high > wilson_interval(7, 10, 0.90).high
        assert wilson_interval(7, 10, 0.99).low < wilson_interval(7, 10, 0.90).low

    def test_interval_brackets_the_point_estimate(self) -> None:
        for successes in range(0, 11):
            interval = wilson_interval(successes, 10)
            assert interval.contains(interval.point)

    def test_degenerate_cases(self) -> None:
        zero = wilson_interval(0, 0)
        assert zero.trials == 0
        assert (zero.low, zero.high) == (0.0, 1.0)
        assert "n/a" in zero.render()

    def test_rejects_impossible_counts(self) -> None:
        with pytest.raises(StatsError, match="impossible"):
            wilson_interval(5, 3)
        with pytest.raises(StatsError, match="impossible"):
            wilson_interval(-1, 3)

    def test_rejects_untabulated_confidence(self) -> None:
        with pytest.raises(StatsError, match="tabulated"):
            wilson_interval(1, 2, confidence=0.975)

    def test_render_flags_a_degenerate_interval(self) -> None:
        # Printing "100.0% [100.0%, 100.0%]" for 0 successes would read as certainty.
        # The render says so explicitly.
        text = wilson_interval(0, 10).render()
        assert "degenerate" in text
        assert "0/0" in text

    def test_is_deterministic(self) -> None:
        first = wilson_interval(9, 13, 0.95)
        second = wilson_interval(9, 13, 0.95)
        assert first.as_dict() == second.as_dict()


class TestMcNemar:
    @pytest.mark.parametrize(
        ("a_only", "b_only", "expected_p"),
        [
            # Exact two-sided binomial values.
            (0, 0, 1.0),
            (1, 1, 1.0),
            (5, 5, 1.0),
            (10, 0, 0.001953125),
            (0, 10, 0.001953125),
            (10, 2, 0.03857421875),
            (3, 0, 0.25),
        ],
    )
    def test_matches_exact_binomial(self, a_only: int, b_only: int, expected_p: float) -> None:
        result = mcnemar_exact(a_only, b_only, a_only + b_only)
        assert result.p_value == pytest.approx(expected_p, abs=1e-9)

    def test_symmetric_in_its_arguments(self) -> None:
        # Swapping which control won must not change the p-value; only the direction.
        assert mcnemar_exact(10, 2).p_value == pytest.approx(mcnemar_exact(2, 10).p_value)
        assert mcnemar_exact(10, 2).b != mcnemar_exact(2, 10).b

    def test_no_discordant_pairs_is_not_significant(self) -> None:
        # The controls agreed everywhere. p=1 and "identical" is the honest reading;
        # claiming significance here would be a coin flip reported as a result.
        result = mcnemar_exact(0, 0, n_total=20)
        assert result.p_value == 1.0
        assert not result.significant
        assert "identical" in result.verdict()

    def test_non_significant_is_not_reported_as_equivalence(self) -> None:
        result = mcnemar_exact(2, 1, n_total=20)
        assert not result.significant
        assert "no detectable difference" in result.verdict()
        assert "identical" not in result.verdict()

    def test_significant_result_names_the_winner(self) -> None:
        result = mcnemar_exact(10, 0, n_total=20)
        assert result.significant
        assert "A better" in result.verdict()

    def test_alpha_controls_significance(self) -> None:
        strict = mcnemar_exact(10, 2, alpha=0.01)
        lenient = mcnemar_exact(10, 2, alpha=0.10)
        assert lenient.significant
        assert not strict.significant

    def test_rejects_impossible_discordant_counts(self) -> None:
        with pytest.raises(StatsError, match="non-negative"):
            mcnemar_exact(-1, 2)
        with pytest.raises(StatsError, match="exceed"):
            mcnemar_exact(5, 5, n_total=3)


class TestRate:
    def test_zero_trials_returns_zero_without_raising(self) -> None:
        # A category with no cases is a fact about the corpus, not an error. The report
        # prints `n/a` alongside so this is never mistaken for a measured 0%.
        assert rate(0, 0) == 0.0

    def test_normal(self) -> None:
        assert rate(1, 4) == pytest.approx(0.25)


class TestNoNondeterminism:
    def test_identical_inputs_give_identical_output(self) -> None:
        # The design rule: no RNG, no sampling. A score that varies between runs cannot be
        # compared across controls, which is the one thing this module exists to do.
        a = wilson_interval(9, 13, 0.95).as_dict()
        b = wilson_interval(9, 13, 0.95).as_dict()
        assert a == b

        c = mcnemar_exact(7, 2, 13).as_dict()
        d = mcnemar_exact(7, 2, 13).as_dict()
        assert c == d


def test_interval_dataclass_is_frozen() -> None:
    # The scores end up in signed reports, so an accidental mutation after the fact would
    # silently change a published number. Assert the specific exception, not `Exception`.
    interval = wilson_interval(1, 2)
    with pytest.raises(AttributeError):
        interval.point = 0.5  # type: ignore[misc]
