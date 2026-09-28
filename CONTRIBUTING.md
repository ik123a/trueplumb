# Contributing to TruePlumb

Thanks for looking. This project is small and the bar for correctness is high, so this
guide is mostly about what "high" means here.

---

## The one rule

> **If a change alters a verdict, it needs a differential run and a named test.**

This is a tool people use to decide whether something is safe. A wrong verdict is worse
than no tool, because it is trusted. Before opening a PR that touches `ltl/`, `cli.py`, or
the trace loader:

```bash
pytest                                     # 107 tests
python scripts/differential_test.py 4      # 6,540 traces, no disagreements allowed
```

If either is not green, the change is not ready. A PR with a failing differential run and
a note explaining why the new behaviour is correct is fine; a PR with a failing
differential run and no note is not.

---

## Getting set up

```bash
git clone https://github.com/ik123a/trueplumb
cd trueplumb
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pre-commit install        # optional but recommended
```

Python 3.11+. CI runs 3.11, 3.12, and 3.13.

---

## What lives where

| Path | What goes in it |
|---|---|
| `src/trueplumb/ltl/` | The verification engine. Changes here need the most care. |
| `src/trueplumb/cli.py` | Argument parsing and rendering. **No verdicts computed here.** |
| `scripts/differential_test.py` | The reference semantics. Treat as ground truth. |
| `tests/` | Hand-written tests. Document *why* a rule exists. |
| `examples/` | Small, readable traces used by the README. |
| `docs/` | Methodology and architecture. |

---

## Writing a test

Hand-written tests document **why** a rule exists, not just that it holds. The comment is
the point.

```python
def test_stalled_run_cannot_be_laundered_into_compliant(self) -> None:
    # The product-level guarantee: a run that never finishes is NOT compliant.
    # This is the single most important case in the file -- an agent that stalls
    # halfway through a policy must not be able to report a pass.
    assert not check("F(done)", ev(["work"], ["work"], ["work"])).compliant
```

A test with no comment will be deleted or will be changed to match a bug in six months.
Both outcomes are worse than the effort of writing the sentence now.

### If you find a bug

Add a test named for the bug, and say in the comment what the wrong answer was. Every
semantics bug found so far is pinned this way in
`tests/test_end_of_trace_semantics.py`. That file is the most useful documentation in the
repository precisely because it records the mistakes.

---

## Style

Enforced by `ruff` and `mypy --strict`; both run in CI.

```bash
ruff check .
ruff format .
mypy
```

- Type annotations everywhere. `mypy --strict` is not negotiable on `src/`.
- Comments explain **why**, not what. The code already says what.
- Line length 100.
- Docstrings on public functions, one line where possible, and stating the guarantee
  rather than restating the signature.

### Do not add dependencies to the core

The engine imports only the standard library. There is a test asserting it never imports
`openai`, `anthropic`, `requests`, `httpx`, `torch`, `socket`, or `urllib`, and that
assertion is load-bearing: the determinism claim is what the whole project rests on, and a
transitive network or model dependency would quietly break it.

A heavy dependency is acceptable behind an extra in `pyproject.toml` (`stats` already
exists for scipy), never in the verified path.

---

## Things that are easy to get wrong

Each of these was a real bug here. If your change touches them, read
`docs/METHODOLOGY.md` §4 first.

- **End-of-trace semantics.** Operators do not agree on what an absent future means.
  `G` is vacuously true; `F` recurses; `U` rejects; `R` is vacuously true. `F` and `U`
  differ, and that asymmetry is deliberate.
- **Subformula elimination.** Never drop a term merely because it appears as a
  subformula of a surviving loop. It erases live obligations and reports violations as
  compliant. Only the loop identities in `_absorbed` are sound.
- **Minimization equivalence.** The residual is part of the equivalence key. Merging two
  states because they agree on transitions can discard a live obligation.
- **Atom coercion.** Never `str(a)` over a presumed-iterable `atoms`. A string
  `"dangerous"` becomes nine single-character atoms and produces a confident wrong verdict.
- **Lowercase operator aliases.** `g`/`f`/`x` must stay usable as atom names.

---

## Reporting a bug

Open an issue with:

- the policy, verbatim
- the trace, as JSONL (this is most of it — `trueplumb atoms <trace>` will help)
- what you expected, and what happened
- Python version and OS

A failing policy with a surprising result is **not** a bug report — it is probably correct.
If the semantics surprise you, that is a documentation gap; please open an issue anyway,
because it means the docs are unclear and someone else will hit the same wall.

---

## What would help most

The verification core is in reasonable shape. The **attack corpus** is the bottleneck, and
it needs domain knowledge more than it needs code.

If you know how prompt injection, tool poisoning, memory poisoning, or agent authorization
attacks actually work, the most valuable contribution is a set of traces with expected
verdicts. See the "not built yet" table in `docs/ARCHITECTURE.md` §8.
