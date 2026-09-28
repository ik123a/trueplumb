"""End-of-trace semantics: the rules that are easy to get wrong, and why.

Every case here was a bug at some point during development. They are pinned because the
end-of-trace boundary is where a finite-trace LTL monitor silently produces confident
wrong answers -- the formula is still "true" or "false", the monitor still terminates, and
nothing crashes. A differential test caught them; a hand-derived test written by the same
author would not have, which is the whole reason `scripts/differential_test.py` exists.

The governing rule: derivatives shift obligations forward, so after the last event every
atom in the residual refers to a position that does not exist. What happens then is decided
per operator, and the operators do NOT agree with each other.
"""

from __future__ import annotations

import pytest

from trueplumb import TraceEvent
from trueplumb.ltl.ast import Atom, to_nnf
from trueplumb.ltl.automata import accepts, deriv, simplify
from trueplumb.ltl.parser import parse
from trueplumb.ltl.trace import check


def ev(*steps: list[str]) -> list[TraceEvent]:
    return [TraceEvent(index=i, atoms=frozenset(s)) for i, s in enumerate(steps)]


def residual_after(formula: str, *steps: list[str]) -> object:
    f = simplify(to_nnf(parse(formula)))
    for step in steps:
        f = deriv(f, frozenset(step))
    return f


class TestAtomWitnessing:
    def test_bare_atom_is_never_witnessed(self) -> None:
        # An atom names a fact that must occur. No future position exists to carry it.
        assert accepts(parse("a")) is False

    def test_negated_atom_is_vacuously_true(self) -> None:
        # `!a` is a claim about everything in the future, and the future is empty. There
        # is no position at which a could be violated, so the claim survives.
        assert accepts(parse("!a")) is True

    def test_stalled_run_cannot_be_laundered_into_compliant(self) -> None:
        # The product-level guarantee: a run that never finishes is NOT compliant.
        # This is the single most important case in the file -- an agent that stalls
        # halfway through a policy must not be able to report a pass.
        assert not check("F(done)", ev(["work"], ["work"], ["work"])).compliant
        assert not check("G(!dangerous) && F(done)", ev(["safe"], ["safe"])).compliant


class TestAlwaysIsVacuouslyTrue:
    def test_globally_is_true_over_an_empty_future(self) -> None:
        assert accepts(parse("G(a)")) is True

    def test_globally_holds_on_a_trace_that_ends_in_the_atom(self) -> None:
        # Derivative of G(a) after a step holding a is still G(a), which is a statement
        # about the future. The future here contains nothing, so it holds.
        assert check("G(a)", ev(["a"], ["a"])).compliant

    def test_globally_fails_when_a_single_step_breaks_it(self) -> None:
        assert not check("G(a)", ev(["a"], ["b"], ["a"])).compliant

    def test_nested_globally_is_also_vacuous(self) -> None:
        assert accepts(parse("G(G(a))")) is True


class TestEventuallyNeedsARealWitness:
    def test_eventually_bottoms_out_at_the_atom(self) -> None:
        # F(a) reduces to a, and a has no position to be witnessed at.
        assert accepts(parse("F(a)")) is False

    def test_eventually_of_globally_is_witnessed_by_the_end(self) -> None:
        # This is the subtle one, and it was wrong twice during development.
        #
        # F(G(b)) asks whether there is a position from which b holds forever. On a finite
        # trace there is no such position inside the trace, but the END position is a
        # legitimate witness: from there, "b holds forever" is true of the empty future.
        # So the absence of any future counterexample is itself the proof, and the verdict
        # is True.
        #
        # Collapsing F(p) to an unconditional False -- the intuitive shortcut -- would
        # reject every trace whose entire evidence is "the policy held to the end", which
        # is precisely the evidence you most want to accept.
        assert accepts(parse("F(G(b))")) is True
        assert check("F(G(b))", ev(["b"], ["b"])).compliant

    def test_nested_eventually_still_fails_without_the_vacuous_anchor(self) -> None:
        # F(G(F(a))) has no vacuous anchor: G(F(a)) is not vacuously true at the end,
        # because F(a) at that position still needs a real witness. So the whole thing
        # fails. Vacuity is structural, not a blanket "F at the end is True".
        assert accepts(parse("F(G(F(a)))")) is False

    def test_eventually_of_a_conjunct_needs_the_whole_conjunct(self) -> None:
        assert accepts(parse("F(a && b)")) is False
        assert check("F(a && b)", ev(["a"], ["a", "b"])).compliant


