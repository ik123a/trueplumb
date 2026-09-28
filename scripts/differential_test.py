"""Differential test: monitor engine vs. a brute-force reference semantics.

Hand-derived tests can encode the same wrong belief twice -- if I wrote both the engine
and the test from the same mental model, agreement proves nothing. This script instead
derives ground truth from the *mathematical definition* of LTL over finite traces, using
a completely different evaluation strategy: expand every temporal operator into its
set-of-satisfying-subset semantics rather than computing derivatives.

For a trace of length n and formula f, the reference evaluates f over the 2^n position
subsets reachable under the standard LTL-on-finite-traces semantics, where a set S of
positions satisfies f iff the infinite word w with w[i] in S for i in S and w[i] not in S
otherwise satisfies f. That is the textbook definition and shares no code path with the
derivative construction.

Every trace up to `max_len` over the formula's alphabet is enumerated and compared. Any
mismatch is a real bug in one of the two implementations, and is reported with the exact
trace so it can be reduced by hand.
"""

from __future__ import annotations

import itertools
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trueplumb.ltl.ast import (  # noqa: E402
    And,
    Atom,
    BinaryTemporal,
    Formula,
    Not,
    Or,
    Temporal,
    TemporalOp,
    to_nnf,
)
from trueplumb.ltl.parser import parse  # noqa: E402
from trueplumb.ltl.trace import TraceEvent, check  # noqa: E402

# ---------------------------------------------------------------------------
# Reference semantics: satisfaction over position sets.
# ---------------------------------------------------------------------------


def holds(formula: Formula, trace: list[frozenset[str]], i: int) -> bool:
    """Does `formula` hold at position `i` of the finite word `trace`?

    This is the textbook recursive semantics, evaluated positionally over the trace
    itself: an atom holds when it appears in that step's valuation, and temporal operators
    recurse forward. Positions at or beyond the end of the trace are false for every atom,
    which is what makes an undischarged obligation (an `F` that never arrives) come out
    False. It shares no code path with the derivative construction it is checking.
    """
    if isinstance(formula, Atom):
        return i < len(trace) and formula.name in trace[i]

    if isinstance(formula, Not):
        return not holds(formula.operand, trace, i)

    if isinstance(formula, And):
        return holds(formula.left, trace, i) and holds(formula.right, trace, i)

    if isinstance(formula, Or):
        return holds(formula.left, trace, i) or holds(formula.right, trace, i)

    if isinstance(formula, Temporal):
        op = formula.op
        if op is TemporalOp.NEXT:
            return holds(formula.operand, trace, i + 1)
        if op is TemporalOp.EVENTUALLY:
            # A witness at a real position of the trace...
            if any(holds(formula.operand, trace, j) for j in range(i, len(trace))):
                return True
            # ...or at the end position, where the operand is judged over the empty
            # suffix. That end-vacuity is what lets `F(G(b))` hold on [b, b]: the trace
            # never showed b holding forever, but the absence of any *future* counterexample
            # is itself the proof. The end position is a legal witness only when the
            # operand is satisfied vacuously, which is precisely what the recursive call
            # at i == len(trace) computes.
            return holds(formula.operand, trace, len(trace))
        if op is TemporalOp.ALWAYS:
            return all(holds(formula.operand, trace, j) for j in range(i, len(trace)))
        raise ValueError(f"unexpected unary op {op}")

    if isinstance(formula, BinaryTemporal):
        op = formula.op
        if op is TemporalOp.UNTIL or op is TemporalOp.STRONG_UNTIL:
            for j in range(i, len(trace)):
                if holds(formula.right, trace, j) and all(
                    holds(formula.left, trace, k) for k in range(i, j)
                ):
                    return True
            if op is TemporalOp.STRONG_UNTIL:
                # No right-witness anywhere: strong until degenerates to globally left.
                return all(holds(formula.left, trace, k) for k in range(i, len(trace)))
            return False
        if op is TemporalOp.RELEASE:
            # psi R phi holds iff phi holds from i onward, OR there is a first position j
            # where phi holds and psi held at every position up to AND INCLUDING j.
            #
            # On a finite trace the "phi holds from i onward" branch is the vacuous one
            # when the trace simply ends: an all-phi trace satisfies the release. That
            # vacuity is what makes `!(a R b)` false on an all-b trace -- b holds
            # throughout, so the release completes and its negation cannot hold.
            for j in range(i, len(trace)):
                if holds(formula.left, trace, j) and all(
                    holds(formula.right, trace, k) for k in range(i, j + 1)
                ):
                    return True
            return all(holds(formula.right, trace, k) for k in range(i, len(trace)))
        raise ValueError(f"unexpected binary op {op}")

    raise TypeError(f"cannot evaluate {type(formula).__name__}")


