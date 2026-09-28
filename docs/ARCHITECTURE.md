# Architecture

How the verification core is put together, and why each piece is the way it is.

The **verification core**, the **corpus format**, and the **measurement layer** exist and
are tested. What is missing is the part that makes it matter — adapters for real products,
a corpus large enough to rank them, and signed reports. This document describes what is
real and marks the rest explicitly.

---

## 1. The whole system

```
                    policy string
                         |
                         v
                  +--------------+
                  |    parser    |   recursive descent, no eval()
                  +--------------+
                         |
                         v
                  +--------------+
                  |  to_nnf()    |   negations pushed to atoms
                  +--------------+
                         |
                         v
                  +--------------+
                  |  to_dfa()    |   formula derivatives -> monitor
                  |              |   + Moore minimization
                  +--------------+
                         |
                         v
                  +--------------+
                  |    check()   |   step the monitor over a trace
                  +--------------+
                         |
                         v
              +--------------------+
              |   CheckResult      |   compliant / violation_index
              |                    |   counterexample / monitor_stats
              +--------------------+
```

Everything above is pure, deterministic, offline, and free of LLM calls.

The measurement layer sits above it and adds nothing to the verified path:

```
                    corpus/*.json
                          |
                          v
                  +--------------+
                  |  load_corpus |   schema validation, expected verdicts
                  +--------------+
                          |
          +---------------+---------------+
          |                               |
          v                               v
  +---------------+              +----------------+
  | ControlAdapter|              |    check()     |
  |  (yours)      |              |  (core, above) |
  +---------------+              +----------------+
          |  StepOutcome per step        |  CheckResult
          +---------------+---------------+
                          v
                  +--------------+
                  | score_control |  detection + false-positive Wilson intervals
                  +--------------+
                          |
                          v
                  +--------------+
                  |compare_controls|  exact McNemar, paired
                  +--------------+
```

Three properties hold across that second diagram as well:

- **The core never learns about controls.** `check()` knows nothing about adapters, and
  adapter code never touches the monitor. A guardrail bug cannot reach the verdict path.
- **Adapters never execute anything.** A guardrail is arbitrary third-party code; running it
  is the caller's job, in a sandbox they choose. TruePlumb defines the interface and does
  the arithmetic, and stays out of the execution path.
- **Adapters record decisions, never scores.** A control returning "confidence 0.97" is not
  trusted for it. A vendor's own number is unverifiable, and checking vendor claims is the
  premise of the project. The report is computed here from counts.

---

## 2. Module layout

| File | Responsibility |
|---|---|
| `ltl/parser.py` | Tokenizer + recursive-descent parser. No `eval()` anywhere. |
| `ltl/ast.py` | Formula nodes, `to_nnf`, structural helpers. |
| `ltl/automata.py` | `simplify`, `deriv`, `accepts`, `to_dfa`, `_minimize`. The engine. |
| `ltl/trace.py` | `TraceEvent`, `check`, counterexample extraction. |
| `cli.py` | Argument parsing and rendering only. No verdicts computed here. |
| `scripts/differential_test.py` | Independent reference semantics + exhaustive comparison. |

`cli.py` computes no verdicts deliberately. The moment presentation code can produce a
verdict there are two implementations to keep in sync and only one of them is covered by
the differential harness.

---

## 3. Why derivatives rather than a tableau

The first implementation translated LTL to an alternating automaton and resolved it into
a DFA. It was replaced.

**Why it failed:** the closure and expansion rules produced degenerate automata — a formula
requiring several Boolean states yielded one. Getting Büchi emptiness right is subtle, and
the failure mode is silent: the automaton is still well-formed, still terminates, and just
gives the wrong answer.

**What replaced it:** derivatives. Each state is an *outstanding obligation* — a formula
that still has to be discharged. Consuming an event rewrites the obligation. This is:

- far easier to test, because a state is a readable formula rather than a set index
- directly aligned with how the semantics is actually defined
- self-limiting, because the residual is normalized at every step

