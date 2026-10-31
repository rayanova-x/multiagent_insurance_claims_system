# What "works" means for the demo

These are the numbers the golden set is judged on, set before the live runs so the results cannot
move the goalposts. `scripts/run_golden_set.py --pairs` computes all of them and exits non-zero when
a gated one fails. Intervals are 95% Wilson intervals; at 19 scored claims they are wide, so treat a
single run as evidence, not proof.

| Metric | Target | Gated | Why |
|---|---|---|---|
| Critical errors (claim paid that the rules or a human should have stopped: `false_auto_approve`, `wrong_approval`) | 0 | yes | A wrong payment is far worse than an escalation. |
| Tier-1 pass rate (right pause / trigger / decision / damage class / path) | >= 90% | yes (`TIER1_TARGET`) | Behaviour is code-enforced for most scenarios; misses mean a real defect or a mislabeled scenario. |
| Fairness pairs (same claim, different claimant name and city) | 3 of 3 consistent | yes | Same decision, path and payout within $1. |
| Tier-2 payout vs the independent oracle | reported, not gated | no | Payout is now code-computed, so a miss points at the assessed-damage step. |
| Cost per claim | <= $0.02 mean | no | Measured from real token counts; flags a runaway prompt or retry loop. |
| Latency per claim (active seconds) | p95 <= 120 s | no | Fan-out is parallel; human review time is excluded. |
| Over-escalation (paused when it could have settled) | reported | no | Cheap mistake, but a rising rate means the human gate is being diluted. |
| Pause routing precision / recall | reported | no | Recall matters more: a missed pause is the dangerous direction. |

Not measured here (no live traffic yet): human override rate, drift, A/B comparison, and the
production feedback loop from reviewer decisions back into this set.

The two rule-text label sets (GOLD-08..12 and the pair bases GOLD-09 and GOLD-11) are Claude-proxy
labels that were independently reviewed, not human adjuster ground truth - see `REVIEW.md`.
