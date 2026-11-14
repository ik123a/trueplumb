"""Power analysis tests.

The point of this module is to stop "the corpus is too small" from being an unfalsifiable
hand-wave, so the tests check the numbers against an independent computation rather than
against themselves.

The exact path is validated by brute-force enumeration with explicit `comb()` terms. If both
share a helper, both could be wrong together and the tests would still pass.
"""

from __future__ import annotations

from math import comb

import pytest

from trueplumb.power import (
    MIN_DISCORDANT_FOR_NORMAL,
    corpus_targets,
    mcnemar_power,
    mcnemar_power_exact,
    required_n,
    cases_for_precision,
)
from trueplumb.stats import StatsError, _binom_two_sided_p


def brute_force_power(n: int, psi: float, theta: float, alpha: float = 0.05) -> float:
    """Exact power by direct enumeration, sharing no code with `mcnemar_power_exact`.

    Deliberately the slow, obvious way: explicit `comb()`, no pmf recurrence. It is the
    independent opinion that makes the fast version trustworthy.
    """
    p_b = psi * theta
    p_c = psi * (1.0 - theta)
    total = 0.0
    for b in range(n + 1):
        w_b = comb(n, b) * (p_b**b) * ((1.0 - p_b) ** (n - b))
        if w_b == 0.0:
            continue
        for c in range(n + 1):
            w_c = comb(n, c) * (p_c**c) * ((1.0 - p_c) ** (n - c))
            if _binom_two_sided_p(b, c) < alpha:
                total += w_b * w_c
    return total


class TestExactPowerIsActuallyExact:
    @pytest.mark.parametrize(
        ("n", "psi", "theta"),
        [
            (10, 0.5, 0.8),
            (15, 0.5, 0.8),
            (20, 0.5, 0.8),
            (25, 0.6, 0.7),
            (30, 0.5, 0.8),
            (12, 0.8, 0.9),
        ],
    )
    def test_matches_brute_force(self, n: int, psi: float, theta: float) -> None:
        exact = mcnemar_power_exact(n, psi, theta).power
        assert exact == pytest.approx(brute_force_power(n, psi, theta), abs=1e-9)

    def test_no_effect_gives_alpha(self) -> None:
        # theta=0.5 means the controls are balanced among the cases where they differ, so
        # there is nothing to detect and the test rejects only at chance.
        assert mcnemar_power_exact(40, 0.5, 0.5).power == pytest.approx(0.05, abs=1e-9)

    def test_power_is_in_probability_range(self) -> None:
        for n in (5, 10, 25):
            for psi in (0.2, 0.5, 0.9):
                for theta in (0.1, 0.5, 0.9):
                    power = mcnemar_power_exact(n, psi, theta).power
                    assert 0.0 <= power <= 1.0

    def test_power_grows_with_n(self) -> None:
        # Monotone in n for a fixed real effect. A drop here would mean the summation is
        # wrong, since more data cannot reduce power.
        powers = [mcnemar_power_exact(n, 0.5, 0.8).power for n in (10, 15, 20, 30, 40)]
        assert powers == sorted(powers)

    def test_perfect_discordance_gives_certainty(self) -> None:
        # psi=1: the controls disagree on every case, so a real effect is certain to show.
        assert mcnemar_power_exact(10, 1.0, 0.9).power > 0.95

    def test_refuses_an_impractically_large_n(self) -> None:
        # Refusing beats silently truncating a partial sum and printing it as a power figure.
        with pytest.raises(StatsError, match="exceeds max_n"):
            mcnemar_power_exact(5000, 0.5, 0.8)


class TestTheApproximationIsLabelledLoudly:
    """The normal approximation overstates power. The code must say so, not the docs."""

    def test_it_overstates_at_small_n(self) -> None:
        # Measured, not asserted from a paper: at n=20 the approximation claims 0.80 power
        # where the exact answer is 0.35. Anyone sizing a corpus on that number would be
        # promised evidence that does not exist.
        approximation = mcnemar_power(20, 0.5, 0.8)
        exact = mcnemar_power_exact(20, 0.5, 0.8)
        assert approximation.power - exact.power > 0.4

    def test_it_is_flagged_unreliable_below_the_threshold(self) -> None:
        assert not mcnemar_power(20, 0.5, 0.8).reliable
        assert not mcnemar_power(40, 0.5, 0.5).reliable

    def test_it_is_flagged_reliable_above_the_threshold(self) -> None:
        assert mcnemar_power(200, 0.5, 0.8).reliable

    def test_the_threshold_is_stated_in_discordant_pairs(self) -> None:
        # The error tracks discordant count, not n. High psi at low n is still reliable;
        # low psi at high n is not.
        assert mcnemar_power(50, 0.9, 0.8).reliable, "45 discordant pairs"
        assert not mcnemar_power(120, 0.2, 0.8).reliable, "24 discordant pairs"
        assert MIN_DISCORDANT_FOR_NORMAL == 40

    def test_render_says_when_a_figure_is_optimistic(self) -> None:
        text = mcnemar_power(15, 0.5, 0.8).render()
        assert "unreliable" in text
        assert str(MIN_DISCORDANT_FOR_NORMAL) in text

    def test_exact_results_are_always_reliable(self) -> None:
        for n in (5, 15, 40):
            assert mcnemar_power_exact(n, 0.5, 0.8).reliable

    def test_it_converges_to_the_exact_answer(self) -> None:
        # The reason a threshold exists at all: at 40 discordant pairs the two agree.
        approximation = mcnemar_power(80, 0.5, 0.8)
        exact = mcnemar_power_exact(80, 0.5, 0.8)
        assert abs(approximation.power - exact.power) <= 0.05


