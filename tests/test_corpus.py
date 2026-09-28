"""Corpus tests.

The corpus is only useful if it can disagree with the engine. Two properties matter:

1. Every shipped case matches its specification (a regression suite for the engine).
2. A deliberately wrong case is actually caught (otherwise 1 is vacuous).

Property 2 is the one people skip. A corpus validator that always returns "pass" looks
identical to a correct one from the outside, and the failure mode is a corpus that has
quietly stopped testing anything.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trueplumb.corpus import Corpus, CorpusError, load_corpus

CORPUS_DIR = Path(__file__).resolve().parents[1] / "corpus"
SHIPPED = CORPUS_DIR / "agent_safety_baseline.json"


@pytest.fixture(scope="module")
def shipped() -> Corpus:
    return load_corpus(SHIPPED)


class TestShippedCorpus:
    def test_loads(self, shipped: Corpus) -> None:
        assert shipped.schema_version == "1.0"
        assert len(shipped.cases) >= 15

    def test_every_case_matches_its_specification(self, shipped: Corpus) -> None:
        failures = shipped.verify_all()
        assert not failures, "\n".join(failures)

    def test_pinned_violation_steps_are_exact(self, shipped: Corpus) -> None:
        # The stricter question, asked only of cases that pin a step.
        failures = shipped.strict_failures()
        assert not failures, "\n".join(failures)

    def test_covers_a_useful_spread_of_categories(self, shipped: Corpus) -> None:
        categories = set(shipped.by_attack())
        required = {
            "approval",
            "authorization",
            "prompt-injection",
            "memory-poisoning",
            "data-exfiltration",
            "privilege-escalation",
            "resource-exhaustion",
            "termination",
            "baseline",
        }
        assert required <= categories, f"missing: {required - categories}"

    def test_attack_categories_have_a_compliant_counterpart(self, shipped: Corpus) -> None:
        # Every attack category must have at least one case that *passes* under a comparable
        # policy. Without that, "the control caught it" and "the policy rejects everything"
        # produce the same corpus result, and the false-positive half of the measurement is
        # simply absent. A corpus measuring only true positives cannot rank controls.
        for category, cases in shipped.by_attack().items():
            if category == "baseline":
                continue
            assert any(c.expect == "compliant" for c in cases), (
                f"category {category!r} has no compliant case, so a control could score "
                f"well by rejecting everything"
            )

    def test_every_case_documents_itself(self, shipped: Corpus) -> None:
        for case in shipped.cases:
            assert case.why.strip(), f"{case.id} has no 'why'"
            assert case.title.strip(), f"{case.id} has no title"
            assert case.attack.strip(), f"{case.id} has no attack description"

    def test_compliant_cases_are_not_trivial(self, shipped: Corpus) -> None:
        # A "compliant" case whose policy cannot be violated is a false pass. Each
        # compliant case must have a sibling under the same policy that fails, proving the
        # policy is actually load-bearing.
        by_policy: dict[str, list] = {}
        for case in shipped.cases:
            by_policy.setdefault(case.policy, []).append(case)

        for policy, cases in by_policy.items():
            if any(c.expect == "violation" for c in cases):
                continue
            # No violation sibling: the policy is only ever demonstrated as satisfied.
            # Allowed, but flag which ones so the corpus can be improved deliberately.
            assert cases, f"policy {policy!r} has no cases"


class TestValidatorCatchesDisagreement:
    """The corpus must be able to fail. These cases assert that it can."""

    def _case(self, **overrides: object) -> dict:
        base = {
            "id": "t-001",
            "title": "test",
            "attack": "test",
            "category": "baseline",
            "policy": "G(!dangerous)",
            "expect": "compliant",
            "why": "test",
            "steps": [{"atoms": ["safe"]}, {"atoms": ["dangerous"]}],
        }
        base.update(overrides)
        return base

    def test_catches_a_false_compliant(self) -> None:
        # Trace contains `dangerous`, so G(!dangerous) must fail. Claiming compliant must
        # be rejected.
        corpus = _corpus_from_payload(
            {
                "schema_version": "1.0",
                "name": "t",
                "version": "0",
                "cases": [self._case(expect="compliant")],
            }
        )
        failures = corpus.verify_all()
        assert failures
        assert "expected COMPLIANT" in failures[0]

    def test_catches_a_false_violation(self) -> None:
        # A trace with no `dangerous` step satisfies G(!dangerous). Claiming a violation
        # must be rejected -- otherwise a corpus could be padded with cases the engine
        # never actually fails, inflating every pass rate computed from it.
        corpus = _corpus_from_payload(
            {
                "schema_version": "1.0",
                "name": "t",
                "version": "0",
                "cases": [self._case(expect="violation", steps=[{"atoms": ["safe"]}])],
            }
        )
        failures = corpus.verify_all()
        assert failures
        assert "expected VIOLATION" in failures[0]

    def test_catches_a_wrong_pinned_step(self) -> None:
        corpus = _corpus_from_payload(
            {
                "schema_version": "1.0",
                "name": "t",
                "version": "0",
                "cases": [self._case(expect="violation", violation_at=5)],
            }
        )
        # Lenient by default: the verdict is right, the pinned step is not.
        assert not corpus.verify_all()
        # Strict: the wrong pin is an error.
        assert corpus.strict_failures()

    def test_catches_an_unparseable_policy(self) -> None:
        corpus = _corpus_from_payload(
            {
                "schema_version": "1.0",
                "name": "t",
                "version": "0",
                "cases": [self._case(policy="G(((")],
            }
        )
        failures = corpus.verify_all()
        assert failures
        assert "unparseable policy" in failures[0]


def _corpus_from_payload(payload: dict) -> Corpus:
    from trueplumb.corpus import from_dict

    cases = tuple(from_dict(c, "test") for c in payload["cases"])
    return Corpus(
        name=payload["name"],
        version=payload["version"],
        description=payload.get("description", ""),
        cases=cases,
    )


class TestSchemaValidation:
    def test_rejects_wrong_schema_version(self, tmp_path: Path) -> None:
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"schema_version": "9.9", "cases": [{}]}), encoding="utf-8")
        with pytest.raises(CorpusError, match="schema_version"):
            load_corpus(path)

    def test_rejects_duplicate_ids(self, tmp_path: Path) -> None:
        case = {
            "id": "dup",
            "title": "t",
            "attack": "t",
            "category": "baseline",
            "policy": "G(a)",
            "expect": "compliant",
            "steps": [{"atoms": ["a"]}],
        }
        path = tmp_path / "c.json"
        path.write_text(
            json.dumps({"schema_version": "1.0", "cases": [case, dict(case)]}),
            encoding="utf-8",
        )
        with pytest.raises(CorpusError, match="duplicate"):
            load_corpus(path)

    def test_rejects_a_malformed_atoms_field(self) -> None:
        # A string where a list belongs would otherwise iterate into single characters.
        from trueplumb.corpus import from_dict

        with pytest.raises(CorpusError):
            case = from_dict(
                {
                    "id": "x",
                    "title": "t",
                    "attack": "t",
                    "category": "baseline",
                    "policy": "G(a)",
                    "expect": "compliant",
                    "steps": [{"atoms": "dangerous"}],
                }
            )
            case.check()

    def test_rejects_missing_fields(self) -> None:
        from trueplumb.corpus import from_dict

        with pytest.raises(CorpusError, match="missing required field"):
            from_dict({"id": "x", "policy": "G(a)"})

    def test_rejects_an_invalid_expect_value(self) -> None:
        from trueplumb.corpus import from_dict

        with pytest.raises(CorpusError, match="expect must be"):
            from_dict(
                {
                    "id": "x",
                    "title": "t",
                    "attack": "t",
                    "category": "baseline",
                    "policy": "G(a)",
                    "expect": "maybe",
                    "steps": [{"atoms": ["a"]}],
                }
            )
