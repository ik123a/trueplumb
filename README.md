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

**v0.1.0 — alpha.** The verification core is implemented, tested, and differentially
verified. The corpus, control adapters, and statistics are not built yet.

This project is being built in the open. The table below is the honest state, and it is
kept honest: anything not marked working does not exist in the tree.

| Component | State |
|---|---|
| LTL parser + AST | ✅ working |
| LTL → monitor construction (derivatives + minimization) | ✅ working |
| Trace checking + counterexample extraction | ✅ working |
| End-of-trace semantics | ✅ verified against an independent reference |
| CLI (`verify`, `atoms`, `explain`) | ✅ working |
| Corpus schema + validator | ✅ working |
| Attack corpus (26 cases, 9 categories) | 🚧 baseline only — needs contributors |
| Statistics (Wilson intervals, McNemar) | ❌ not started |
| Control adapter interface | ❌ not started |
| YAML policy files | ❌ not started |

### The corpus

`corpus/agent_safety_baseline.json` — 26 cases across 9 attack categories: approval,
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

This is a **baseline**, not a competitive corpus. 26 cases do not rank anything. It exists
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

Exit codes: `0` compliant, `1` violated, `2` bad input. A violation fails your build.

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
