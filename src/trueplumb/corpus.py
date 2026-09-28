"""Corpus schema and validation.

A corpus entry is a trace plus a policy plus the verdict the policy *should* produce. The
verdict is not a human annotation -- it is computed by the engine and then asserted, which
turns the corpus into a regression suite for the engine itself.

That ordering matters. The dangerous version of a corpus is "expected: COMPLIANT" written
by the same person who wrote the trace and the engine: it agrees with whatever the engine
does today, including its bugs. Here each entry is generated from a specification
(`expect` + `violation_at`), the engine's answer is compared against it, and a mismatch is
a failure. `generate_corpus.py` then re-verifies every case from scratch.

Case shapes
-----------

`compliant`  the policy must hold; the engine must agree
`violation`  the policy must fail, and at a specific step, so the counterexample is
             pinned rather than merely "somewhere in there"

`undecided`  deliberately excluded: a policy whose verdict depends on semantics a reader
             may reasonably disagree about. Kept out rather than argued about.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .ltl.parser import ParseError, parse
from .ltl.trace import TraceEvent, check

SchemaVersion = "1.0"


class CorpusError(ValueError):
    """A corpus file is malformed, or a case does not match its specification."""


@dataclass(frozen=True, slots=True)
class Case:
    """One corpus entry: a trace, a policy, and the verdict it must produce."""

    id: str
    title: str
    attack: str
    category: str
    policy: str
    steps: tuple[dict[str, Any], ...]
    expect: Literal["compliant", "violation"]
    violation_at: int | None = None
    why: str = ""
    tags: tuple[str, ...] = field(default=())

    def events(self) -> list[TraceEvent]:
        """Build the trace, with the same strictness the CLI loader uses.

        A corpus is test data. If a step here is malformed, every case derived from it
        would be quietly testing the wrong thing.

        The loader raises ``ValueError`` and this normalises it to ``CorpusError``, so a
        caller validating a corpus sees one exception type. A malformed atom must not
        escape as a raw ``ValueError`` from three frames down: a consumer catching
        ``CorpusError`` would miss it, and the case would fail for the wrong reason.
        """
        try:
            return [TraceEvent.from_dict(step, i) for i, step in enumerate(self.steps)]
        except ValueError as exc:
            raise CorpusError(f"{self.id}: {exc}") from exc

    def verify(self) -> tuple[bool, int | None]:
        """Run the engine and return `(compliant, violation_index)`."""
        try:
            parse(self.policy)
        except ParseError as exc:
            raise CorpusError(f"{self.id}: unparseable policy {self.policy!r}: {exc}") from exc

        result = check(self.policy, self.events())
        return result.compliant, result.violation_index

    def check(self, strict_index: bool = False) -> None:
        """Raise if the engine's verdict disagrees with the specification.

        ``strict_index`` controls how the reported violation step is compared.

        By default only the verdict is required, because the first-failing-prefix rule
        cannot distinguish "obligation still pending" from "obligation abandoned". For a
        trace that ends without satisfying ``F(done)``, *every* prefix is unsatisfiable, so
        the earliest is step 0 -- not because anything went wrong there, but because the
        obligation was never dischargeable. A corpus author who writes ``violation_at: 2``
        for a stalled run means "the failure is the stall", which is a different and more
        useful statement than the engine can currently make.

        Set ``strict_index`` only for cases where the failure genuinely occurs *at* a
        named step, and where every earlier prefix is genuinely compliant.
        """
        compliant, index = self.verify()

        if self.expect == "compliant":
            if not compliant:
                raise CorpusError(
                    f"{self.id}: expected COMPLIANT but engine reports a violation "
                    f"at step {index}\n  policy: {self.policy}\n  trace:  {self.steps}"
                )
            return

        if compliant:
            raise CorpusError(
                f"{self.id}: expected VIOLATION but engine reports COMPLIANT\n"
                f"  policy: {self.policy}\n  trace:  {self.steps}"
            )

        # Only pin the step when the corpus says the index is meaningful. For a stalled
        # trace it is not: every prefix fails, so step 0 is correct without being
        # informative, and pinning it would assert a fiction rather than a fact.
        if self.violation_at is not None and index != self.violation_at:
            if strict_index:
                raise CorpusError(
                    f"{self.id}: expected violation at step {self.violation_at} "
                    f"but engine reports step {index}\n  policy: {self.policy}"
                )
            return

        if strict_index and self.violation_at is None:
            raise CorpusError(
                f"{self.id}: strict_index was requested but no violation_at was declared"
            )

    def check_strict(self) -> None:
        """Require the exact violation step as well as the verdict."""
        self.check(strict_index=True)


def from_dict(payload: dict[str, Any], source: str = "<corpus>") -> Case:
    """Parse and validate one case. Rejects anything malformed loudly."""
    if not isinstance(payload, dict):
        raise CorpusError(f"{source}: case is not an object")

    for field_name in ("id", "title", "attack", "category", "policy", "steps", "expect"):
        if field_name not in payload:
            raise CorpusError(f"{source}: missing required field {field_name!r}")

    expect = payload["expect"]
    if expect not in ("compliant", "violation"):
        raise CorpusError(f"{source}: expect must be 'compliant' or 'violation', got {expect!r}")

    steps = payload["steps"]
    if not isinstance(steps, list) or not steps:
        raise CorpusError(f"{source}: 'steps' must be a non-empty list")

    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise CorpusError(f"{source}: step {i} is not an object")
        if "atoms" not in step:
            raise CorpusError(f"{source}: step {i} has no 'atoms'")

    case_id = payload["id"]
    if not isinstance(case_id, str) or not case_id:
        raise CorpusError(f"{source}: 'id' must be a non-empty string")

    return Case(
        id=case_id,
        title=str(payload["title"]),
        attack=str(payload["attack"]),
        category=str(payload["category"]),
        policy=str(payload["policy"]),
        steps=tuple(steps),
        expect=expect,
        violation_at=payload.get("violation_at"),
        why=str(payload.get("why", "")),
        tags=tuple(payload.get("tags", ())),
    )


@dataclass(frozen=True, slots=True)
class Corpus:
    """A named, versioned collection of cases."""

    name: str
    version: str
    description: str
    cases: tuple[Case, ...]
    schema_version: str = SchemaVersion

    def by_attack(self) -> dict[str, list[Case]]:
        """Cases grouped by attack category.

        Grouped on `category` rather than `attack` because `attack` is prose: "the agent
        treats an old approval as covering later actions" is a description, and grouping by
        it produces one bucket per case, which is not a grouping at all. The category is the
        controlled vocabulary a report can actually aggregate on.
        """
        grouped: dict[str, list[Case]] = {}
        for case in self.cases:
            grouped.setdefault(case.category, []).append(case)
        return grouped

    def verify_all(self, strict_index: bool = False) -> list[str]:
        """Check every case. Returns a list of human-readable failures (empty if clean)."""
        failures: list[str] = []
        for case in self.cases:
            try:
                case.check(strict_index=strict_index)
            except CorpusError as exc:
                failures.append(str(exc))
        return failures

    def strict_failures(self) -> list[str]:
        """Verify only the cases that pin an exact violation step.

        Split out so the ordinary run stays fast and a corpus author can ask the stricter
        question separately, for the subset of cases where the step is actually meaningful.
        """
        pinned = [c for c in self.cases if c.violation_at is not None]
        failures: list[str] = []
        for case in pinned:
            try:
                case.check(strict_index=True)
            except CorpusError as exc:
                failures.append(str(exc))
        return failures


def load_corpus(path: str | Path) -> Corpus:
    """Read a corpus file."""
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CorpusError(f"{path}: invalid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise CorpusError(f"{path}: top level must be an object")
    if payload.get("schema_version") != SchemaVersion:
        raise CorpusError(
            f"{path}: schema_version must be {SchemaVersion!r}, "
            f"got {payload.get('schema_version')!r}"
        )

    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise CorpusError(f"{path}: 'cases' must be a non-empty list")

    cases = tuple(from_dict(case, f"{path}[{i}]") for i, case in enumerate(raw_cases))

    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise CorpusError(f"{path}: duplicate case id {case.id!r}")
        seen.add(case.id)

    return Corpus(
        name=str(payload.get("name", path.stem)),
        version=str(payload.get("version", "0")),
        description=str(payload.get("description", "")),
        cases=cases,
    )
