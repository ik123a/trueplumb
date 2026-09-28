"""Linear Temporal Logic abstract syntax tree and formula types.

Deliberate design constraints:
  * No LLM calls anywhere in this module. Everything is deterministic.
  * No third-party solver dependency. LTL -> automata is implemented here so the
    verification core stays auditable and dependency-free.

Reference: the operator set follows standard LTL over finite traces (LTLf), which is
what runtime verification of a recorded agent execution actually needs. The "eventually"
and "always" operators are therefore trace-relative, not infinite-word relative.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum


class TemporalOp(str, Enum):  # noqa: UP042 - str mixin is load-bearing, see below
    """Temporal operators, named as they are written in the DSL.

    The ``str`` mixin is deliberate and should not be removed to satisfy UP042. These
    values are serialised straight into the trace corpus and into JSON reports, and
    ``TemporalOp.ALWAYS`` is used as a dict key in a few places. Without the mixin every
    such site would need an explicit ``.value``, and a forgotten one would serialise as
    ``"TemporalOp.ALWAYS"`` instead of ``"always"`` -- a corpus file that no longer
    matches the parser. Making the enum *be* its string removes that whole class of bug.
    """

    __str__ = str.__str__

    ALWAYS = "always"  # G  / []  - holds at every point from here on
    EVENTUALLY = "eventually"  # F  / <>  - holds at some point from here on
    NEXT = "next"  # X  - holds at the immediately next point
    UNTIL = "until"  # U
    RELEASE = "release"  # R
    STRONG_UNTIL = "strong_until"  # W


class Formula(ABC):
    """Base class for all LTL formulas. Atoms are formulas too -- they are the leaves."""

    @abstractmethod
    def atoms(self) -> Iterator[Atom]:
        """Yield every atom occurring anywhere in this subformula."""
        raise NotImplementedError

    @abstractmethod
    def __str__(self) -> str:
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self}>"


@dataclass(frozen=True, slots=True)
class Atom(Formula):
    """A propositional atom: a named predicate over a trace event.

    Atoms are opaque strings. Who decides whether ``atom`` holds for a given event is the
    caller's job (see ``trueplumb.trace.TraceEvent``) -- this module never inspects event
    payloads, which keeps verification independent of any particular agent framework.
    """

    name: str

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("atom name must be non-empty")

    def atoms(self) -> Iterator[Atom]:
        yield self

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True, slots=True)
class Not(Formula):
    operand: Formula

    def atoms(self) -> Iterator[Atom]:
        yield from self.operand.atoms()

    def __str__(self) -> str:
        return f"!({self.operand})"


@dataclass(frozen=True, slots=True)
class And(Formula):
    left: Formula
    right: Formula

    def atoms(self) -> Iterator[Atom]:
        yield from self.left.atoms()
        yield from self.right.atoms()

    def __str__(self) -> str:
        return f"({self.left} && {self.right})"


@dataclass(frozen=True, slots=True)
class Or(Formula):
    left: Formula
    right: Formula

    def atoms(self) -> Iterator[Atom]:
        yield from self.left.atoms()
        yield from self.right.atoms()

    def __str__(self) -> str:
        return f"({self.left} || {self.right})"


@dataclass(frozen=True, slots=True)
class Implies(Formula):
    """``a -> b``, sugar for ``!a || b``."""

    left: Formula
    right: Formula

    def atoms(self) -> Iterator[Atom]:
        yield from self.left.atoms()
        yield from self.right.atoms()

    def __str__(self) -> str:
        return f"({self.left} -> {self.right})"


@dataclass(frozen=True, slots=True)
class Temporal(Formula):
    """A unary temporal operator applied to a subformula."""

    op: TemporalOp
    operand: Formula

    def atoms(self) -> Iterator[Atom]:
        yield from self.operand.atoms()

    def __str__(self) -> str:
        if self.op is TemporalOp.NEXT:
            return f"X({self.operand})"
        if self.op is TemporalOp.ALWAYS:
            return f"G({self.operand})"
        return f"F({self.operand})"


@dataclass(frozen=True, slots=True)
class BinaryTemporal(Formula):
    """A binary temporal operator: ``U`` (until), ``R`` (release), ``W`` (strong until)."""

    op: TemporalOp
    left: Formula
    right: Formula

    def atoms(self) -> Iterator[Atom]:
        yield from self.left.atoms()
        yield from self.right.atoms()

    def __str__(self) -> str:
        symbols = {
            TemporalOp.UNTIL: "U",
            TemporalOp.RELEASE: "R",
            TemporalOp.STRONG_UNTIL: "W",
        }
        return f"({self.left} {symbols[self.op]} {self.right})"


def normalize(formula: Formula) -> Formula:
    """Collapse trivial temporal wrappers.

    ``G(F(p))`` is just ``F(p)``: if some point exists at all, "always eventually" is the
    same statement on a finite trace. These collapses keep the automaton construction from
    building state the formula never needed, which is where the state explosion in
    LTL-to-DFA translation comes from.
    """
    if isinstance(formula, Temporal):
        inner = normalize(formula.operand)
        if (
            formula.op is TemporalOp.ALWAYS
            and isinstance(inner, Temporal)
            and inner.op is TemporalOp.EVENTUALLY
        ):
            return inner
        return Temporal(formula.op, inner)

    if isinstance(formula, Implies):
        return Or(Not(normalize(formula.left)), normalize(formula.right))

    if isinstance(formula, And):
        return And(normalize(formula.left), normalize(formula.right))

    if isinstance(formula, Or):
        return Or(normalize(formula.left), normalize(formula.right))

    if isinstance(formula, Not):
        return Not(normalize(formula.operand))

    if isinstance(formula, BinaryTemporal):
        return BinaryTemporal(formula.op, normalize(formula.left), normalize(formula.right))

    return formula


def collect_atoms(formula: Formula) -> set[str]:
    """All distinct atom names occurring in ``formula``."""
    return {atom.name for atom in formula.atoms()}


def to_nnf(formula: Formula, negated: bool = False) -> Formula:
    """Push negations down to the atoms (negation normal form).

    This is what makes the tableau tractable: a state never has to reason about
    ``Not(And(a, b))``. It also converts negated temporal operators into their positive
    duals, which is the step that lets ``!G(p)`` and ``!F(p)`` mean what a policy author
    expects rather than silently behaving as an unconstrained atom.
    """
    if isinstance(formula, Atom):
        return Not(formula) if negated else formula

    if isinstance(formula, Not):
        return to_nnf(formula.operand, not negated)

    if isinstance(formula, And):
        if negated:
            return Or(to_nnf(formula.left, True), to_nnf(formula.right, True))
        return And(to_nnf(formula.left), to_nnf(formula.right))

    if isinstance(formula, Or):
        if negated:
            return And(to_nnf(formula.left, True), to_nnf(formula.right, True))
        return Or(to_nnf(formula.left), to_nnf(formula.right))

    if isinstance(formula, Implies):
        return to_nnf(Or(Not(formula.left), formula.right), negated)

    if isinstance(formula, Temporal):
        dual = {
            TemporalOp.ALWAYS: TemporalOp.EVENTUALLY,
            TemporalOp.EVENTUALLY: TemporalOp.ALWAYS,
            TemporalOp.NEXT: TemporalOp.NEXT,
        }[formula.op]
        op = dual if negated else formula.op
        return Temporal(op, to_nnf(formula.operand, negated))

    if isinstance(formula, BinaryTemporal):
        dual_op = {
            TemporalOp.UNTIL: TemporalOp.RELEASE,
            TemporalOp.RELEASE: TemporalOp.UNTIL,
            TemporalOp.STRONG_UNTIL: TemporalOp.RELEASE,
        }[formula.op]
        op = dual_op if negated else formula.op
        return BinaryTemporal(op, to_nnf(formula.left, negated), to_nnf(formula.right, negated))

    raise TypeError(f"cannot normalise {type(formula).__name__}")
