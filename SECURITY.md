# Security Policy

## Supported versions

TruePlumb is pre-1.0. Fixes land on `main` and in the next release; there are no
long-term support branches yet.

| Version | Supported |
|---|---|
| `0.1.x` | ✅ |
| `< 0.1` | ❌ |

## Reporting a vulnerability

**Do not open a public issue for a security vulnerability.**

Open a
[private security advisory](https://github.com/ik123a/trueplumb/security/advisories/new)
on this repository instead. That keeps the report private, gives us a place to discuss the
fix before it is public, and works even before a release exists.

Include:

- what the issue is, and what an attacker gains
- a minimal policy + trace that reproduces it, if you have one
- the version and commit

You will get an acknowledgement within 72 hours and a substantive response within seven
days. Fixes for confirmed issues are released as soon as they are verified, and credited
in the release notes unless you prefer otherwise.

While the project is alpha, expect fast turnaround and occasional breaking changes.

---

## Threat model

TruePlumb decides whether a recorded trace satisfies a temporal policy. Its correctness
properties are therefore security properties.

### What the design defends against

| Threat | Mitigation |
|---|---|
| **Policy injection via the parser** | Hand-written recursive descent. No `eval()`, no token-to-callable dispatch. |
| **Network or model dependency in the verified path** | Engine imports stdlib only; asserted by a test that fails on `openai`, `requests`, `httpx`, `socket`, `torch`, and others. |
| **Non-deterministic verdicts** | No sampling, no temperature, no seed, no clock, no randomness. Same input → same verdict. |
| **Trace data corrupting a verdict silently** | Malformed `atoms` are rejected, not coerced. A string `"dangerous"` must not become nine single-character atoms. |
| **A vacuous pass** | An undischarged obligation is a violation. `F(agent_done)` fails on a trace that never finished. |
| **Unsound simplification** | Only loop identities that are provably valid are applied. Subformula elimination is banned. |
| **Minimization discarding a live obligation** | The residual is part of the state equivalence key. |

### What the design does NOT defend against

Stated plainly, because overclaiming here would be self-defeating.

- **A wrong policy.** TruePlumb verifies that behaviour matches the policy *as written*. It
  does not check that the policy is the right policy. `G(!delete_records || approved)` is
  trivially satisfied by a trace that never mentions either atom.
- **Incomplete traces.** Only what is recorded is constrained. If an action is absent from
  the trace, no policy can constrain it.
- **A dishonest trace producer.** The verifier trusts the trace it is given. Recording is
  the integrator's responsibility, and an adapter that silently drops events defeats every
  guarantee above.
- **Model behaviour on unseen traces.** No claim is made about what an agent would do on a
  trace that was not recorded.

See [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) §6 for the full statement.

---

## Verifying a release

Because determinism is the product claim, a consumer can check it:

```bash
# 1. The differential gate: engine vs independent reference semantics, every short trace.
python scripts/differential_test.py 5     # expect: PASS, zero disagreements

# 2. Determinism: identical verdicts across repeated runs.
python -c "
from trueplumb import TraceEvent, check
t=[TraceEvent(index=0, atoms=frozenset({'act'}))]
print([check('G(!dangerous)', t).compliant for _ in range(100)])
"

# 3. No hidden dependencies in the verified path.
grep -rE '\b(openai|anthropic|requests|httpx|torch|socket|urllib)\b' src/trueplumb/ltl/ && echo 'FAIL' || echo 'OK'
```

Step 3 should print nothing. Any match means a dependency crept into the engine, and the
determinism guarantee is no longer something the project can make.
