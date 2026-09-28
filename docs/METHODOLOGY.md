# Methodology

How TruePlumb computes a verdict, and — more importantly — what that verdict does not
claim.

This document exists because a security tool that overstates what it has proven is worse
than no tool. Everything below is written so a skeptical reader can disagree with it.

---

## 1. What a verdict is

A verdict answers exactly one question:

> Given this recorded sequence of events, and this temporal policy, does the policy hold
> at every point the policy constrains?

That is a statement about **the trace you provided**. It is not a statement about the
agent, the model, the deployment, or the control being evaluated.

| Verdict | Means |
|---|---|
| `COMPLIANT` | The policy holds over this trace. |
| `VIOLATION` | The policy fails, and a counterexample shows where. |

There is no third verdict. There is no confidence score on an individual trace, because
uncertainty about a single deterministic computation is not a meaningful concept — either
the automaton accepted the trace or it did not.

Uncertainty appears one level up, when many traces are aggregated into a pass rate. That
is where confidence intervals belong, and that is implemented — see §7.

---

## 2. Why a verdict is reproducible

The scoring path contains **no LLM calls, no network access, and no model weights**.

Policy compliance is decided by runtime verification. A policy is parsed into a temporal
logic formula, compiled to a deterministic finite automaton, and run over the trace. The
computation is a fixed function of `(policy, trace)`:

- same inputs → same output, on any machine, in any Python version
- no sampling, no temperature, no seed
- no external service that could change its answer between runs

This is a design constraint, not an implementation detail. The motivating observation is
that model-graded evaluation is not reproducible: a grader called without an explicit
sampling configuration runs at non-zero temperature, so per-item verdicts drift between
runs and a reported score is an average over noise the reader cannot see. A conformance
claim that cannot be reproduced is not a claim, it is an anecdote.

---

## 3. What "verified" means in this repository

The engine is checked against a **second, independent implementation** of LTL semantics
in `scripts/differential_test.py`.

That reference:

- evaluates formulas by direct positional recursion over the trace
- shares **no code path** with the derivative-based monitor
- is checked against every possible trace over each formula's alphabet, up to a configured
  length

```bash
python scripts/differential_test.py 4    # 6,540 traces   (CI, every push)
python scripts/differential_test.py 5    # 23,946 traces
python scripts/differential_test.py 6    # 91,182 traces  (scheduled)
```

Current result: **zero disagreements.**

### Why this matters more than test count

A hand-derived test can only confirm what its author already believed. When the author and
the implementation share a misconception, the test suite agrees with the bug — and a green
suite becomes evidence of nothing.

That is not hypothetical here. The differential harness found **three end-of-trace
semantics bugs that all 52 hand-written tests agreed on**, including two where I had
written a test encoding the same wrong belief as the engine. The suite was green while the
engine was wrong.

Both kinds of testing are kept. The hand-written tests document *why* each rule exists;
the differential harness is what actually establishes correctness. Neither substitutes for
the other.

---

## 4. End-of-trace semantics

The subtle part, and the part most likely to be got wrong by anyone reimplementing this.

Derivatives shift obligations **forward**: after the final event is consumed, every atom
left in the residual refers to a position that does not exist. Each operator therefore
needs its own rule for what an absent future means, and **the operators do not agree**:

| Residual | Verdict | Reasoning |
|---|---|---|
| atom `a` | reject | needs a *fact*; no position remains to carry it |
| `!a` | accept | a claim about everything ahead; the future is empty |
| `G(p)` | accept | vacuously true — no future, no future violation |
| `F(p)` | recurse | the end position can witness, but only vacuously |
| `X(p)` | reject | the successor genuinely does not exist |
| `p U q`, `p W q` | reject | needs a right-witness at a **real** position |
| `p R q` | accept | nothing can break a release with no future steps |

### The `U` asymmetry

`F` recursing but `U` not is the rule most implementations get wrong, including this one,
twice.

Vacuous truth over the empty suffix is a legitimate witness for `G` and `R`. It is **not**
a witness for `U`. `!(a R b)` normalizes to `(!a) U (!b)`; on a trace of all-`b`, the
`!b` is vacuously true at the end, and accepting that would let an obligation the agent
never satisfied be discharged by the trace simply running out.

### The guarantee this produces

`F(agent_done)` is **violated** by a trace that stops without ever finishing.

An agent that stalls halfway through a policy cannot report success. This is the single
property the whole design rests on, and it is pinned by
`tests/test_end_of_trace_semantics.py`.

---

## 5. Counterexamples

On violation, TruePlumb reports the **first step at which the policy stopped holding**,
plus the entire remaining suffix.

Choosing the earliest failure is deliberate: everything after it is downstream of that
decision, and a developer debugging the incident needs the point where it went wrong, not
the last point where things happened to be fine.

The extracted slice is itself a violating trace. It can be replayed on its own and will
independently fail — which is what makes it evidence rather than a description. This is
asserted in the test suite, because a counterexample that cannot be replayed is just a
claim.

---

## 6. What a verdict does NOT claim

Stated plainly, because this is where a tool like this would normally overreach.

- **Not** that the agent is safe. Only that *this trace* satisfied *this policy*.
- **Not** that the policy is correct. TruePlumb verifies that behaviour matches the
  policy as written. Whether the policy is the right policy is a human judgement, and
  nothing here checks it.
- **Not** statistical confidence, *at the trace level*. One trace in, one deterministic
  verdict out. Confidence intervals appear only when many traces are aggregated into a rate
  (§7), and even then only over the corpus that was measured.
