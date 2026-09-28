"""Deterministic statistics for conformance scoring.

Everything here is closed-form arithmetic over counts. No sampling, no RNG, no scipy, and
no LLM. That is a requirement of the design rule, not a preference: a confidence interval
computed from a bootstrap is only reproducible if the seed is fixed, and a seed is exactly
the kind of thing that silently differs between a developer's laptop and a CI runner. The
formulas below have no such hidden state.

Two tests are provided, and they answer different questions:

`wilson_interval`  "how precise is this pass rate?" -- the uncertainty around one
                    control's detection or false-positive rate.

`mcnemar_exact`    "is control A actually better than control B?" -- a paired test. Both
                    controls see the same corpus, so the comparison is paired and
                    McNemar is the right tool; an unpaired comparison would throw away the
                    correlation and need far more data to reach the same conclusion.

Both are small enough to read and check by hand, which is the point. A reader who does not
trust the result can recompute it.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import comb, sqrt

# Two-sided normal quantiles. A table beats an inverse-CDF implementation here because the
# values are used for display thresholds, not for arbitrary quantiles, and a wrong tail
# probability in an inverse normal function is very hard to notice.
Z_SCORES: dict[float, float] = {
    0.80: 1.2815515655446004,
    0.90: 1.6448536269514722,
    0.95: 1.959963984540054,
    0.99: 2.5758293035489004,
}


class StatsError(ValueError):
    """Invalid input to a statistical routine."""


@dataclass(frozen=True, slots=True)
class Interval:
    """A point estimate with a confidence interval around it."""

    point: float
    low: float
    high: float
    successes: int
    trials: int
    confidence: float

    def contains(self, value: float) -> bool:
        return self.low <= value <= self.high

    def as_dict(self) -> dict[str, float | int]:
        return {
            "point": self.point,
            "low": self.low,
            "high": self.high,
            "successes": self.successes,
            "trials": self.trials,
            "confidence": self.confidence,
        }

    def render(self) -> str:
        """Human-readable form, with a visible note when the interval is degenerate.

        Zero successes or zero trials collapses the interval to a point. Printing
        `100.0% [100.0%, 100.0%]` for 0/0 would be a lie that reads like certainty, so the
        caller is told instead.
        """
        if self.trials == 0:
            return "n/a (no trials)"
        if self.successes == 0 or self.successes == self.trials:
            edge = "0/0" if self.successes == 0 else f"{self.trials}/{self.trials}"
            return (
                f"{self.point:.1%} [{self.low:.1%}, {self.high:.1%}] "
                f"({self.confidence:.0%} CI, degenerate at {edge})"
            )
        return f"{self.point:.1%} [{self.low:.1%}, {self.high:.1%}] ({self.confidence:.0%} CI)"


def wilson_interval(
    successes: int,
    trials: int,
    confidence: float = 0.95,
) -> Interval:
    """Wilson score interval for a binomial proportion.

    Preferred over the normal (Wald) interval because Wald is badly wrong exactly where
    conformance results live: at rates near 0 and 1, and at small n. Wald can return a
    lower bound below zero or an upper bound above 1, and it under-covers precisely when a
    control passes 20 of 20 attacks -- which is the case a vendor will be most eager to
    quote. Wilson stays inside [0, 1] and keeps sensible width.

        centre = (p + z^2/2n) / (1 + z^2/n)
        halfwidth = z/(1 + z^2/n) * sqrt( p(1-p)/n + z^2/4n^2 )
    """
    if trials < 0:
        raise StatsError("trials must be non-negative")
    if not 0 <= successes <= trials:
        raise StatsError(f"{successes} successes out of {trials} trials is impossible")
    if confidence not in Z_SCORES:
        raise StatsError(
            f"confidence {confidence} has no tabulated z; use one of {sorted(Z_SCORES)}"
        )

    if trials == 0:
        return Interval(0.0, 0.0, 1.0, successes, trials, confidence)

    z = Z_SCORES[confidence]
    n = float(trials)
    p = successes / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / denom
    spread = (z / denom) * sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))

    low = max(0.0, centre - spread)
    high = min(1.0, centre + spread)

    # The clamp alone is not enough: at 10/10 the arithmetic yields 0.9999999999999999, so
    # an exact point estimate of 1.0 would sit marginally outside its own interval. Snapping
    # to the endpoints when the count is degenerate is correct rather than cosmetic -- the
    # bound is exactly 1.0 when every trial succeeded, and letting float error shave it
    # makes `contains(point)` false for a result that is mathematically true.
    if successes == trials:
        high = 1.0
    if successes == 0:
        low = 0.0

    return Interval(
        point=p,
        low=low,
        high=high,
        successes=successes,
        trials=trials,
        confidence=confidence,
    )


def _binom_two_sided_p(b: int, c: int) -> float:
    """Exact two-sided McNemar p-value, binomial formulation.

    Under H0 the discordant pairs split evenly, so `b + c ~ Binomial(b + c, 0.5)`. The
    two-sided p-value doubles the smaller tail and is capped at 1.0, which is what keeps it
    from exceeding the maximum possible value when the tails are unbalanced.
    """
    n = b + c
    if n == 0:
        return 1.0  # no discordant pairs: the controls agreed everywhere
    k = min(b, c)
    tail = sum(comb(n, i) for i in range(k + 1)) / (2.0**n)
    return min(1.0, 2.0 * tail)


@dataclass(frozen=True, slots=True)
class PairedResult:
    """Outcome of comparing two controls on the same corpus."""

    b: int
    """Cases where control A caught it and control B did not."""

    c: int
    """Cases where control B caught it and control A did not."""

    p_value: float
    significant: bool
    confidence: float
    n_discordant: int
    n_total: int

    def verdict(self, alpha: float = 0.05) -> str:
        """A sentence, not just a number.

        A bare p-value forces the reader to know the convention. Stating "no detectable
        difference" is the honest reading of a non-significant result, and it is the one
        most likely to be quietly omitted from a vendor comparison.
        """
        if self.n_discordant == 0:
            return f"identical on all {self.n_total} cases"
        if self.significant:
            if self.b > self.c:
                return f"control A better on {self.b} cases, B on {self.c} (p={self.p_value:.4f})"
            return f"control B better on {self.c} cases, A on {self.b} (p={self.p_value:.4f})"
        return (
            f"no detectable difference: A better on {self.b}, B on {self.c}, p={self.p_value:.4f}"
        )

    def as_dict(self) -> dict[str, float | int | bool]:
        return {
            "b": self.b,
            "c": self.c,
            "p_value": self.p_value,
            "significant": self.significant,
            "confidence": self.confidence,
            "n_discordant": self.n_discordant,
            "n_total": self.n_total,
        }


def mcnemar_exact(
    a_only: int,
    b_only: int,
    n_total: int = 0,
    alpha: float = 0.05,
) -> PairedResult:
    """Exact McNemar test on two controls run over the same corpus.

    `a_only` counts cases A got right and B got wrong; `b_only` counts the reverse. The
    exact binomial formulation is used rather than the chi-square approximation because
    discordant counts in a security corpus are routinely in single digits, where the
    approximation is not trustworthy.
    """
    if a_only < 0 or b_only < 0:
        raise StatsError("discordant counts must be non-negative")
    if a_only + b_only > n_total > 0:
        raise StatsError("discordant counts exceed the number of cases")

    p = _binom_two_sided_p(a_only, b_only)
    return PairedResult(
        b=a_only,
        c=b_only,
        p_value=p,
        significant=p < alpha,
        confidence=1.0 - alpha,
        n_discordant=a_only + b_only,
        n_total=n_total,
    )


def rate(successes: int, trials: int) -> float:
    """A rate with an explicit zero case.

    Returns 0.0 for 0/0 rather than raising, because a category with no cases is a fact
    about the corpus, not an error. The scoring report distinguishes the two by printing
    `n/a` alongside, so this is a convenience rather than a silent fabrication.
    """
    if trials == 0:
        return 0.0
    return successes / trials
