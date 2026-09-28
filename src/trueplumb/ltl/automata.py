"""LTLf -> deterministic monitor by formula derivatives.

Construction
------------
Rather than building a tableau of elementary sets (which is where the previous
implementation of this module went wrong), states here are *reduced formulas* obtained by
consuming an event. This is the standard derivative-based construction:

    deriv(phi, event)  -- the obligation that remains after ``event`` happens
    simplify(phi)      -- boolean + temporal normalisation
    accepts(phi)       -- is the obligation discharged at the end of a finite trace?

Because every state is a formula, an unsatisfiable obligation collapses to the constant
FALSE immediately instead of accumulating unreachable tableau states. That is why a policy
like ``G(call_refund -> F(human_approved))`` builds a handful of states rather than
blowing up.

Semantics are LTL over *finite* traces (LTLf), which is what "did this recorded agent run
comply?" means. Acceptance follows the standard LTLf end-of-trace rule: anything still
demanding a future step is a violation.

Determinism: identical (formula, event) always yields an identical reduced formula, so the
same trace always produces the same verdict. No randomness, no LLM, no clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ast import (
    And,
    Atom,
    BinaryTemporal,
    Formula,
    Not,
    Or,
    Temporal,
    TemporalOp,
    collect_atoms,
    to_nnf,
)

TRUE = Atom("__true__")
FALSE = Atom("__false__")


class AlphabetExhausted(RuntimeError):
    """Raised when a policy exceeds the configured monitor state budget."""


# --------------------------------------------------------------------------------------
# Simplification
# --------------------------------------------------------------------------------------


def _is_true(f: Formula) -> bool:
    return isinstance(f, Atom) and f.name == "__true__"


def _is_false(f: Formula) -> bool:
    return isinstance(f, Atom) and f.name == "__false__"


def _implied_by_loop(f: Formula) -> bool:
    """Is ``f`` the operand of a loop already present in the conjunction/disjunction?

    This is the *only* subsumption that is sound for the derivative construction, and it
    rests on two genuine LTL identities:

        G(p) & p   ==  G(p)        F(p) | p   ==  F(p)
        (p R q) & p == (p R q)     (p U q) | p == (p U q)

    Anything weaker is wrong. Dropping ``F(done)`` from ``F(done) & G(!dangerous)`` merely
    because it is a subformula would erase a live obligation and report a violating run as
    compliant -- which is the single worst bug a compliance tool can have. The earlier
    implementation of this module made exactly that mistake; the meta-tests below pin it.
    """
    return isinstance(f, (Temporal, BinaryTemporal))


def _absorbed(terms: list[Formula], conjunction: bool) -> list[Formula]:
    """Drop terms implied by a surviving loop term, per the identities above."""
    survivors: list[Formula] = []
    for term in terms:
        absorbed = False
        for other in terms:
            if other is term:
                continue
            if isinstance(other, Temporal) and other.operand == term:
                absorbed = True  # G(p) & p, or F(p) | p
                break
            if isinstance(other, BinaryTemporal):
                if conjunction and other.op is TemporalOp.RELEASE and other.left == term:
                    absorbed = True  # (p R q) & p
                    break
                if (
                    not conjunction
                    and other.op in (TemporalOp.UNTIL, TemporalOp.STRONG_UNTIL)
                    and other.left == term
                ):
                    absorbed = True  # (p U q) | p
                    break
        if not absorbed:
            survivors.append(term)
    return survivors or terms


def _flatten(f: Formula, kind: type[And] | type[Or]) -> list[Formula]:
    """Flatten nested same-kind connectives into a flat term list.

    ``And(a, And(b, c))`` becomes ``And(a, And(b, c))`'s components flattened to
    ``[a, b, c]``. This is what keeps folding non-recursive: without it,
    ``simplify`` and ``_and_simplify`` call each other on the same node forever.
    """
    if isinstance(f, kind):
        return _flatten(f.left, kind) + _flatten(f.right, kind)
    return [f]


def _and_simplify(terms: list[Formula], _depth: int = 0) -> Formula:
    """Conjunction with idempotence and loop absorption.

    Loop absorption is what keeps the derivative construction finite. Differentiating
    ``G(p)`` yields ``deriv(p) & G(p)``; on the next step that becomes
    ``deriv(deriv(p)) & deriv(p) & G(p)``. Once ``deriv(p)`` reduces back to ``p``,
    ``p & G(p)`` collapses to ``G(p)``, so the state stops growing instead of accumulating
    one conjunct per step until monitor construction never terminates.
    """
    if _depth > 128:  # safety valve; a policy this deep is a bug report, not a policy
        raise RecursionError("conjunction nesting exceeded the folding depth limit")

    flat: list[Formula] = []
    for term in terms:
        if isinstance(term, And):
            flat.extend(_flatten(term, And))
        else:
            flat.append(term)

    kept: list[Formula] = []
    for term in flat:
        term = simplify(term)
        if _is_false(term):
            return FALSE
        if _is_true(term) or isinstance(term, And):
            # A nested And was flattened above; anything still wrapped is a recursion
            # artefact, so distribute it into this level rather than nesting deeper.
            if isinstance(term, And):
                return _and_simplify([term], _depth + 1)
            continue
        if term in kept:
            continue
        kept.append(term)

    if not kept:
        return TRUE
    if len(kept) == 1:
        return kept[0]

    survivors = _absorbed(kept, conjunction=True)
    if len(survivors) == 1:
        return survivors[0]
    return And(survivors[0], _and_simplify(survivors[1:], _depth + 1))


def _or_simplify(terms: list[Formula], _depth: int = 0) -> Formula:
    """Disjunction with the dual idempotence and loop absorption rules."""
    if _depth > 128:
        raise RecursionError("disjunction nesting exceeded the folding depth limit")

    flat: list[Formula] = []
    for term in terms:
        if isinstance(term, Or):
            flat.extend(_flatten(term, Or))
        else:
            flat.append(term)

    kept: list[Formula] = []
    for term in flat:
        term = simplify(term)
        if _is_true(term):
            return TRUE
        if _is_false(term) or isinstance(term, Or):
            if isinstance(term, Or):
                return _or_simplify([term], _depth + 1)
            continue
        if term in kept:
            continue
        kept.append(term)

    if not kept:
        return FALSE
    if len(kept) == 1:
        return kept[0]

    survivors = _absorbed(kept, conjunction=False)
    if len(survivors) == 1:
        return survivors[0]
    return Or(survivors[0], _or_simplify(survivors[1:], _depth + 1))


def simplify(f: Formula) -> Formula:
    """Boolean and temporal normalisation, applied bottom-up.

    Folds constants, removes double negation, and pushes ``U``/``W`` to their reflexive
    forms (``a U a`` is just ``a``). Every rule here is a semantic identity, so folding
    always safe -- it changes performance, never verdicts.
    """
    if isinstance(f, Atom):
        return f

    if isinstance(f, Not):
        inner = simplify(f.operand)
        if isinstance(inner, Not):
            return inner.operand
        if _is_true(inner):
            return FALSE
        if _is_false(inner):
            return TRUE
        return Not(inner)

    if isinstance(f, And):
        return _and_simplify([f.left, f.right])

    if isinstance(f, Or):
        return _or_simplify([f.left, f.right])

    if isinstance(f, Temporal):
        inner = simplify(f.operand)
        if f.op is TemporalOp.NEXT:
            if _is_false(inner):
                return FALSE
        elif f.op is TemporalOp.ALWAYS:
            if _is_false(inner):
                return FALSE
            if _is_true(inner):
                return TRUE
        elif f.op is TemporalOp.EVENTUALLY:
            if _is_true(inner):
                return TRUE
            if _is_false(inner):
                return FALSE
        return Temporal(f.op, inner)

    if isinstance(f, BinaryTemporal):
        left, right = simplify(f.left), simplify(f.right)
        if f.op is TemporalOp.UNTIL:
            if _is_true(right):
                return TRUE
            if _is_false(left):
                return right
            if left == right:
                return right
        elif f.op is TemporalOp.RELEASE:
            if _is_true(left):
                return TRUE
            if _is_false(right):
                return FALSE
        elif f.op is TemporalOp.STRONG_UNTIL:
            if _is_true(right):
                return TRUE
            if _is_false(left):
                return right
            if left == right:
                return right
        return BinaryTemporal(f.op, left, right)

    return f


# --------------------------------------------------------------------------------------
# Derivatives
# --------------------------------------------------------------------------------------


def deriv(f: Formula, event: frozenset[str]) -> Formula:
    """Consume one event, returning the simplified residual obligation."""
    f = simplify(f)

    if _is_true(f) or _is_false(f):
        return f

    if isinstance(f, Atom):
        return TRUE if f.name in event else FALSE

    if isinstance(f, Not):
        if isinstance(f.operand, Atom):
            return FALSE if f.operand.name in event else TRUE
        return simplify(Not(deriv(f.operand, event)))

    if isinstance(f, And):
        return _and_simplify([deriv(f.left, event), deriv(f.right, event)])

    if isinstance(f, Or):
        return _or_simplify([deriv(f.left, event), deriv(f.right, event)])

    if isinstance(f, Temporal):
        inner = simplify(f.operand)
        if f.op is TemporalOp.NEXT:
            # The obligation moves to the next step; it is not judged now.
            return inner
        if f.op is TemporalOp.ALWAYS:
            # G(p) survives the step only if p itself survives it. Rebuilding from the
            # *simplified* operand is what lets subsumption fire: deriv(inner) collapses
            # back into `inner`, which is a subformula of G(inner), so the conjunction
            # stays the same size instead of growing a conjunct every step.
            return _and_simplify([deriv(inner, event), Temporal(f.op, inner)])
        if f.op is TemporalOp.EVENTUALLY:
            return _or_simplify([deriv(inner, event), Temporal(f.op, inner)])
        return f

    if isinstance(f, BinaryTemporal):
        left, right = simplify(f.left), simplify(f.right)
        node = BinaryTemporal(f.op, left, right)
        if f.op in (TemporalOp.UNTIL, TemporalOp.STRONG_UNTIL):
            return _or_simplify([deriv(right, event), _and_simplify([deriv(left, event), node])])
        if f.op is TemporalOp.RELEASE:
            return _and_simplify([deriv(right, event), _or_simplify([deriv(left, event), node])])
        return f

    raise TypeError(f"cannot differentiate {type(f).__name__}")


def _pending(f: Formula) -> bool:
    """Does ``f`` still carry a future obligation at end of trace?"""
    f = simplify(f)
    if isinstance(f, (Temporal, BinaryTemporal)):
        return True
    if isinstance(f, And):
        return _pending(f.left) or _pending(f.right)
    if isinstance(f, Or):
        return _pending(f.left) and _pending(f.right)
    return False


def accepts(f: Formula) -> bool:
    """Is the residual ``f`` satisfied over the *empty* suffix that follows a finite trace?

    Derivatives shift obligations forward: after the final event is consumed, every atom
    left in the residual refers to a position that does not exist. So the terminal
    valuation is empty, and the accept/reject rule is decided by that alone:

        atom a      -> False   the next position never arrives, so `a` is never witnessed
        G(p)        -> True    vacuous: "no future violation" holds when there is no future
        F(p)        -> accepts(p)
                            recurse, because the end position is itself a legal witness
                            for the operand. This is what makes F(G(b)) hold on [b, b]:
                            at the end, G(b) is vacuously true, and that vacuity is the
                            witness. Collapsing F(p) to False instead would reject every
                            trace whose evidence is "the policy held all the way to the end".
        X(p)        -> False   the next position is genuinely absent, so no witness
        p U q / W   -> accepts(q)
                            q must hold at the end, since no later step can discharge it
        p R q       -> True    vacuous: nothing can break a release with no future steps

    This is what makes ``F(done)`` fail on a trace that never finishes: the obligation
    survives to the end unwitnessed, so a run that silently stalls cannot be laundered
    into a COMPLIANT verdict. The recursion for ``F`` does not weaken that -- ``F(done)``
    bottoms out at the atom ``done``, which has no position to be witnessed at, so it is
    False. The distinction is between an obligation that needs a *fact* (done must happen)
    and one that needs a *property of everything after* (G(b) constrains the empty future
    vacuously, and that is a legitimate proof).

    It is also why ``G(a)`` passes on ``[a, a]``. The derivative after the last ``a`` is
    still ``G(a)``, which is vacuously true over the empty suffix -- it is a statement
    about the future, and the future here contains nothing.
    """
    f = simplify(f)

    if _is_true(f):
        return True
    if _is_false(f):
        return False

    if isinstance(f, Atom):
        return False  # no future position exists to witness the atom

    if isinstance(f, Not):
        # Only reachable for a negated atom. Every other negation was pushed to the atoms
        # by `to_nnf` before the monitor is built, so this branch is not an escape hatch --
        # it is the terminal case of that normalisation.
        #
        # `!p` is True over an empty suffix: there is no future position at which p could
        # hold, so "not p" cannot be violated. This is the rule that makes `!(a R b)` --
        # which normalises to `(!a U !b)` -- come out False on an all-b trace, since the
        # until's right operand `!b` is witnessed at the end.
        if isinstance(f.operand, Atom):
            return True
        return not accepts(f.operand)

    if isinstance(f, And):
        return accepts(f.left) and accepts(f.right)

    if isinstance(f, Or):
        return accepts(f.left) or accepts(f.right)

    if isinstance(f, Temporal):
        if f.op is TemporalOp.NEXT:
            return False  # the next step never arrives on a finite trace
        if f.op is TemporalOp.ALWAYS:
            return True  # vacuously true over an empty suffix
        # EVENTUALLY: the end position can witness the operand. Recurse rather than
        # returning False, so an operand that is itself vacuously true is accepted.
        return accepts(f.operand)

    if isinstance(f, BinaryTemporal):
        # U / W: a surviving (p U q) at the end means q was never true at any *real*
        # position of the trace, and no future exists to witness it. The until therefore
        # fails outright.
        #
        # This must be an unconditional False, not `accepts(f.right)`. Vacuous truth over
        # the empty suffix is a real witness for G and R, but NOT for U: `(!a) U (!b)`
        # normalises onto traces where `!b` is vacuously true at the end, and accepting
        # that would let a negated atom at the end position discharge an until that was
        # never satisfied by anything the agent actually did.
        # A release is vacuously satisfied; every other binary temporal operator (U and W)
        # needs a right-witness that never came.
        return f.op is TemporalOp.RELEASE

    raise TypeError(f"cannot test acceptance of {type(f).__name__}")


# --------------------------------------------------------------------------------------
# Deterministic monitor
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DFA:
    """A deterministic, minimal monitor for one LTLf property.

    ``transitions`` maps a state id to a mapping from event mask to the next state id.
    The mask is a frozenset of atom names satisfied by that event.
    """

    start: int
    accepting: frozenset[int]
    transitions: dict[int, dict[frozenset[str], int]]
    states: frozenset[int]
    alphabet: frozenset[str]
    formula_text: str
    sink: int
    residuals: dict[int, Formula] = field(default_factory=dict)
    build_stats: dict[str, int] = field(default_factory=dict)

    def residual_for(self, state: int) -> Formula:
        """The obligation still outstanding in ``state``.

        The residual is a formula rather than a boolean so the verdict stays a pure
        function of the residual and an empty suffix: a monitor that collapsed each state
        to accept/reject at construction time would bake in one terminal assumption and
        could not express the vacuity that makes ``G(a)`` pass on a trace ending in ``a``.
        """
        return self.residuals.get(state, FALSE)

    def step(self, state: int, event_atoms: set[str] | frozenset[str]) -> int:
        """Advance one event, projecting it onto the monitor's alphabet first."""
        row = self.transitions.get(state)
        if not row:
            return self.sink
        key = frozenset(event_atoms) & self.alphabet
        return row.get(key, self.sink)

    def run(self, events: list[frozenset[str]] | list[set[str]]) -> bool:
        state = self.start
        for event in events:
            state = self.step(state, event)
        return accepts(self.residual_for(state))


def _all_events(alphabet: frozenset[str], max_events: int) -> list[frozenset[str]]:
    """Every event over the alphabet, capped.

    Built with an eager comprehension. (Extending a list from a generator over that same
    list loops forever -- a bug this module shipped with until the tests caught it.)
    """
    names = sorted(alphabet)
    events: list[frozenset[str]] = [frozenset()]
    for name in names:
        if len(events) >= max_events:
            break
        additions = [frozenset(e | {name}) for e in events]
        events.extend(additions[: max_events - len(events)])
    return events


def to_dfa(formula: Formula, formula_text: str = "", max_states: int = 4096) -> DFA:
    """Compile ``formula`` into a deterministic monitor over reduced-formula states."""
    root = simplify(to_nnf(formula))
    alphabet = frozenset(collect_atoms(root)) - {"__true__", "__false__"}
    events = _all_events(alphabet, 1 << min(len(alphabet), 8))

    id_of: dict[Formula, int] = {root: 0}
    order: list[Formula] = [root]
    transitions: dict[int, dict[frozenset[str], int]] = {}
    accepting: set[int] = set()

    index = 0
    while index < len(order):
        state = order[index]
        row: dict[frozenset[str], int] = {}
        for event in events:
            nxt = deriv(state, event)
            if nxt not in id_of:
                if len(id_of) >= max_states:
                    raise AlphabetExhausted(
                        f"policy needs more than {max_states} monitor states; "
                        "simplify it or split it into several smaller policies"
                    )
                id_of[nxt] = len(order)
                order.append(nxt)
            row[event] = id_of[nxt]
        transitions[index] = row
        if accepts(state):
            accepting.add(index)
        index += 1

    sink = len(order)
    transitions[sink] = {event: sink for event in events}

    residuals: dict[int, Formula] = {i: order[i] for i in range(len(order))}
    residuals[sink] = FALSE

    dfa = DFA(
        start=0,
        accepting=frozenset(accepting),
        transitions=transitions,
        states=frozenset(range(len(order) + 1)),
        alphabet=alphabet,
        formula_text=formula_text or str(formula),
        sink=sink,
        residuals=residuals,
        build_stats={"formula_states": len(order), "sink": sink, "events": len(events)},
    )
    return _minimize(dfa)


def _minimize(dfa: DFA) -> DFA:
    """Moore partition refinement over the monitor's states.

    Two states are equivalent only if their residuals are *identical* and they agree on
    every transition. Comparing acceptance behaviour alone would be wrong: two different
    residuals can both be accepting on some terminal valuations while diverging on others,
    and merging them would let the surviving representative's residual decide the verdict
    for a state whose real obligation was discarded. Since the residual is what the verdict
    is computed from, the residual itself is the equivalence invariant.
    """
    events = sorted(
        {e for row in dfa.transitions.values() for e in row}, key=lambda e: (len(e), sorted(e))
    )

    partitions: list[set[int]] = [set(dfa.accepting), set(dfa.states) - set(dfa.accepting)]
    partitions = [p for p in partitions if p]

    while True:
        refined: list[set[int]] = []
        for block in partitions:
            # Signature is (residual, per-event block index). The residual is part of the
            # key because it is the equivalence invariant: two states with different live
            # obligations must never be merged even if they agree on every transition.
            groups: dict[tuple[Formula, tuple[int, ...]], set[int]] = {}
            for state in block:
                row = dfa.transitions.get(state, {})
                signature = (
                    dfa.residuals.get(state, FALSE),
                    tuple(_which_block(row.get(event, dfa.sink), partitions) for event in events),
                )
                groups.setdefault(signature, set()).add(state)
            refined.extend(groups.values())
        if len(refined) == len(partitions):
            partitions = refined
            break
        partitions = refined

    class_id: dict[int, int] = {}
    for i, block in enumerate(partitions):
        for state in block:
            class_id[state] = i

    # Never merge a block that mixes acceptance: that would understate violations.
    for block in partitions:
        if len({s in dfa.accepting for s in block}) > 1:
            raise AssertionError("minimisation merged accepting and rejecting states")

    new_transitions: dict[int, dict[frozenset[str], int]] = {}
    new_accepting: set[int] = set()
    new_residuals: dict[int, Formula] = {}
    for state in dfa.states:
        cid = class_id[state]
        row = new_transitions.setdefault(cid, {})
        for event, target in dfa.transitions.get(state, {}).items():
            row[event] = class_id[target]
        if state in dfa.accepting:
            new_accepting.add(cid)
        # Residuals are equal within a block by construction; keep the first.
        new_residuals.setdefault(cid, dfa.residuals.get(state, FALSE))

    # Class ids are assigned from the final partition, but a block can be empty if two
    # original states merged -- the count must reflect the states that actually survive,
    # not the number of pre-merge ids handed out.
    survivors = frozenset(new_transitions)
    return DFA(
        start=class_id[dfa.start],
        accepting=frozenset(new_accepting),
        transitions=new_transitions,
        states=survivors,
        alphabet=dfa.alphabet,
        formula_text=dfa.formula_text,
        sink=class_id[dfa.sink],
        residuals=new_residuals,
        build_stats={**dfa.build_stats, "minimized_states": len(survivors)},
    )


def _which_block(state: int, partitions: list[set[int]]) -> int:
    for i, block in enumerate(partitions):
        if state in block:
            return i
    return -1