class TestNextHasNoSuccessor:
    def test_next_is_never_witnessed(self) -> None:
        assert accepts(parse("X(a)")) is False

    def test_next_fails_even_when_the_last_event_holds_the_atom(self) -> None:
        # Judging X(a) against the terminal event's valuation would read the last step as
        # "next" and wrongly accept. The successor genuinely does not exist.
        assert not check("X(a)", ev(["a"])).compliant
        assert not check("X(X(a))", ev(["a"], ["a"])).compliant

    def test_next_is_satisfied_by_an_interior_position(self) -> None:
        assert check("X(a)", ev(["start"], ["a"])).compliant


class TestUntilRequiresARealRightWitness:
    def test_until_is_false_when_the_right_never_occurred(self) -> None:
        assert not check("p until q", ev(["p"], ["p"], ["p"])).compliant

    def test_until_needs_the_right_operand_at_a_real_position(self) -> None:
        # The regression that took the longest to find. `accepts(f.right)` looked
        # reasonable -- an until's right operand must hold at the end -- but vacuous truth
        # over the empty suffix is NOT a witness for U. On [b] the trace normalises to
        # (!a) U (!b), and `!b` is vacuously true at the end; accepting that would let an
        # obligation the agent never actually satisfied be discharged by the end of the
        # trace.
        assert accepts(to_nnf(parse("!(a R b)"))) is False
        assert not check("!(a R b)", ev(["b"])).compliant

    def test_until_holds_when_the_right_operand_occurs(self) -> None:
        assert check("p until q", ev(["p"], ["p"], ["q"])).compliant

    def test_until_requires_the_left_before_the_right(self) -> None:
        assert not check("p until q", ev(["other"], ["q"])).compliant


class TestReleaseIsVacuouslyTrue:
    def test_release_is_true_over_an_empty_future(self) -> None:
        assert accepts(parse("a R b")) is True

    def test_release_holds_on_a_pure_right_trace(self) -> None:
        assert check("a release b", ev(["b"], ["b"], ["b"])).compliant

    def test_release_fails_when_both_operands_are_false(self) -> None:
        # a R b fails at the first position where neither a nor b holds, because there is
        # no release point to bind to.
        assert not check("a release b", ev(["a"], ["a"])).compliant

    def test_negated_release_is_false_when_the_release_holds(self) -> None:
        assert not check("!(a R b)", ev(["b"], ["b"])).compliant


class TestNegationIsFullyPushedDown:
    def test_nnf_removes_every_double_negation(self) -> None:
        assert to_nnf(parse("!!a")) == to_nnf(parse("a"))
        assert isinstance(to_nnf(parse("!!a")), Atom)

    def test_negated_release_becomes_an_until(self) -> None:
        # !(a R b) must normalise to (!a) U (!b). If to_nnf left a Not wrapping a
        # BinaryTemporal, accepts() would take its negation branch and recurse instead of
        # applying the release rule, producing a confident wrong answer.
        normalised = to_nnf(parse("!(a R b)"))
        assert "U" in str(normalised) and "R" not in str(normalised)

    def test_globally_of_eventually_is_not_the_same_as_eventually_of_globally(self) -> None:
        # A sanity check on the dual mapping, since these two are easy to confuse and
        # both appear in real policies.
        assert accepts(parse("G(F(a))")) is False
        assert accepts(parse("F(G(a))")) is True