The tradeoff is honest and worth stating: derivative construction can produce more states
than a well-tuned tableau. For the policy sizes this project targets — a handful of atoms
over a few dozen steps — that cost is irrelevant, and `explain` reports the state count so
the question stays visible.

### Normalization is where soundness lives

`simplify()` folds constants, removes double negation, and applies loop identities:

```
G(p) & p   ==  G(p)        F(p) | p   ==  F(p)
(p R q) & p == (p R q)     (p U q) | p == (p U q)
```

Only these. An earlier version dropped any term appearing as a **subformula** of a
surviving loop, which is unsound: it rewrites `F(done) & G(!dangerous)` to `G(!dangerous)`
by erasing the live obligation, and reports a violating run as compliant.

Soundness is not negotiable in a compliance tool, so the broader rule was removed even
though it reduced state counts. The reason is recorded in the code so it is not
reintroduced by someone optimising later.

---

## 4. The monitor

```python
@dataclass(frozen=True, slots=True)
class DFA:
    start: int
    accepting: frozenset[int]
    transitions: dict[int, dict[frozenset[str], int]]
    states: frozenset[int]
    alphabet: frozenset[str]
    formula_text: str
    sink: int
    residuals: dict[int, Formula]  # the live obligation per state
    build_stats: dict[str, int]
```

### Residuals, not booleans

Each state stores a **formula**, not an accept/reject flag.

Acceptance depends on vacuity over an absent future, and no construction-time boolean can
capture that. `G(a)` is satisfied on a trace ending in `a` because the residual obligation
is about the future and the future is empty — a property of the residual plus an empty
suffix, not of the state alone.

`accepting` is retained as a static over-approximation of the safe states, useful for
pruning and for inspecting the automaton. It is explicitly **not** the verdict.

### Alphabet projection

Events are projected onto the policy's alphabet before lookup. An atom the policy never
mentions cannot change the verdict, and the projection keeps the transition table finite
regardless of how many unrelated atoms a trace carries.

### Minimization

Moore partition refinement, where the equivalence invariant is **the residual plus
transition agreement**. Comparing acceptance behaviour alone would be wrong: two different
residuals can agree on every transition yet produce different verdicts, and merging them
lets the surviving representative's obligation decide a state whose real obligation was
discarded.

State counts are reported in `build_stats` rather than hidden. A policy whose monitor
explodes is unusable in CI, and the cost should be visible before it bites.

---

## 5. End-of-trace semantics

The part most likely to be got wrong, and the part that took the longest to get right.

Derivatives shift obligations forward, so after the last event every atom in the residual
refers to a position that does not exist. Each operator gets its own rule, and they do not
agree with one another:

| Residual | Verdict |
|---|---|
| atom `a` | reject |
| `!a` | accept |
| `G(p)` | accept (vacuous) |
| `F(p)` | recurse into the operand |
| `X(p)` | reject |
| `p U q`, `p W q` | reject |
| `p R q` | accept (vacuous) |

`F` recursing while `U` rejects is the asymmetry that matters. Vacuous truth is a
witness for `G` and `R`; it is **not** a witness for `U`, or `!(a R b)` would be satisfied
on any all-`b` trace.

Full derivation and the two wrong implementations that preceded it: see
[`METHODOLOGY.md`](METHODOLOGY.md).

---

## 6. The parser

Recursive descent, hand-written. Deliberately **not** a map-tokens-to-callables design
using `eval()` — parsing a security policy through `eval` is a remote-code-execution
surface, and a tool whose job is enforcing policy should not introduce one.

Operator precedence, from loosest to tightest:

```
->        implication
||
&&
!         negation
G F X U R  temporal operators
atoms
```

Single-letter aliases are **uppercase only** (`G`, `F`, `X`, `U`, `R`). Accepting
lowercase `g`/`f`/`x` would make those names unusable as atom identifiers, because the
tokenizer would consume them as operators. A policy DSL where `x` cannot be an atom is
worse than one that demands `next(x)`.

