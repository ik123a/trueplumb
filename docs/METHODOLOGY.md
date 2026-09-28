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
is where confidence intervals belong, and where they are not yet implemented.

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
- **Not** statistical confidence. One trace in, one deterministic verdict out.
  Confidence intervals belong to aggregate pass rates, which are not implemented yet.
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

## 7. Current limitations

Stated so nobody has to discover them:

1. **Finite-trace semantics only.** A policy is judged over the trace provided. There is no
   notion of verifying behaviour beyond the recording, and `G` holds vacuously at the end.
2. **Atom names are the abstraction boundary.** A policy constrains the vocabulary of the
   trace, so a badly chosen vocabulary produces confident verdicts about the wrong things.
   This is why the loader rejects malformed atoms rather than coercing them.
3. **The corpus does not exist yet.** The verifier is real; the adversarial input that
   makes it useful is not built.
4. **No control adapters.** TruePlumb cannot yet run a trace through an actual guardrail.
5. **No aggregate statistics.** Pass rates, false-positive rates, and confidence intervals
   are all unimplemented.
6. **The differential harness is bounded by trace length.** Coverage grows as
   `(alphabet + 1)^length`. Length 6 is roughly 91k traces per formula set, which is strong
   evidence but not a proof for all lengths.
7. **State explosion is real.** A policy whose monitor explodes is unusable in CI. The
   `explain` command reports state counts so this is visible before it bites, but no
   guard rail rejects an expensive policy automatically.
