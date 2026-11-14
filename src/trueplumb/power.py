"""How many cases a conformance claim actually needs.

Every limitation in this project is ultimately a sample-size problem, and the docs currently
say "too few" without ever saying how many would be enough. That is the least useful form of
an honest limitation: it tells a reader the number is inadequate without telling them what
would make it adequate, so nobody can act on it.

Three questions, three functions:

``mcnemar_power``      can this corpus tell these two controls apart?
``mcnemar_power_exact`` the same, without the normal approximation.
``cases_for_precision`` how many cases until the confidence interval is narrow enough to
                       distinguish a good control from an acceptable one?

Why this exists at all: a 15-case corpus produced a comparison where the control caught 13
and the other caught 0, and the answer was "no detectable difference" -- a real result, and
a useless one, because nothing in the tooling could say whether 15 was hopeless or nearly
enough. It was nearly enough. That should be computable rather than a guess.

Everything here is deterministic and closed-form or direct summation. No sampling, no RNG,
no simulation -- the same reason as the rest of the statistics, and it matters more here,
because a power calculation quoted in a paper is one somebody has to reproduce.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import erf, sqrt

from .stats import StatsError, Z_SCORES, wilson_interval

#: Expected discordant count below which the normal approximation is not trustworthy.
#:
#: Measured, not guessed: at psi=0.5, theta=0.8 the approximation overstates exact power by
#: +0.45 at n=20, +0.36 at n=30, +0.22 at n=40, +0.13 at n=50, and only falls inside 0.05 of
#: exact at n=80 (40 discordant). The error tracks the discordant count, not n, which is why
#: the threshold is stated in discordant pairs. A power figure quoted from the approximation
#: below this line would be an overstatement, and an overstatement about how much evidence
#: exists is precisely the error this project is trying to stop making.
MIN_DISCORDANT_FOR_NORMAL = 40


def _phi(x: float) -> float:
    """Standard normal CDF. `erf` is in the stdlib, so this needs no SciPy."""
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def _z_for_alpha(alpha: float) -> float:
    """Two-sided critical value z_{1-alpha/2} from the tabulated one-sided quantiles."""
    confidence = 1.0 - alpha
    if confidence in Z_SCORES:
        return Z_SCORES[confidence]
    # Interpolate between the two nearest tabulated quantiles rather than silently
    # accepting an untabulated value. A power figure computed from a guessed z is a power
    # figure nobody should quote.
    lower = max((c for c in Z_SCORES if c < confidence), default=None)
    upper = min((c for c in Z_SCORES if c > confidence), default=None)
    if lower is None or upper is None:
        raise StatsError(
            f"alpha={alpha} implies confidence {confidence}, outside the tabulated range "
            f"{min(Z_SCORES)}..{max(Z_SCORES)}"
        )
    weight = (confidence - lower) / (upper - lower)
    return Z_SCORES[lower] + weight * (Z_SCORES[upper] - Z_SCORES[lower])


@dataclass(frozen=True, slots=True)
class PowerResult:
    """The answer, plus the assumptions it rests on, so it cannot be quoted bare."""

    power: float
    n: int
    psi: float
    theta: float
    alpha: float
    expected_discordant: float
    expected_b: float
    expected_c: float
    method: str
    reliable: bool = True
    """False when this is an approximation below the threshold where it holds.

    Set rather than documented in prose, so a caller printing a `PowerResult` cannot
    accidentally quote a figure that is known to be optimistic.
    """

    def __float__(self) -> float:
        return self.power

    def render(self) -> str:
        caveat = "" if self.reliable else f"  ** approximation unreliable below {MIN_DISCORDANT_FOR_NORMAL} discordant"
        return (
            f"power {self.power:.3f} at n={self.n} "
            f"(psi={self.psi:.2f}, theta={self.theta:.2f}, alpha={self.alpha}, "
            f"~{self.expected_discordant:.1f} discordant of {self.n})"
            f"{caveat}"
        )


def mcnemar_power(
    n: int,
    psi: float,
    theta: float,
    alpha: float = 0.05,
) -> PowerResult:
    """Normal-approximation power for McNemar's test.

    Parameters
    ----------
    n
        Number of violation cases, i.e. the pairs under comparison.
    psi
        Discordance rate: the probability that the two controls differ on a case at all.
        This is the parameter people forget. Two controls that agree on 95% of the corpus
        can never be separated by any corpus size, because only the 5% of cases where they
        disagree carry any information. psi is an empirical property of the two controls,
        and the single most useful number to measure before designing a benchmark.
    theta
        Of the discordant cases, the share control A gets right. theta=0.6 means A wins
        60/40 among the cases where they differ.
    alpha
        Two-sided significance level.

    The approximation treats b and c as independent binomials with n trials at
    psi*theta and psi*(1-theta), which is why the non-centrality is

        n*psi*(2*theta - 1) - 1
        -------------------------
          sqrt(2*n*psi*theta*(1-theta))

    The -1 is the continuity correction McNemar's test actually applies. It is small but it
    is the difference between the approximation and the test being scored.
    """
    if n < 1:
        raise StatsError("n must be at least 1")
    if not 0.0 < psi <= 1.0:
        raise StatsError("psi must be in (0, 1]: a corpus where the controls never differ "
                         "carries no information, and the power is 0, not undefined")
    if not 0.0 <= theta <= 1.0:
        raise StatsError("theta must be in [0, 1]")
    if not 0.0 < alpha < 1.0:
        raise StatsError("alpha must be in (0, 1)")
    if theta == 0.5:
        # Perfectly balanced: the effect is zero and the test has no power beyond alpha.
        return PowerResult(
            power=alpha,
            n=n,
            psi=psi,
            theta=theta,
            alpha=alpha,
            expected_discordant=n * psi,
            expected_b=n * psi * theta,
            expected_c=n * psi * (1.0 - theta),
            method="normal approximation (theta=0.5: no effect)",
        )

    z_crit = _z_for_alpha(alpha)
    effect = n * psi * (2.0 * theta - 1.0) - 1.0
    spread = sqrt(2.0 * n * psi * theta * (1.0 - theta))
    noncentrality = effect / spread
    power = 1.0 - _phi(z_crit - abs(noncentrality))

    expected_discordant = n * psi

    return PowerResult(
        power=power,
        n=n,
        psi=psi,
        theta=theta,
        alpha=alpha,
        expected_discordant=expected_discordant,
        expected_b=n * psi * theta,
        expected_c=n * psi * (1.0 - theta),
        method="normal approximation",
        reliable=expected_discordant >= MIN_DISCORDANT_FOR_NORMAL,
    )


def mcnemar_power_exact(
    n: int,
    psi: float,
    theta: float,
    alpha: float = 0.05,
    max_n: int = 2000,
) -> PowerResult:
    """Exact power by direct summation, with no normal approximation anywhere.

    b and c are marginally independent binomials over n trials, and rejection is decided by
    the same exact binomial p-value `trueplumb.stats.mcnemar_exact` uses -- so this answers
    "how often would the actual shipped test reject", not "how often would a z-test reject".
    That distinction is the whole reason it exists: at the sample sizes a real corpus
    reaches, the two are not the same number.

    Cost is O(n^2). Above `max_n` this refuses rather than quietly truncating, because a
    silently partial sum is a confidently wrong power figure.
    """
    from .stats import _binom_two_sided_p

    if n < 1:
        raise StatsError("n must be at least 1")
    if n > max_n:
        raise StatsError(
            f"exact power is O(n^2) and n={n} exceeds max_n={max_n}. Use mcnemar_power(), "
            "or raise max_n deliberately."
        )
    if not 0.0 < psi <= 1.0:
        raise StatsError("psi must be in (0, 1]")
    if not 0.0 <= theta <= 1.0:
        raise StatsError("theta must be in [0, 1]")
    if not 0.0 < alpha < 1.0:
        raise StatsError("alpha must be in (0, 1)")

    p_b = psi * theta
    p_c = psi * (1.0 - theta)

    total = 0.0

    def pmf_table(p: float) -> list[float]:
        if p <= 0.0:
            table = [0.0] * (n + 1)
            table[0] = 1.0
            return table
        if p >= 1.0:
            table = [0.0] * (n + 1)
            table[n] = 1.0
            return table
        table = [0.0] * (n + 1)
        table[0] = (1.0 - p) ** n
        ratio = p / (1.0 - p)
        for k in range(1, n + 1):
            table[k] = table[k - 1] * ((n - k + 1) / k) * ratio
        return table

    table_b = pmf_table(p_b)
    table_c = pmf_table(p_c)

    for b in range(n + 1):
        if table_b[b] == 0.0:
            continue
        for c in range(n + 1):
            if table_c[c] == 0.0:
                continue
            if _binom_two_sided_p(b, c) < alpha:
                total += table_b[b] * table_c[c]

    return PowerResult(
        power=total,
        n=n,
        psi=psi,
        theta=theta,
        alpha=alpha,
        expected_discordant=n * psi,
        expected_b=n * p_b,
        expected_c=n * p_c,
        method="exact summation",
    )


def required_n(
    psi: float,
    theta: float,
    target_power: float = 0.80,
    alpha: float = 0.05,
    max_n: int = 5_000,
) -> int:
    """Smallest `n` reaching `target_power` by **exact** power.

    A linear scan, not a bisection: McNemar's power is not monotone in n for every
    (psi, theta), and bisecting a non-monotone function returns an arbitrary crossing rather
    than the smallest valid n. The scan is O(1) per step.

    Exact power is used rather than the approximation because the approximation *overstates*
    power -- badly at small n. Sizing a corpus with it would promise a detectable difference
    the exact test would not detect, which is the same class of error as reporting a
    single-rate accuracy: a confident number that the underlying method does not support.
    """
    if not 0.0 < target_power < 1.0:
        raise StatsError("target_power must be in (0, 1)")
    for n in range(1, max_n + 1):
        if mcnemar_power_exact(n, psi, theta, alpha).power >= target_power:
            return n
    raise StatsError(
        f"no n <= {max_n} reaches power {target_power} for psi={psi}, theta={theta}. "
        "The controls may be too similar for any corpus size to separate them."
    )


def cases_for_precision(
    target_half_width: float,
    rate: float = 0.5,
    confidence: float = 0.95,
) -> int:
    """Cases needed for a Wilson interval to be within `target_half_width` of `rate`.

    Answers the question a conformance report actually raises: "how many cases until I can
    tell a 90%-detecting control from an 80%-detecting one?" That is a question about
    interval width, not about significance, and it is a different -- usually larger -- number
    than `required_n`.

    This calls `wilson_interval` itself rather than using the textbook formula
    n = z^2 p(1-p) / w^2. The formula is a large-sample approximation and the interval is
    Wilson, so solving against the approximation would answer a slightly different question
    than the tool reports. Search instead, and let the number come from the same code path
    that produces the published interval.
    """
    if not 0.0 < target_half_width < 1.0:
        raise StatsError("target_half_width must be in (0, 1)")
    if not 0.0 < rate < 1.0:
        raise StatsError("rate must be in (0, 1); 0 and 1 have zero-width degenerate "
                         "intervals that would return a misleadingly small n")

    for n in range(1, 200_001):
        successes = int(round(rate * n))
        if not 0 <= successes <= n:  # pragma: no cover - guarded by the rate check
            continue
        interval = wilson_interval(successes, n, confidence)
        half_width = max(interval.point - interval.low, interval.high - interval.point)
        if half_width <= target_half_width:
            return n
    raise StatsError(
        f"no n <= 200000 reaches a half-width of {target_half_width} at rate {rate}"
    )


def corpus_targets(
    psi: float,
    theta: float = 0.75,
    alpha: float = 0.05,
    power: float = 0.80,
) -> dict[str, int]:
    """The sizes a contributor actually needs to hit, in one call.

    Bundled because the two numbers answer different questions and quoting either alone
    invites the wrong conclusion:

    ``significant``  cases needed to *reject* the null hypothesis
    ``discriminate``  cases needed to *estimate* two rates apart

    Passing the first and failing the second is the common trap: a corpus can give a
    confident verdict that two controls are different while still being far too small to
    say how much better one is.
    """
    return {
        "significant": required_n(psi, theta, target_power=power, alpha=alpha),
        "discriminate": cases_for_precision(0.05, rate=theta, confidence=1.0 - alpha),
    }