There is a test asserting the engine imports nothing from `openai`, `anthropic`,
`requests`, `httpx`, `torch`, `socket`, or `urllib`. The determinism claim should be
enforced by something, not just asserted in a README.

---

## 7. Differential verification

```
engine (derivatives)          reference (positional recursion)
      |                                   |
      +-------- verdict == verdict? ------+
                   every trace
```

`scripts/differential_test.py` implements the reference by evaluating formulas directly
at each position of the trace. It shares no code with `automata.py`.

The design goal is **disagreement capability**. A test suite written by the same person
who wrote the engine tends to agree with it. A second implementation that can say "no" is
worth more than ten more assertions.

It found three real bugs the 52 hand-written tests missed. Current status: 91,182 traces
compared, zero disagreements.

Wired into CI at length 4 on every push, length 6 on a schedule — the exhaustive cost
grows as `(alphabet + 1)^length`.

---

## 8. The measurement layer

Three modules, deliberately separated by what they are allowed to know.

| Module | Responsibility |
|---|---|
| `corpus.py` | Parse and validate corpus files; check every case against the engine |
| `adapters.py` | The `ControlAdapter` interface; score and compare two controls |
| `stats.py` | Wilson intervals and exact McNemar. Closed-form, stdlib only |
| `baseline.py` | Three reference controls, including the two useless ones |

### Three decisions, not status symbols

`Decision` has **three** values, not two. A control that saw only the first three steps of a
five-step trace has not "allowed" the last two; it has never been exposed to them.
Collapsing `NOT_REACHED` into `ALLOWED` would inflate a false-negative rate, and it would do
so silently, on partial runs, in a way nobody would notice. `ControlRun.decision_for`
therefore defaults a missing entry to `NOT_REACHED` — the safe direction, because an
incomplete run must never read as evidence of permission.

### The reference controls exist to be beaten

`baseline.py` ships `none` (allows everything), `block-everything` (blocks everything), and
`allowlist` (keyword matching). The last two are not useful as controls; they are useful as
arguments:

- `block-everything` scores **100% detection and 100% false positives.** It is what a
  single-rate report would present as a perfect score.
- `allowlist` scores 69.2% detection with a 38.5% false-positive rate, and misses exactly
  four cases: `refund-002-unapproved`, `refund-003-prior-approval-insufficient`,
  `refund-004-stalled-before-approval`, and `terminated-002-stalled`. All four violate a
  *temporal* obligation while containing no banned token at all. That is the corpus doing
  the job it exists for — a keyword-matching guardrail cannot score well here, and now there
  is a number that says so.

`compare_controls` counts only violation cases, because a blocks-everything control wins on
detection while being unusable, and a test comparison that rewarded that would be measuring
the wrong thing.

---

## 9. Not built yet

Named explicitly so this document cannot be mistaken for a complete design.

| Component | State |
|---|---|
| **Attack corpus** | 🚧 baseline only — 26 cases, 9 categories, in `corpus/` |
| **Corpus schema + validator** | ✅ `corpus.py`, verified in CI |
| **Control adapter interface** | ✅ `adapters.py` — abstract, not yet against a real product |
| **Scoring + paired comparison** | ✅ detection, false positives, Wilson, exact McNemar |
| **Real vendor adapters** | ❌ not started — **the honest gap** |
| **Power analysis** | ❌ not started; 13 violation cases cannot separate two products |
| **YAML policies** | ❌ not started; policies are strings today |
| **Report generation** | ❌ not started |

The pipeline is complete end to end: corpus → adapter → scores → intervals → paired test.
Everything in it is tested and deterministic. What is missing is the two things that would
make the result mean something — a real adapter, and a corpus large enough for the
statistics to have power.

Those are not the same kind of gap. The adapter is engineering and needs a target product
and a sandbox. The corpus is domain knowledge, and no amount of code produces it. Both are
open invitations in [`CONTRIBUTING.md`](../CONTRIBUTING.md).
