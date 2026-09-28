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
properties compiled to deterministic finite automata, checked against recorded traces.
Same corpus in, same verdict out — every time, on any machine, offline.

LLMs may be used **offline and opt-in** to help *build* the corpus (synthesizing attack
variants, clustering failure modes). They never decide a verdict.

---

## Status

**v0.1.0 — alpha.** The verification core is implemented and tested. The corpus and
control adapters are in progress.

| Component | State |
|---|---|
| LTL parser + AST | ✅ working |
| LTL → NBA → DFA monitor construction | ✅ working |
| Trace checking + counterexamples | ✅ working |
| Wilson score intervals | ✅ working |
| Control adapter interface | ✅ working |
| Attack corpus | 🚧 in progress |
| CLI | 🚧 in progress |

---

## Quickstart

```bash
git clone https://github.com/ik123a/trueplumb
cd trueplumb
pip install -e ".[dev]"

# Check a trace against a policy
trueplumb check \
  --policy policies/refund_requires_human.yaml \
  --trace examples/support_agent_trace.jsonl
```

## Documentation

- [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) — how verdicts are computed, and what they don't claim
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — component design

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). The corpus needs domain experts more than it
needs code.

## License

MIT — see [LICENSE](LICENSE).
