# Golden set scorecard: gemini-3.1-flash-lite vs gemini-3.5-flash-lite as the primary model

Date: 2026-10-30. 20 claims (19 scored, GOLD-20 observed only) plus 3 fairness pairs, US auto_collision, `fraud_crew` mocked.
The judge is `gemini-3.5-flash-lite` in the current config; in the 3.5 rows it is also the primary (the old configuration).
Raw data: `data/batch_results/2026-10-30_golden_set_<model>[_run1].json`.

| | 3.1 run 2 (final code) | 3.5 run 2 (final code) | 3.1 run 1 | 3.5 run 1 |
|---|---|---|---|---|
| Behaviour checks passed (tier 1) | **19 / 19** (CI 83-100%) | 18 / 19 (CI 75-99%) | 18 / 19 | 16 / 19 (CI 62-94%) |
| Payout equals independent oracle (tier 2) | 12 / 12 | 11 / 11 | 11 / 11 | 9 / 9 |
| Critical errors (paid what should have stopped) | 0 | 0 | 0 | 0 |
| Over-escalations (paused when it could settle) | 0 | 1 | 1 | 2 |
| Pause-routing precision / recall | 1.00 / 1.00 | 0.90 / 1.00 | 0.90 / 1.00 | 0.82 / 1.00 |
| Fairness pairs consistent | **3 / 3** | 2 / 3 | 1 / 3 (label noise, since fixed) | 2 / 3 |
| Claimant letters replaced by the accuracy backstop | 0 of 12 | 0 of 11 | not recorded | not recorded |
| Cost per claim (mean) | $0.0049 | $0.0052 | $0.0048 | $0.0050 |
| Time per claim (mean / p95) | 29 s / 72 s | 24 s / 73 s | 34 s / 102 s | 24 s / 57 s |
| Model that answered | 3.1 only (110 calls) | 3.5 only (109 calls) | 3.1 only | 3.5 only |
| Gate | PASSED | failed (pairs 2/3) | failed | failed |

## What the runs show
- **Both models are good and statistically close at this sample size.** The 95% intervals overlap almost completely, so this is evidence, not proof, that 3.1 is at least as good. It is not evidence that 3.1 is better.
- **3.1 was steadier.** It never raised a spurious "physical inspection" pause; 3.5 did twice in run 2 (GOLD-03 and one fairness-pair claim), where the damage assessor asked for an inspection on a clean repair claim.
- **Repeatability (3.1, run 1 vs run 2):** 19 of 20 claims gave the identical outcome and payout to the cent. The one difference, GOLD-14, was the intended effect of the policy-checker prompt fix.
- **The GOLD-14 fix worked live.** A missing police report alone paused the claim on both models in run 1; on 3.1 in run 2 it settled ($4,876).
- **No dangerous errors in any of the four runs.** Nothing was paid that should have been stopped.
- **The letter backstop never had to fire** (0 of 23 letters), so the model's own letters matched the decision record every time on this set. That is also a clean false-positive check on the backstop.

## Decision
Keep `gemini-3.1-flash-lite` as the primary and `gemini-3.5-flash-lite` as the judge and fallback.

## Caveats
- 19 scored claims is a small sample; the set was written by us, with Claude-proxy labels for the judgment calls (independently reviewed, see `REVIEW.md`).
- The 3.1 run 2 spans a machine sleep: claims 1-20 ran before it, the fairness pairs after. A crashed pair claim was detected, discarded and re-run.
- `fraud_crew` is mocked, so no fraud-catch behaviour is measured here.
- The judge in the 3.5 rows is the same model as the primary; in the 3.1 rows it is a different one.
