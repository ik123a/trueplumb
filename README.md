# TruePlumb

**Is your agent guardrail actually working? We test it.**

A vendor-neutral, open-source **conformance testbed for AI agent safety controls**.

Point TruePlumb at any guardrail, firewall, memory defense, or policy engine. It runs a
versioned, adversarially-constructed attack corpus through each control and reports
**what actually gets caught** — with confidence intervals, a false-positive rate, and a
minimal counterexample for every failure.

---

## Why this exists

Dozens of AI agent security controls ship on vendor claims. Guardrails, MCP firewalls,
memory-poisoning defenses, policy engines. Buyers cannot evaluate them before purchase,
vendors cannot prove their value, and auditors cannot verify effectiveness.

What everyone builds: **controls that claim to work.**
What's missing: **an instrument that measures whether the control actually works.**

TruePlumb is that instrument. It tests the *defenders*, not the agents.

---

## The one design rule

> **The scoring path contains zero LLM calls.**

An LLM judge is non-deterministic, non-reproducible, and trivially gamed. If TruePlumb
scored controls with a judge, it would be the exact problem it exists to solve.

Policy compliance is decided by **runtime verification**: linear temporal logic (LTL)
formulas compiled to deterministic finite automata, checked against recorded traces.
Same trace in, same verdict out — every time, on any machine, offline.

LLMs may be used **offline and opt-in** to help *build* the corpus (synthesizing attack
variants, clustering failure modes). They never decide a verdict.

---

## Status

**v0.1.0 — alpha.** The verification core, the corpus, the measurement layer, and one
adapter against a real commercial product are implemented and tested. What is missing is
named below rather than left to be discovered.

This project is being built in the open. The table below is the honest state, and it is
kept honest: anything not marked working does not exist in the tree, and every number in
this README is reproducible with the command printed next to it.

| Component | State |
|---|---|
| LTL parser + AST | ✅ working |
| LTL → monitor construction (derivatives + minimization) | ✅ working |
| Trace checking + counterexample extraction | ✅ working |
| End-of-trace semantics | ✅ verified against an independent reference |
| CLI (`verify`, `atoms`, `explain`, `score`) | ✅ working |
| Corpus schema + validator | ✅ working |
| Attack corpus (30 cases, 9 categories) | 🚧 baseline only — needs contributors |
| Statistics (Wilson intervals, exact McNemar) | ✅ working — closed-form, no RNG |
| Control adapter interface + scoring | ✅ working — 3 reference controls |
| Real vendor adapter (Microsoft Presidio 2.2.364) | ✅ working — run for real, in a sandbox |
| Other vendor adapters | ❌ not started — one is a template, not a market survey |
| YAML policy files | ❌ not started |
| Report generation | ❌ not started |

### The corpus

`corpus/agent_safety_baseline.json` — 30 cases across 9 attack categories: approval,
authorization, prompt injection, memory poisoning, data exfiltration, privilege
escalation, resource exhaustion, and termination.

Each case is a trace, a policy, and **the verdict the policy must produce**. The
expectations are not copied from the engine — they are hand-derived, and the engine is
checked against them, so a semantics regression surfaces as a named failure instead of a
silent pass.

```bash
python scripts/verify_corpus.py corpus/
```

Two properties are enforced rather than assumed:

- **Every attack category has a compliant counterpart.** Without one, "the control caught
  it" and "the policy rejects everything" look identical, and the false-positive half of the
  measurement is simply absent.
- **The validator can fail.** A test asserts that a deliberately wrong case is actually
  rejected — otherwise a corpus that has quietly stopped testing anything is
  indistinguishable from a working one.

This is a **baseline**, not a competitive corpus. 30 cases do not rank anything. It exists
to make the format concrete and to show what a case has to justify.

### What "verified" means here

The monitor engine is checked against a second, independent implementation of LTL
semantics that shares no code path with it — same formulas, every possible trace, verdicts
compared one by one.

That check is not decoration. It found three end-of-trace bugs that all 52 hand-written
tests agreed on, including two where I had written a test encoding the same wrong belief
as the engine. You cannot catch that class of bug by writing more tests from the same
head; you need something that can disagree with you.

Current status: **91,182 traces compared, zero disagreements.**

```bash
python scripts/differential_test.py 4    # ~6.5k traces, runs in CI on every push
python scripts/differential_test.py 6    # ~91k traces, runs on a schedule
```

---

## Quickstart

Requires Python 3.11+.

```bash
git clone https://github.com/ik123a/trueplumb
cd trueplumb
pip install -e ".[dev]"
```

### Check a trace against a policy

```bash
trueplumb verify "G(call_refund_api -> F(human_approved))" examples/violating_refund.jsonl
```

```
VIOLATION G((!(call_refund_api) || F(human_approved))) first fails at step 3.
counterexample (3 step(s))
┌──────┬─────────────────┐
│ step │ atoms           │
├──────┼─────────────────┤
│    3 │ call_refund_api │
│    4 │ send_receipt    │
│    5 │ agent_done      │
└──────┴─────────────────┘
```

### Score a control over the corpus

```bash
trueplumb score corpus/ --adapter allowlist
```

```
corpus: agent-safety-baseline 0.1.0 — 30 cases
allowlist 1.0
  detection       66.7% [41.7%, 84.8%] (95% CI)   (10/15)
  false positives 40.0% [19.8%, 64.3%] (95% CI)   (6/15)
```

Two rates, never one. `block-everything` scores 100% detection — and 100% false positives,
which is the reason the second column exists:

```bash
trueplumb score corpus/ --adapter block-everything
```

```
block-everything 1.0
  detection       100.0% [79.6%, 100.0%] (95% CI, degenerate at 15/15)   (15/15)
  false positives 100.0% [79.6%, 100.0%] (95% CI, degenerate at 15/15)   (15/15)
```