- **Not** coverage of unrecorded behaviour. If an action is not in the trace, no policy
  constrains it. A trace that never mentions `delete_records` trivially satisfies
  `G(!delete_records)` — and that is a statement about the trace, not about the system
  that produced it.
- **Not** model behaviour. No claim is made about what a model *would* do on a trace not
  recorded. Sampling an agent to build traces introduces its own non-determinism and is
  explicitly outside the verified path.

That fourth point is the most important limitation, and it is why the corpus — not the
verifier — is the hard part of this project.

---

## 7. Aggregating to a rate

Once a corpus exists, a verdict per case becomes a proportion, and a proportion without an
interval is a number that invites over-reading. Two decisions govern how TruePlumb reports
it.

### Two rates, never one

A control's score is **detection rate** over cases that should be blocked, and **false
positive rate** over cases that should pass. They are never combined.

This is not a stylistic preference. A control that blocks every case scores 100% detection
and is completely useless; the only number that reveals this is the false-positive rate.
`tests/test_adapters.py` asserts this against a control that really does block everything,
and asserts that no single `accuracy` field is ever produced — the report shape is the
defence against someone later "simplifying" it.

### Why Wilson and not Wald

The normal-approximation interval is wrong precisely where conformance results live. Near
rates of 0 and 1, and at small n, it can return a lower bound below zero or an upper bound
above one, and it under-covers exactly when a control passes 20 of 20 attacks — the case a
vendor is most eager to quote. The Wilson score interval stays inside [0, 1] and keeps
sensible width. Reference values are pinned in `tests/test_stats.py` against the standard
tables (10/10 → [0.7225, 1.0], 1/10 → [0.0179, 0.4042]).

The **point estimate is never shrunk toward the interval centre.** Wilson shifts the
interval, not the estimate, so the reported rate is the observed rate.

### Why exact McNemar

Both controls see the same corpus, so the comparison is paired, and only the cases where
they *disagree* carry information. McNemar is the test for that, and the exact binomial
formulation is used rather than the chi-square approximation because discordant counts in a
security corpus are routinely in single digits, where the approximation is not trustworthy.

Three honesty rules are enforced in the code:

- No discordant pairs → p = 1, reported as "identical on all N cases". A coin flip is not
  a result.
- Not significant → "**no detectable difference**", never "equivalent" and never "as good
  as". A non-significant result means the corpus is too small to tell them apart, and the
  CLI prints that caveat alongside the number.
- Detection only. False-positive differences are real, but mixing them into one test
  answers "are these different" without answering "which is better" — and a
  blocks-everything control wins on detection while being unusable.

### Why there is no RNG

Every formula here is closed-form. A bootstrap interval is only reproducible if the seed is
fixed, and a fixed seed is exactly the sort of thing that silently differs between a laptop
and a CI runner. Determinism is a stated design rule, and a confidence interval that varied
between runs could not be compared across controls — which is the only reason it exists.

---

## 8. Current limitations

Stated so nobody has to discover them:

1. **Finite-trace semantics only.** A policy is judged over the trace provided. There is no
   notion of verifying behaviour beyond the recording, and `G` holds vacuously at the end.
2. **Atom names are the abstraction boundary.** A policy constrains the vocabulary of the
   trace, so a badly chosen vocabulary produces confident verdicts about the wrong things.
   This is why the loader rejects malformed atoms rather than coercing them.
3. **The corpus is a baseline, not a measurement.** 26 cases across 9 attack categories
   exist and are verified in CI, but that is a regression suite for the engine expressed as
   data. It is far too small to rank two controls, which is the product's actual claim.
4. **No real vendor adapters.** The adapter interface, the scoring pipeline, and three
   reference controls ship and work. What is missing is the part that actually matters: an
   adapter for a real product. Until a contributor supplies one, TruePlumb measures itself
   and its own reference baselines, not the market. Nothing here has been run against a
   commercial guardrail, and no result in this repository should be presented as if it had.
9. **Adapter scores are trusted at face value in one specific way.** TruePlumb records a
   control's *decision*, not its internal state. A control that silently truncates its own
   trace, or that only evaluates the first N steps, will be recorded as `NOT_REACHED` on
   the rest — correct, but it will look like a miss rather than a broken harness. Real
   adapters need their own checks that every step was genuinely presented.
5. **Statistics are implemented but underpowered by the corpus.** Wilson intervals and the
   exact McNemar test are closed-form, seed-free, and verified against published values.
   They are computed over 13 violation and 13 compliant cases, so every interval in this
   repository is very wide. A wide interval is an honest one, and a 13-case corpus is too
   small to separate two real products. The arithmetic is ready; the data is not.
   Note also that McNemar is a test on discordant pairs only: with few of them it lacks the
   power to detect a real difference, and "no detectable difference" must not be read as
   "these are equivalent." The CLI says so at the point of output.
6. **Counterexample extraction cannot distinguish "not yet" from "never".** The
   first-failing-prefix rule reports the earliest prefix that fails. For a trace ending
   without discharging `F(done)`, every prefix fails, so the report is step 0 — correct,
   but not informative. The useful answer ("the run stalled") is not what the tool currently
   says. Corpus cases therefore do not pin a step where the failure is an abandonment.
7. **The differential harness is bounded by trace length.** Coverage grows as
   `(alphabet + 1)^length`. Length 6 is roughly 91k traces per formula set, which is strong
   evidence but not a proof for all lengths.
8. **State explosion is real.** A policy whose monitor explodes is unusable in CI. The
   `explain` command reports state counts so this is visible before it bites, but no
   guard rail rejects an expensive policy automatically.