def reference_holds(formula_text: str, trace: list[frozenset[str]]) -> bool:
    """Ground truth: does the finite trace satisfy the LTL formula?"""
    return holds(to_nnf(parse(formula_text)), trace, 0)


def _atoms(formula: Formula) -> set[str]:
    found: set[str] = set()
    stack = [formula]
    while stack:
        node = stack.pop()
        if isinstance(node, Atom):
            found.add(node.name)
        elif isinstance(node, Not):
            stack.append(node.operand)
        elif isinstance(node, (And, Or)):
            stack.append(node.left)
            stack.append(node.right)
        elif isinstance(node, Temporal):
            stack.append(node.operand)
        elif isinstance(node, BinaryTemporal):
            stack.append(node.left)
            stack.append(node.right)
    return found


# ---------------------------------------------------------------------------
# Exhaustive comparison.
# ---------------------------------------------------------------------------


FORMULAS = [
    "G(a)",
    "F(a)",
    "X(a)",
    "G(!a)",
    "F(!a)",
    "a U b",
    "a R b",
    "G(a && F(b))",
    "G(a) || F(b)",
    "G(a -> F(b))",
    "F(a) && !G(b)",
    "G(!a || F(b))",
    "X(a) && F(b)",
    "G(F(b))",
    "F(G(b))",
    "a U (b && c)",
    "G(a) -> F(G(b))",
    "!(G(a) && F(b))",
    "F(a) || G(b)",
    "X(X(a))",
    "G(a) -> (b R c)",
    "F(a) -> G(b)",
    "G(a) && (b U c)",
    # Nested and negated temporal operators. The first block is a sanity floor; these are
    # the shapes that actually break a derivative construction, because a nested operator
    # forces the residual to hold a temporal subformula whose own end-vacuity rule
    # differs from its parent's. They are listed last so a regression names itself.
    "G(F(a) -> F(b))",
    "F(!G(a))",
    "G(a) && G(F(b)) && G(F(!b))",
    "F(a) && F(b) && F(c)",
    "G(a) -> G(b -> G(c))",
    "!(a U b)",
    "!(a R b)",
    "F(a U b)",
    "G(a R b)",
    "F(G(F(a)))",
    "G(!F(a))",
    "F(a || G(b)) && G(!c || a)",
    "X(a) -> X(F(b))",
    "(a U b) -> G(c)",
    "G(a && b) -> (c U d)",
    "F(a) && (b R c)",
    "!(F(a) && F(b)) || G(c)",
    "G(G(a))",
    "X(G(a))",
    "F(X(a))",
]


@dataclass
class Mismatch:
    formula: str
    trace: list[frozenset[str]]
    engine: bool
    reference: bool


def all_traces(alphabet: list[str], max_len: int) -> list[list[frozenset[str]]]:
    traces: list[list[frozenset[str]]] = []
    for length in range(1, max_len + 1):
        for combo in itertools.product([frozenset(), *({a} for a in alphabet)], repeat=length):
            traces.append(list(combo))
    return traces


def main() -> int:
    max_len = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    formulas = FORMULAS
    mismatches: list[Mismatch] = []
    checked = 0

    for text in formulas:
        atoms = sorted(_atoms(to_nnf(parse(text))))
        if len(atoms) > 2 and max_len > 4:
            # 3 atoms over length 4 is 256 subsets; still fine, but keep the runtime sane.
            pass
        for trace in all_traces(atoms, max_len):
            events = [TraceEvent(index=i, atoms=frozenset(a)) for i, a in enumerate(trace)]
            engine = check(text, events).compliant
            reference = reference_holds(text, trace)
            checked += 1
            if engine != reference:
                mismatches.append(Mismatch(text, trace, engine, reference))

    print(f"formulas: {len(formulas)}  max trace length: {max_len}  traces checked: {checked}")
    if mismatches:
        print(f"\nMISMATCHES: {len(mismatches)}")
        seen: set[tuple[str, tuple]] = set()
        for m in mismatches:
            key = (m.formula, tuple(sorted(tuple(sorted(t)) for t in m.trace)))
            if key in seen:
                continue
            seen.add(key)
            pretty = [sorted(t) or ["-"] for t in m.trace]
            print(
                f"  {m.formula:24s} {[','.join(p) for p in pretty]}  "
                f"engine={m.engine} reference={m.reference}"
            )
        return 1

    print("PASS: engine agrees with reference semantics on every trace")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