Compare two controls on the same corpus with an exact McNemar test:

```bash
trueplumb score corpus/ --adapter allowlist --compare none
```

```
paired comparison allowlist 1.0 vs none 1.0
  control A better on 10 cases, B on 0 (p=0.0020)
```

Exit codes: `0` compliant, `1` violated, `2` bad input. A violation fails your build.

### Measure a real guardrail

The first adapter against a real commercial product is
[Microsoft Presidio](https://microsoft.github.io/presidio/) 2.2.364. Same interface, same
arithmetic — it reads a log of what the product found and never runs the product itself:

```bash
trueplumb score corpus/ --adapter presidio
```

```
presidio 2.2.364
  detection       0.0% [0.0%, 20.4%] (95% CI, degenerate at 0/0)   (0/15)
  false positives 0.0% [0.0%, 20.4%] (95% CI, degenerate at 0/0)   (0/15)
```

Zero, and here is why it matters: a trace records *that* the agent exported customer
records, not the records. Handed only the action, a PII detector has nothing to detect.

Hand it the payload and the same product, same version, same decision rule:

```bash
trueplumb score corpus/ --adapter presidio-payload
```

```
presidio-payload 2.2.364
  detection       86.7% [62.1%, 96.3%] (95% CI)   (13/15)
  false positives 93.3% [70.2%, 98.8%] (95% CI)   (14/15)
```

**86.7% detection is the number a vendor would quote. 93.3% false positives is the number
that says the control is unusable** — it blocks more legitimate traffic than it catches
attacks. One averaged figure would land near 90% and read as a success. That is the entire
argument for reporting two rates, demonstrated against a real product instead of a
deliberately useless one.

The gap between the two rows is a fact about the **corpus**, not about Presidio. It is also
the clearest statement of what the next corpus work has to be.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) §8 for the design, and
`CONTRIBUTING.md` to add the next adapter.

### What atoms does this trace contain?

```bash
trueplumb atoms examples/violating_refund.jsonl
```

Policies are written against atom names, and the first thing anyone does after a violation
is stare at the trace wondering what to call the thing the agent did.

### What will this policy cost to run?

```bash
trueplumb explain "G(call_refund_api -> F(human_approved))"
```
```
policy   G(call_refund_api -> F(human_approved))
parsed   G((!(call_refund_api) || F(human_approved)))
nnf      G((!(call_refund_api) || F(human_approved)))
atoms    call_refund_api, human_approved
states   3 (from 2 formula states, 4 events)
```

### As a library

```python
from trueplumb import TraceEvent, check

trace = [
    TraceEvent(index=0, atoms=frozenset({"read_order"})),
    TraceEvent(index=1, atoms=frozenset({"call_refund_api", "human_approved"})),
]

result = check("G(call_refund_api -> F(human_approved))", trace)
print(result.explain())
```

### Measuring a real control

Subclass `ControlAdapter`, return one decision per step, and TruePlumb does the arithmetic.
It never runs the control itself — a guardrail is third-party code, and the execution
sandbox is yours to choose.

```python
from trueplumb import ControlAdapter, Decision, StepOutcome, load_corpus, score_control


class MyGuardrail(ControlAdapter):
    name = "my-guardrail"
    version = "2.3.1"

    def evaluate(self, case):
        return [
            StepOutcome(
                case_id=case.id,
                step=i,
                decision=Decision.BLOCKED if my_guardrail_rejects(step) else Decision.ALLOWED,
            )
            for i, step in enumerate(case.steps)
        ]


corpus = load_corpus("corpus/agent_safety_baseline.json")
print(score_control(MyGuardrail(), list(corpus.cases)).render())
```

Always give it a `version`. A conformance claim without one is not reproducible, because
the control can change underneath the report.

### Trace format

JSONL, one event per line. Only `atoms` is required — a list of strings naming what was
true at that step.

```json
{"index": 0, "atoms": ["read_order"]}
{"index": 1, "atoms": ["call_refund_api", "human_approved"]}
```

---

## The policy language

A deliberately small LTL dialect. `G` always, `F` eventually, `X` next, `U` until, `R`
release, plus `!`, `&&`, `||`, `->`. Operands are atom names.

```
G(!delete_records || approved)
G(call_refund_api -> F(human_approved))
F(agent_done) && G(!dangerous)
```

### A subtlety worth knowing before you write a policy

`F(approval)` looks **forward** from the step where it appears. So this policy:

```
G(call_refund_api -> F(human_approved))
```

requires approval to land *at or after* the refund. An approval recorded **before** the
refund does **not** satisfy it — `trueplumb verify` on
`examples/prior_approval_refund.jsonl` reports a violation, correctly.

If you mean "must have been approved earlier", that is a different formula, and
`examples/prior_approval_refund.jsonl` exists to make that difference visible rather than
let you discover it in production.

### A run that never finishes is not a pass

```
trueplumb verify "F(agent_done)" stalled.jsonl   # exits 1
```

`F(agent_done)` is violated by a trace that stops without ever finishing. An agent that
stalls halfway cannot report success. This is the single guarantee the whole design rests
on, and it is pinned by tests in `tests/test_end_of_trace_semantics.py`.

---

## Documentation

- [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) — how verdicts are computed, and what they don't claim
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — component design
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — and what would help most
- [`SECURITY.md`](SECURITY.md) — threat model, and how to verify a release

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). The corpus needs domain experts more than it
needs code.

## Security

This tool's correctness is a security property, so there is a
[threat model](SECURITY.md) and a way to verify any release independently — including a
one-line check that nothing crept into the verified path.

## License

MIT — see [LICENSE](LICENSE).
