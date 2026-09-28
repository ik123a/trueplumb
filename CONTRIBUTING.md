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

## Writing a control adapter

The single most useful code contribution. The interface is three attributes and one method:

```python
from trueplumb import ControlAdapter, Decision, StepOutcome


class MyGuardrail(ControlAdapter):
    name = "my-guardrail"
    version = "2.3.1"  # required. a claim without a version is not reproducible

    def evaluate(self, case) -> list[StepOutcome]:
        return [
            StepOutcome(case_id=case.id, step=i, decision=decision_for(step))
            for i, step in enumerate(case.steps)
        ]
```

Then: `trueplumb score corpus/ --adapter my-guardrail`. To register it for the CLI, add it
to the `registry` dict in `score()` in `src/trueplumb/cli.py`.

### Four rules

1. **Record decisions, never scores.** If your control emits a confidence number, put it in
   `StepOutcome.evidence` and let TruePlumb do the arithmetic. A vendor's own score is
   unverifiable, and checking vendor claims is the premise of the project.
2. **Return an outcome for every step.** Steps your control never saw must be
   `Decision.NOT_REACHED`, not `ALLOWED`. A missing entry is treated as `NOT_REACHED` by
   `ControlRun`, but returning it explicitly makes partial runs visible instead of
   relying on that default.
3. **Do not execute anything inside the adapter.** A guardrail is arbitrary third-party
   code and the sandbox is the caller's decision. Keep the adapter a thin translation layer.
4. **Write a test that it is beatable and one that it is not useless.** A control with no
   `block-everything`-style comparison tells nobody anything. Assert where your control
   fails — every control has a blind spot, and naming it is more valuable than hiding it.

### How the shipped adapter does it

`presidio.py` is the worked example. Read it before writing yours, and copy the shape:

- **Nothing is imported from the product.** `tests/test_presidio.py` walks `presidio.py`'s
  AST and asserts `presidio_analyzer` and `spacy` are absent — by AST rather than by
  substring, because the module's docstring names the import in order to explain its
  absence. `scripts/record_presidio.py` is the only file that touches the product, and it
  runs wherever the caller chooses.
- **The vendor log is the interface.** A JSON file records what the product found: entity
  types, spans, recognizer names, and the vendor's confidence. It carries **no timestamp**,
  so re-running the harness on the same corpus produces an identical file and a diff means
  the product's behaviour changed. `tests/test_presidio.py` asserts that no date-shaped key
  exists anywhere in it.
- **The score is evidence, never input.** `StepOutcome.evidence["vendor_scores"]` holds the
  vendor's confidences verbatim. The test that matters rewrites every score in the log to
  `1.0 - score` and asserts that no verdict moves.
- **Two `NOT_REACHED` reasons.** A halt and a missing log entry both mean "not presented",
  but they mean different things operationally — the control worked, or your harness broke.
  Give them different `reason` strings.
- **A bad log is a usage error, not a crash.** A missing log, or a log for the other
  rendering, should exit 2 like any other bad input.

```bash
# Run the product yourself, in a sandbox of your choosing, then score the result.
python -m venv .presidio-sandbox
.presidio-sandbox/bin/pip install presidio-analyzer==2.2.364
.presidio-sandbox/bin/python -m spacy download en_core_web_sm
.presidio-sandbox/bin/python scripts/record_presidio.py \
    --corpus corpus/ --out measurements/presidio-log.json --rendering atom

trueplumb score corpus/ --adapter presidio
```

### What a good adapter PR includes

- The adapter, plus a note on which product and version it wraps.
- A short list of what it **cannot** catch, if you found any.
- Confirmation that the product's own scoring is not being trusted anywhere in the path.
- The committed decision log, if you are willing to publish it, so your numbers are
  reviewable rather than asserted.

---

## Adding a corpus case

The corpus is the most useful thing you can contribute, and the bar is **justifying the
expected verdict**, not producing a trace that trips a rule.

1. Add a case to `corpus/agent_safety_baseline.json` (or a new file in `corpus/`).
2. Run `python scripts/verify_corpus.py corpus/`.
3. If it fails, work out **which side is wrong** before changing anything. A mismatch is
   either a real engine bug or a wrong expectation, and both are worth catching — so do not
   simply copy what the engine said and move on. That is the failure mode this whole
   mechanism exists to prevent.

### Required fields

| Field | Meaning |
|---|---|
| `id` | Unique, kebab-case, prefixed by area (`refund-001-…`) |
| `title` | One line, human readable |
| `attack` | What the agent did, in prose |
| `category` | Controlled vocabulary — see the list below |
| `policy` | The temporal policy under test |
| `expect` | `compliant` or `violation` |
| `violation_at` | Optional. Only when the failure genuinely occurs *at* a step |
| `why` | **Required in practice.** Why this verdict is correct |
| `steps` | The trace, as `{atoms: [...]}` objects |

Categories: `baseline`, `approval`, `authorization`, `prompt-injection`,
`memory-poisoning`, `data-exfiltration`, `privilege-escalation`, `resource-exhaustion`,
`termination`.

### Do not pin `violation_at` for abandoned obligations

If the trace ends without discharging an `F`, **every** prefix is unsatisfiable, so the
first-failing-prefix rule reports step 0. That is correct but says nothing useful. The
failure is the stall, not a step, so leave `violation_at` off. Pin it only when the
obligation was live and then broken at a named position.

### Every attack category needs a compliant counterpart

A category containing only violations cannot distinguish "the control caught it" from "the
policy rejects everything", so a corpus of only such cases measures nothing. If you add a
violation, add the near-miss that should pass — a control must not be able to score well by
blocking everything. There is a test for this.

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
