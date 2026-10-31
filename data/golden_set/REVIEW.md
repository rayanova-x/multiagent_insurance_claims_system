# Golden set: label provenance

All labels are either code-deterministic (outcome fixed by a hard rule in code) or rule-text (a judgment call
checked against the written exclusion criteria in `src/agents/policy_checker.py`). None is human-adjuster ground
truth: the rule-text labels are LLM-proxy labels that were independently reviewed, as described below.

## Blind review of the judgment calls (GOLD-08 to GOLD-12)
Fresh-context reviewers saw only the exclusion criteria text and shuffled narratives, with no labels, no category
names, and decoys with known answers. They were told to apply the criteria strictly and not to assume.

| Round | Reviewer | Claims | Agreement with our label |
|---|---|---|---|
| 1 | A (order 1) | 5 golden + 5 decoys | 10/10 |
| 1 | B (reversed order) | 5 golden + 5 decoys | 10/10 |
| 2 | C, after rewriting GOLD-09/11/12 | 3 rewritten + 3 decoys | 6/6 |

Round 1 also rated realism. Both reviewers called the original GOLD-09 and GOLD-12 keyword-stuffed, and one called
GOLD-11 that. Those three were rewritten to remove explicit "negative criteria" phrasing (e.g. "no timing or
ranking", "the shop notes the earlier dent") and re-reviewed blind in round 2; the verdicts did not change.

## Residual caveats
- The rewritten GOLD-09 (near-miss racing) and GOLD-12 (pre-existing true positive) were still rated
  "keyword-stuffed" by the round 2 reviewer. A near-miss or true positive must state its deciding fact, so some
  stiffness is inherent. Treat them as demo-quality, not as a representative sample of real claimant prose.
- GOLD-10 and GOLD-08 were rated "stilted" by one reviewer each, "realistic" by the other. Left unchanged.
- Reviewer disagreement risk that remains: GOLD-12 (does the exclusion apply to the whole claim or only the old dent?
  The system pauses for review either way, which is what the golden set expects) and the decoy about spontaneous
  racing without a stated agreement (not part of the scored set).
- GOLD-20 is observed-only: whether a live judge raises the cross-branch conflict is its own judgment call.