class TestCounterexampleExtraction:
    def test_violation_index_is_the_first_failing_step(self) -> None:
        # Not the last compliant one. A developer needs the earliest point the policy
        # broke, because everything after it is downstream of that decision.
        result = check("G(!dangerous)", ev(["safe"], ["dangerous"], ["safe"]))
        assert not result.compliant
        assert result.violation_index == 1

    def test_violation_at_the_final_step_is_still_reported(self) -> None:
        result = check("G(!dangerous)", ev(["safe"], ["safe"], ["dangerous"]))
        assert not result.compliant
        assert result.violation_index == 2

    def test_counterexample_is_the_failing_suffix(self) -> None:
        result = check("G(!dangerous)", ev(["safe"], ["dangerous"], ["safe"]))
        assert [e.index for e in result.counterexample] == [1, 2]

    def test_the_counterexample_slice_itself_is_violating(self) -> None:
        # A counterexample you cannot replay is not evidence. Re-checking the extracted
        # slice must independently fail, which is what makes it worth handing to a user.
        result = check("G(!dangerous)", ev(["safe"], ["dangerous"], ["safe"]))
        assert result.counterexample
        assert not check("G(!dangerous)", list(result.counterexample)).compliant

    def test_compliant_trace_has_no_counterexample(self) -> None:
        result = check("G(!dangerous)", ev(["safe"], ["safe"]))
        assert result.compliant
        assert result.counterexample == ()
        assert result.violation_index is None


class TestRealisticPolicies:
    REFUND_APPROVAL = "G(call_refund -> F(human_approved))"

    def test_refund_without_approval_is_violation(self) -> None:
        result = check(
            self.REFUND_APPROVAL,
            ev(["lookup_order"], ["call_refund"], ["send_receipt"]),
        )
        assert not result.compliant
        assert result.counterexample

    def test_refund_with_later_approval_is_compliant(self) -> None:
        result = check(
            self.REFUND_APPROVAL,
            ev(["lookup_order"], ["call_refund"], ["human_approved"]),
        )
        assert result.compliant, result.explain()

    def test_no_destructive_calls(self) -> None:
        policy = "G(!delete_records) && G(!drop_table)"
        assert check(policy, ev(["read"], ["write"])).compliant
        assert not check(policy, ev(["read"], ["delete_records"])).compliant

    def test_approval_must_precede_the_dangerous_call(self) -> None:
        # "Approved eventually" is not enough -- a refund that completes before the
        # approval lands is still a violation, because F only looks forward from the
        # dangerous step.
        policy = "G(human_approved -> F(call_refund))"
        assert check(policy, ev(["human_approved"], ["call_refund"])).compliant
        assert not check(policy, ev(["call_refund"], ["human_approved"])).compliant

    def test_never_unapproved_destructive_call(self) -> None:
        # The policy most teams actually want. The approval must be true at the *same step*
        # as the destructive call, because G distributes the requirement over every
        # position: an approval recorded at step 0 says nothing about step 1.
        #
        # The "lapses" case below is the one worth internalising. Reading an approval as
        # a sticky licence ("once approved, always approved") is the classic way to write
        # a policy that looks safe and is not -- a human approving one delete silently
        # authorises every later one.
        policy = "G(!delete_records || approved)"
        assert check(policy, ev(["read"], ["write"])).compliant
        assert not check(policy, ev(["delete_records"])).compliant
        assert check(policy, ev(["read"], ["approved", "delete_records"])).compliant
        assert not check(policy, ev(["approved", "delete_records"], ["delete_records"])).compliant


class TestDeterminism:
    """The product claim: same input, same verdict, every time, on every machine."""

    TRACES = [
        ev([], ["a"], ["b"]),
        ev(["a"], ["dangerous"], ["a"], ["b"], ["c"]),
        ev(["a"], ["b"], ["c"], ["d"], ["e"], ["f"]),
    ]
    POLICIES = [
        "G(!dangerous)",
        "G(a -> F(b))",
        "F(done) && G(!dangerous)",
        "a U b",
        "a R b",
    ]

    @pytest.mark.parametrize("policy", POLICIES)
    def test_repeated_runs_agree(self, policy: str) -> None:
        first = [check(policy, t).compliant for t in self.TRACES]
        second = [check(policy, t).compliant for t in self.TRACES]
        assert first == second

    def test_monitor_construction_is_reproducible(self) -> None:
        from trueplumb.ltl.automata import to_dfa

        for policy in self.POLICIES:
            a = to_dfa(parse(policy))
            b = to_dfa(parse(policy))
            assert a.build_stats == b.build_stats
            assert a.transitions == b.transitions
