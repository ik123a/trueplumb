"""Command line interface for TruePlumb.

The CLI is deliberately thin. All judgement lives in the library; this layer only reads a
policy, reads a trace, and prints a verdict. That split matters for a tool whose entire
value is that you can trust its output -- the moment verdicts start being computed in
presentation code there are two implementations to keep in sync, and only one of them is
covered by the differential harness.

Exit codes are part of the interface:

    0  policy held
    1  policy violated
    2  bad input (unreadable trace, unparseable policy)

A verifier is only useful in CI if a violation fails the build, and that has to be the
default rather than a flag nobody remembers to pass.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from .ltl.ast import to_nnf
from .ltl.automata import DFA, to_dfa
from .ltl.parser import ParseError, parse
from .ltl.trace import CheckResult, TraceEvent, check, load_trace

app = typer.Typer(
    name="trueplumb",
    help="Deterministic conformance checking for AI agent safety policies.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)


def _fail(message: str) -> typer.Exit:
    """Build a usage error, to be raised by the caller.

    Nothing here is a policy verdict, so it goes to stderr under a distinct exit code. A
    wrapper script that cannot tell "your file was malformed" apart from "the agent broke
    policy" will eventually treat one as the other.
    """
    err_console.print(f"[bold red]error:[/] {message}")
    return typer.Exit(code=2)


def _read_trace(path: Path) -> list[TraceEvent]:
    if not path.is_file():
        raise _fail(f"no such trace file: {path}")

    if path.suffix == ".jsonl":
        try:
            return load_trace(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise _fail(f"could not read trace {path}: {exc}") from exc

    # A bare .json list is accepted too: it is what you paste into an issue, whereas the
    # JSONL form is what an agent harness appends to as it runs.
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _fail(f"could not read trace {path}: {exc}") from exc

    raw = decoded if isinstance(decoded, list) else decoded.get("events", [])
    if not isinstance(raw, list):
        raise _fail(f"{path} must be a list of events, or an object with an 'events' list")

    events: list[TraceEvent] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise _fail(f"event {i} of {path} is not an object")
        try:
            events.append(TraceEvent.from_dict(item, i))
        except ValueError as exc:
            raise _fail(str(exc)) from exc
    return events


def _compile(policy: str) -> DFA:
    """Parse and compile a policy, turning a parse error into a usage error.

    Compiling up front rather than letting `check` do it means an unparseable policy fails
    before the trace is even read -- you get the policy error, not a confusing downstream
    one -- and it leaves the monitor ready to be reused across a whole corpus.
    """
    try:
        return to_dfa(parse(policy), formula_text=policy)
    except ParseError as exc:
        raise _fail(f"invalid policy {policy!r}: {exc}") from exc


def _render(result: CheckResult) -> None:
    if result.compliant:
        console.print(
            f"[bold green]COMPLIANT[/] {result.policy} held for all {result.total_steps} steps."
        )
    else:
        console.print(
            f"[bold red]VIOLATION[/] {result.policy} first fails at step {result.violation_index}."
        )
        table = Table(
            title=f"counterexample ({len(result.counterexample)} step(s))",
            header_style="bold",
        )
        table.add_column("step", justify="right")
        table.add_column("atoms")
        for event in result.counterexample:
            table.add_row(str(event.index), ", ".join(sorted(event.atoms)) or "[dim]-[/]")
        console.print(table)

    console.print(
        f"[dim]states visited: {result.states_visited}  monitor: {result.monitor_stats}[/]"
    )


@app.command()
def verify(
    policy: Annotated[str, typer.Argument(help="Temporal policy in the TruePlumb DSL.")],
    trace: Annotated[Path, typer.Argument(help="Trace file (.json or .jsonl).")],
    json_out: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Check a trace against a policy. Exits 1 if the policy was violated."""
    monitor = _compile(policy)
    events = _read_trace(trace)
    result = check(policy, events, monitor=monitor)

    if json_out:
        console.print_json(json.dumps(result.to_dict(), indent=2))
    else:
        _render(result)

    if not result.compliant:
        raise typer.Exit(code=1)


@app.command()
def atoms(
    trace: Annotated[Path, typer.Argument(help="Trace file (.json or .jsonl).")],
) -> None:
    """List the atoms a trace mentions, to help write a policy against it.

    The first thing anyone does after reading a violation is stare at the trace wondering
    what to call the thing the agent did. This is that answer, and it costs one read.
    """
    events = _read_trace(trace)
    names = sorted({atom for event in events for atom in event.atoms})
    if not names:
        console.print("[yellow]this trace contains no atoms[/]")
        return

    table = Table(title=f"{len(names)} atom(s) in {trace.name}")
    table.add_column("atom", style="cyan")
    table.add_column("steps", justify="right")
    for name in names:
        steps = ", ".join(str(e.index) for e in events if name in e.atoms)
        table.add_row(name, steps)
    console.print(table)


@app.command()
def explain(
    policy: Annotated[str, typer.Argument(help="Temporal policy to explain.")],
) -> None:
    """Show how a policy parsed, plus what the monitor will cost to run.

    Parsing failures are the least legible part of any DSL. Seeing the normalised form
    first turns "unexpected token )" into "oh, my parentheses bind differently". The state
    count matters too, because a policy whose monitor explodes is unusable in a CI loop,
    and finding that out here is much better than finding it on a 4,000-step trace.
    """
    try:
        parsed = parse(policy)
    except ParseError as exc:
        raise _fail(f"invalid policy {policy!r}: {exc}") from exc

    monitor = to_dfa(parsed, formula_text=policy)
    stats = monitor.build_stats

    console.print(f"[bold]policy[/]   {policy}")
    console.print(f"[bold]parsed[/]   {parsed}")
    console.print(f"[bold]nnf[/]      {to_nnf(parsed)}")
    console.print(f"[bold]atoms[/]    {', '.join(sorted(monitor.alphabet)) or '(none)'}")
    console.print(
        f"[bold]states[/]   {stats['minimized_states']} "
        f"(from {stats['formula_states']} formula states, {stats['events']} events)"
    )


def main() -> None:
    """Console-script entry point.

    Deliberately does not catch exceptions. Typer exits on its own error paths, and
    swallowing anything else would turn a traceback into a silent wrong answer -- the one
    failure mode this project exists to prevent.
    """
    try:
        app()
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        err_console.print("[yellow]interrupted[/]")
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