class TestRequiredN:
    def test_answers_the_question_the_docs_kept_avoiding(self) -> None:
        # 80% power to detect a control that wins 80% of the cases where the two differ.
        n = required_n(0.5, 0.8)
        assert 35 <= n <= 60, f"expected roughly 43, got {n}"

    def test_the_answer_actually_reaches_the_target(self) -> None:
        n = required_n(0.5, 0.8, target_power=0.80)
        assert mcnemar_power_exact(n, 0.5, 0.8).power >= 0.80

    def test_one_less_does_not_reach_it(self) -> None:
        # Otherwise the answer is not minimal and nobody can trust it as a target.
        n = required_n(0.5, 0.8, target_power=0.80)
        assert mcnemar_power_exact(n - 1, 0.5, 0.8).power < 0.80

    def test_low_discordance_needs_a_bigger_corpus(self) -> None:
        # psi is the parameter people forget: two controls that rarely disagree cannot be
        # separated by any corpus size, because only disagreeing cases carry information.
        assert required_n(0.2, 0.8) > required_n(0.8, 0.8)

    def test_a_closer_pair_needs_a_bigger_corpus(self) -> None:
        assert required_n(0.5, 0.6) > required_n(0.5, 0.9)

    def test_the_current_corpus_is_underpowered(self) -> None:
        # The number the project docs kept asserting without evidence. 15 violation cases
        # gives roughly a fifth of the power needed, so most real differences are invisible.
        power = mcnemar_power_exact(15, 0.5, 0.8).power
        assert power < 0.25, f"expected roughly 0.21, got {power}"

    def test_refuses_an_unreachable_target(self) -> None:
        with pytest.raises(StatsError, match="too similar|no n"):
            required_n(0.001, 0.5, target_power=0.8, max_n=50)


class TestCasesForPrecision:
    def test_known_values(self) -> None:
        # Textbook sample sizes for +/-5% at 95% confidence, near a 50% rate.
        assert cases_for_precision(0.05, rate=0.5) == pytest.approx(385, abs=10)
        assert cases_for_precision(0.05, rate=0.9) == pytest.approx(194, abs=10)

    def test_the_answer_actually_reaches_the_target(self) -> None:
        n = cases_for_precision(0.05, rate=0.8)
        from trueplumb.stats import wilson_interval

        interval = wilson_interval(int(round(0.8 * n)), n)
        assert max(interval.point - interval.low, interval.high - interval.point) <= 0.05

    def test_a_tighter_target_needs_more_cases(self) -> None:
        assert cases_for_precision(0.02, rate=0.8) > cases_for_precision(0.05, rate=0.8)

    def test_a_rate_near_one_needs_fewer_cases(self) -> None:
        # The asymmetry a single-rate report hides: 90% is estimated more precisely than
        # 50% from the same number of cases.
        assert cases_for_precision(0.05, rate=0.9) < cases_for_precision(0.05, rate=0.5)

    def test_rejects_degenerate_rates(self) -> None:
        # At rate 0 or 1 the interval collapses and would return a misleadingly small n.
        for rate in (0.0, 1.0):
            with pytest.raises(StatsError, match="degenerate"):
                cases_for_precision(0.05, rate=rate)


class TestCorpusTargets:
    def test_answers_both_questions(self) -> None:
        targets = corpus_targets(0.5, 0.75)
        assert targets["significant"] < targets["discriminate"]

    def test_the_two_numbers_are_not_interchangeable(self) -> None:
        # The trap this exists to prevent: a corpus can prove two controls differ while
        # still being far too small to say by how much. Quoting either number alone
        # invites the wrong conclusion.
        targets = corpus_targets(0.5, 0.75)
        assert targets["significant"] >= 40
        assert targets["discriminate"] >= 300


class TestRejectsNonsenseInput:
    @pytest.mark.parametrize("n", [0, -1])
    def test_non_positive_n(self, n: int) -> None:
        with pytest.raises(StatsError, match="at least 1"):
            mcnemar_power(n, 0.5, 0.8)
        with pytest.raises(StatsError, match="at least 1"):
            mcnemar_power_exact(n, 0.5, 0.8)

    def test_zero_psi_is_rejected_rather_than_returning_zero(self) -> None:
        # Controls that never disagree carry no information, and saying "power 0" would be
        # a different and wrong claim about what a large corpus would achieve.
        for fn in (mcnemar_power, mcnemar_power_exact):
            with pytest.raises(StatsError, match="no information"):
                fn(50, 0.0, 0.8)

    @pytest.mark.parametrize("theta", [-0.1, 1.1])
    def test_theta_out_of_range(self, theta: float) -> None:
        with pytest.raises(StatsError, match="theta"):
            mcnemar_power(50, 0.5, theta)

    @pytest.mark.parametrize("alpha", [0.0, 1.0, -0.1])
    def test_alpha_out_of_range(self, alpha: float) -> None:
        with pytest.raises(StatsError, match="alpha"):
            mcnemar_power(50, 0.5, 0.8, alpha)

    def test_rejects_an_untolerable_confidence(self) -> None:
        with pytest.raises(StatsError, match="outside the tabulated range"):
            mcnemar_power(50, 0.5, 0.8, alpha=1e-6)

    def test_is_deterministic(self) -> None:
        # Same rule as the rest of the statistics: no sampling, so no seed, so no drift.
        a = mcnemar_power_exact(20, 0.5, 0.8).power
        b = mcnemar_power_exact(20, 0.5, 0.8).power
        assert a == b
