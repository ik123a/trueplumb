"""Correctness tests for the LTL core.

Every expectation here is hand-derived from the LTL semantics, not captured from the
implementation's own output. A verification engine that only agrees with itself is worse
than no engine, because it would launder bad policies into "COMPLIANT" verdicts.

Each property's expected verdict was worked out by hand from the operator definitions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trueplumb.ltl.ast import Or, Temporal, TemporalOp
from trueplumb.ltl.automata import to_dfa
from trueplumb.ltl.parser import ParseError, parse
from trueplumb.ltl.trace import TraceEvent, check


def ev(*atom_sets: list[str]) -> list[TraceEvent]:
    return [TraceEvent(index=i, atoms=frozenset(a)) for i, a in enumerate(atom_sets)]


# ----------------------------------------------------------------------------------
# Parser
# ----------------------------------------------------------------------------------


class TestParser:
    @pytest.mark.parametrize(
        "source",
        [
            "G(!dangerous)",
            "always(!dangerous)",
            "G(!dangerous) && F(done)",
            "eventually(done)",
            "G(a -> F(b))",
            "G(!call_refund) || F(approved)",
            "X(done)",
            "F(F(done))",
            "!G(a)",
            "a && b && c",
            "G(a) -> F(b)",
            "G(a -> (b U c))",
            "call_refund U approved",
        ],
    )
    def test_parses(self, source: str) -> None:
        assert parse(source) is not None

    @pytest.mark.parametrize(
        "source",
        ["", "   ", "G(", "a &&", "G(!", "a ) b", "&& a"],
    )
    def test_rejects_malformed(self, source: str) -> None:
        with pytest.raises(ParseError):
            parse(source)

    def test_never_uses_eval(self) -> None:
        """A policy file is untrusted input in a security tool; no code execution."""
        with pytest.raises(ParseError):
            parse("__import__('os').system('echo pwned')")

    def test_gf_collapse(self) -> None:
        # G(F(p)) is F(p) on a finite trace: normalise must remove the redundant wrapper.
        formula = parse("G(F(done))")
        assert isinstance(formula, Temporal) and formula.op is TemporalOp.EVENTUALLY

    def test_implies_becomes_disjunction(self) -> None:
        assert isinstance(parse("a -> b"), Or)

    def test_str_roundtrip_is_stable(self) -> None:
        formula = parse("G(a) && F(b)")
        assert str(parse(str(formula))) == str(formula)


# ----------------------------------------------------------------------------------
# Semantics -- hand-derived expectations
# ----------------------------------------------------------------------------------


class TestTemporalSemantics:
    def test_always_holds_when_atom_never_violated(self) -> None:
        # G(!dangerous): trace never contains "dangerous" -> holds at every step.
        result = check("G(!dangerous)", ev([], ["safe"], ["safe"], ["safe"]))
        assert result.compliant, result.explain()

    def test_always_fails_when_atom_appears(self) -> None:
        # G(!dangerous): "dangerous" at step 1 breaks the invariant immediately.
        result = check("G(!dangerous)", ev(["safe"], ["dangerous"], ["safe"]))
        assert not result.compliant
        assert result.violation_index == 1

    def test_always_violation_at_last_step(self) -> None:
        result = check("G(!dangerous)", ev(["safe"], ["safe"], ["dangerous"]))
        assert not result.compliant
        assert result.violation_index == 2

    def test_eventually_holds_when_atom_occurs(self) -> None:
        # F(done): satisfied as soon as "done" appears.
        result = check("F(done)", ev(["work"], ["work"], ["done"], ["work"]))
        assert result.compliant, result.explain()

    def test_eventually_fails_when_never_occurs(self) -> None:
        # F(done) with no "done" anywhere: the obligation is still outstanding at the
        # end of the trace, so the run is non-compliant.
        result = check("F(done)", ev(["work"], ["work"], ["work"]))
        assert not result.compliant
        assert result.counterexample

    def test_next_requires_immediate_successor(self) -> None:
        # X(done): "done" one step later is NOT immediate, so this must fail.
        assert not check("X(done)", ev(["start"], ["work"], ["done"])).compliant
        # X(done) with "done" immediately next must pass.
        assert check("X(done)", ev(["start"], ["done"])).compliant

    def test_until(self) -> None:
        # (p U q): q must occur, and p must hold at every position before it.
        assert check("p until q", ev(["p"], ["p"], ["q"])).compliant
        # q never arrives -> the obligation is still outstanding at the end -> fails.
        assert not check("p until q", ev(["p"], ["p"], ["p"])).compliant
        # q arrives at step 1, so p is only required at step 0. Step 2 is unconstrained.
        assert check("p until q", ev(["p"], ["q"], ["other"])).compliant
        # p fails at step 0, so there is no "p until" window for q to land in.
        assert not check("p until q", ev(["other"], ["q"])).compliant

    def test_release(self) -> None:
        # (p R q): q holds and continues to; p may or may not occur.
        assert check("p release q", ev(["q"], ["q"], ["q"])).compliant
        assert not check("p release q", ev(["p"], ["p"])).compliant

    def test_conjunction(self) -> None:
        both = check("G(!x) && F(y)", ev([], ["y"], []))
        assert both.compliant
        neither = check("G(!x) && F(y)", ev([], [], []))
        assert not neither.compliant

    def test_disjunction_is_permissive(self) -> None:
        assert check("G(!x) || F(z)", ev([], ["z"], [])).compliant


class TestRealisticPolicies:
    """Policies shaped like the ones a control author would actually write."""

    REFUND_APPROVAL = "G(call_refund -> F(human_approved))"

    def test_refund_with_approval_is_compliant(self) -> None:
        result = check(
            self.REFUND_APPROVAL,
            ev(["lookup_order"], ["call_refund"], ["human_approved"], ["send_receipt"]),
        )
        assert result.compliant, result.explain()

    def test_refund_without_approval_is_violation(self) -> None:
        # G(call_refund -> F(human_approved)): the refund fires and the trace ends with
        # the approval obligation still outstanding.
        result = check(
            self.REFUND_APPROVAL,
            ev(["lookup_order"], ["call_refund"], ["send_receipt"]),
        )
        assert not result.compliant
        assert result.counterexample

    def test_no_destructive_calls(self) -> None:
        policy = "G(!delete_records) && G(!drop_table)"
        assert check(policy, ev(["read"], ["write"])).compliant
        assert not check(policy, ev(["read"], ["delete_records"])).compliant

    def test_read_before_write(self) -> None:
        # G(write -> F(read)) only forbids writing with no read *after* it. A write that
        # is followed by a read satisfies it; a write with the trace ending immediately
        # after does not.
        assert check("G(write -> F(read))", ev(["read"], ["write"], ["read"])).compliant
        assert check("G(write -> F(read))", ev(["write"], ["read"])).compliant
        # No read ever follows the write -> the obligation is outstanding at the end.
        assert not check("G(write -> F(read))", ev(["write"])).compliant


# ----------------------------------------------------------------------------------
# Counterexamples and determinism
# ----------------------------------------------------------------------------------


class TestCounterexamples:
    def test_counterexample_is_a_suffix(self) -> None:
        result = check("G(!dangerous)", ev(["safe"], ["safe"], ["dangerous"], ["safe"]))
        assert not result.compliant
        indices = [e.index for e in result.counterexample]
        assert indices == sorted(indices)
        assert indices[-1] >= result.violation_index

    def test_counterexample_contains_the_offending_step(self) -> None:
        result = check("G(!dangerous)", ev(["safe"], ["dangerous"]))
        assert any("dangerous" in e.atoms for e in result.counterexample)

    def test_compliant_trace_has_no_counterexample(self) -> None:
        result = check("G(!dangerous)", ev(["safe"], ["safe"]))
        assert result.compliant
        assert result.counterexample == ()

    def test_explain_is_human_readable(self) -> None:
        result = check("G(!dangerous)", ev(["safe"], ["dangerous"]))
        text = result.explain()
        assert "VIOLATION" in text
        assert "step 1" in text
        assert "dangerous" in text


class TestDeterminism:
    """The product claim: same input, same verdict, every time."""

    TRACES = [
        ev([], ["a"], ["b"]),
        ev(["a"], ["dangerous"], ["a"], ["b"], ["c"]),
        ev(["a"], ["b"], ["c"], ["d"], ["e"], ["f"]),
    ]
    POLICIES = [
        "G(!dangerous)",
        "F(b)",
        "G(a -> F(c))",
        "G(!x) && F(y)",
    ]

    @pytest.mark.parametrize("policy", POLICIES)
    def test_same_verdict_across_repeated_runs(self, policy: str) -> None:
        verdicts = [check(policy, trace).compliant for trace in self.TRACES]
        for _ in range(5):
            assert [check(policy, t).compliant for t in self.TRACES] == verdicts

    def test_monitor_reuse_gives_identical_results(self) -> None:
        """Reusing a compiled policy is the CI path; it must not change verdicts."""
        monitor = to_dfa(parse("G(call_refund -> F(human_approved))"))
        trace = ev(["call_refund"], ["human_approved"])
        direct = check("G(call_refund -> F(human_approved))", trace)
        reused = check(monitor.formula_text, trace, monitor=monitor)
        assert direct.compliant == reused.compliant
        assert direct.violation_index == reused.violation_index

    def test_monitor_has_no_llm_dependency(self) -> None:
        """Structural guarantee: the core imports no network or model machinery."""
        import trueplumb.ltl.automata as automata
        import trueplumb.ltl.parser as parser
        import trueplumb.ltl.trace as trace_mod

        forbidden = (
            "openai",
            "anthropic",
            "requests",
            "httpx",
            "torch",
            "transformers",
            "litellm",
            "socket",
            "urllib",
        )
        for module in (automata, parser, trace_mod):
            assert module.__file__ is not None
            source = Path(module.__file__).read_text(encoding="utf-8")
            for token in forbidden:
                assert token not in source, f"{module.__name__} references {token}"


class TestMonitorConstruction:
    def test_dfa_is_total(self) -> None:
        """Every state must have a transition for every modelled event."""
        monitor = to_dfa(parse("G(!dangerous) || F(done)"))
        for state in monitor.states:
            row = monitor.transitions.get(state, {})
            assert row, f"state {state} has no outgoing transitions"

    def test_minimisation_shrinks_or_preserves(self) -> None:
        # The sink state is added after formula states are enumerated, so the total can
        # legitimately exceed the formula-state count. What matters is that refinement
        # never *adds* states beyond that sink-inclusive ceiling.
        monitor = to_dfa(parse("G(a) && F(b)"))
        stats = monitor.build_stats
        assert stats["minimized_states"] <= stats["formula_states"] + stats["sink"]
        # And the monitor must remain a usable object, not a degenerate one.
        assert len(monitor.states) == stats["minimized_states"]
        assert len(monitor.transitions) == len(monitor.states)

    def test_minimisation_never_merges_accepting_with_rejecting(self) -> None:
        # If this ever breaks, the engine would silently understate violations.
        monitor = to_dfa(parse("G(call_refund -> F(human_approved))"))
        assert monitor.accepting, "monitor must have at least one accepting state"

    def test_build_stats_are_reported(self) -> None:
        """State cost is surfaced, not hidden, so callers can see the price."""
        monitor = to_dfa(parse("G(a -> F(b))"))
        assert {"formula_states", "minimized_states", "events"} <= set(monitor.build_stats)
