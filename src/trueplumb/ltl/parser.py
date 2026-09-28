"""Recursive-descent parser for the TruePlumb LTL policy DSL.

Hand-written on purpose. The obvious shortcut -- mapping operator names to Python callables
and calling ``eval()`` -- would be a remote code execution hole in a security tool that
parses policy files, and it would make "no eval in the verification path" untrue.

Grammar (lowest precedence first)::

    formula   := implies
    implies   := until_expr ( '->' until_expr )?
    until_expr:= release_expr ( ('U'|'W') release_expr )*
    release   := strong_next ( 'R' strong_next )*
    strong_next:= unary ( 'W' unary )*
    unary     := '!' unary | 'G' unary | 'F' unary | 'X' unary
               | '(' formula ')' | ATOM
    ATOM      := [A-Za-z_][A-Za-z0-9_:.]*

Supported aliases so policies read naturally: ``always``/``globally``/``G``/``[]``,
``eventually``/``finally``/``sometime``/``F``/``<>``, ``next``/``X``, ``until``/``U``,
``release``/``R``, ``strong_until``/``W``.
"""

from __future__ import annotations

import re

from ..ltl.ast import (
    And,
    Atom,
    BinaryTemporal,
    Formula,
    Implies,
    Not,
    Or,
    Temporal,
    TemporalOp,
    normalize,
)

_TOKEN_RE = re.compile(
    r"""
    \s*(?:
        (?P<string>"[^"]*"|'[^']*')
      | (?P<lbrack>\[\s*\])
      | (?P<langle><>)
      | (?P<lparen>\()
      | (?P<rparen>\))
      | (?P<not>!)
      | (?P<and>&&)
      | (?P<or>\|\|)
      | (?P<implies>->)
      | (?P<ident>[A-Za-z_][A-Za-z0-9_:.]*)
    )
    """,
    re.VERBOSE,
)

# Single-letter operator aliases. Uppercase only: a lowercase "x", "g" or "f" is an
# ordinary atom name, not an operator. Accepting lowercase made `G(x)` unparseable
# because "x" was consumed as the next-step operator -- a policy DSL where `x` is
# unusable is worse than one that demands `next(x)`.
_ALWAYS = {"always", "globally", "all", "throughout", "G"}
_EVENTUALLY = {"eventually", "finally", "sometime", "event", "reach", "F"}
_NEXT = {"next", "X"}
_UNTIL = {"until", "U"}
_RELEASE = {"release", "unless", "R"}
_STRONG_UNTIL = {"strong_until", "strictly_until", "W"}
_NOT = {"not"}


class ParseError(ValueError):
    """Raised with a human-readable message and source position."""


class _Token:
    __slots__ = ("kind", "value", "pos")

    def __init__(self, kind: str, value: str, pos: int) -> None:
        self.kind = kind
        self.value = value
        self.pos = pos

    def __repr__(self) -> str:  # pragma: no cover
        return f"Token({self.kind!r}, {self.value!r}, {self.pos})"


def tokenize(source: str) -> list[_Token]:
    tokens: list[_Token] = []
    pos = 0
    length = len(source)
    while pos < length:
        if source[pos].isspace():
            pos += 1
            continue
        match = _TOKEN_RE.match(source, pos)
        if not match or match.end() == match.start():
            raise ParseError(f"unexpected character {source[pos]!r} at position {pos}")
        kind = match.lastgroup or ""
        value = match.group()
        if kind == "lbrack":
            kind, value = "always_lit", "[]"
        elif kind == "langle":
            kind, value = "eventually_lit", "<>"
        elif kind == "ident":
            value = match.group("ident")
        else:
            value = value.strip()
        tokens.append(_Token(kind, value, pos))
        pos = match.end()
    tokens.append(_Token("eof", "", length))
    return tokens


class Parser:
    def __init__(self, source: str) -> None:
        self._tokens = tokenize(source)
        self._i = 0

    @property
    def _cur(self) -> _Token:
        return self._tokens[self._i]

    def _advance(self) -> _Token:
        token = self._tokens[self._i]
        self._i += 1
        return token

    def _expect(self, kind: str, what: str) -> _Token:
        if self._cur.kind != kind:
            raise ParseError(
                f"expected {what} at position {self._cur.pos}, found {self._cur.value!r}"
            )
        return self._advance()

    # formula := implies
    def parse(self) -> Formula:
        formula = self._parse_implies()
        if self._cur.kind != "eof":
            raise ParseError(
                f"unexpected trailing input at position {self._cur.pos}: {self._cur.value!r}"
            )
        return normalize(formula)

    def _parse_implies(self) -> Formula:
        left = self._parse_or()
        if self._cur.kind == "implies":
            self._advance()
            right = self._parse_or()
            return Implies(left, right)
        return left

    def _parse_or(self) -> Formula:
        left = self._parse_and()
        while self._cur.kind == "or":
            self._advance()
            right = self._parse_and()
            left = Or(left, right)
        return left

    def _parse_and(self) -> Formula:
        left = self._parse_release()
        while self._cur.kind == "and":
            self._advance()
            right = self._parse_release()
            left = And(left, right)
        return left

    def _parse_release(self) -> Formula:
        left = self._parse_until()
        while self._cur.kind == "ident" and self._cur.value in _RELEASE:
            self._advance()
            right = self._parse_until()
            left = BinaryTemporal(TemporalOp.RELEASE, left, right)
        return left

    def _parse_until(self) -> Formula:
        left = self._parse_unary()
        while self._cur.kind == "ident" and self._cur.value in _UNTIL:
            self._advance()
            right = self._parse_unary()
            left = BinaryTemporal(TemporalOp.UNTIL, left, right)
        return left

    def _parse_unary(self) -> Formula:
        token = self._cur

        # Alias sets hold both spellings, so match exactly -- no case folding. Folding
        # would make lowercase "x" match _NEXT and break `G(x)`, and lowercase "f" match
        # _EVENTUALLY and break the atom `f`.
        if token.kind == "not" or (token.kind == "ident" and token.value in _NOT):
            self._advance()
            return Not(self._parse_unary())

        if token.kind == "always_lit" or (token.kind == "ident" and token.value in _ALWAYS):
            self._advance()
            return Temporal(TemporalOp.ALWAYS, self._parse_unary())

        if token.kind == "eventually_lit" or (token.kind == "ident" and token.value in _EVENTUALLY):
            self._advance()
            return Temporal(TemporalOp.EVENTUALLY, self._parse_unary())

        if token.kind == "ident" and token.value in _NEXT:
            self._advance()
            return Temporal(TemporalOp.NEXT, self._parse_unary())

        if token.kind == "ident" and token.value in _STRONG_UNTIL:
            self._advance()
            return BinaryTemporal(TemporalOp.STRONG_UNTIL, self._parse_unary(), self._parse_unary())

        if token.kind == "lparen":
            self._advance()
            # A parenthesised group is a full formula, so parse it at the top of the
            # precedence chain -- routing this to _parse_unary() would silently reject
            # "(a && b)" while accepting "(a)", which is exactly the kind of inconsistency
            # that makes a policy language untrustworthy.
            inner = self._parse_implies()
            self._expect("rparen", "')'")
            return inner

        if token.kind in ("ident", "string"):
            self._advance()
            return Atom(token.value)

        raise ParseError(f"unexpected token {token.value!r} at position {token.pos}")


def parse(source: str) -> Formula:
    """Parse a policy expression. Raises :class:`ParseError` on invalid input."""
    if not source or not source.strip():
        raise ParseError("empty policy expression")
    return Parser(source).parse()
