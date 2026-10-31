# System card: Smart Claims Processor (demo)

Status: demo build, 2026-10-30. Not validated for production use. All claims used in testing are synthetic.

## Intended use
Assist a claims adjuster with **US auto-collision claims**: validate a claim, assess damage, check coverage, propose a settlement, and write the claimant letter. The system recommends; a human decides on anything risky. It is not a substitute for an adjuster and makes no coverage or fraud determination on its own authority.

## How it works (one paragraph)
A LangGraph workflow. Intake validates the claim in code. Damage assessment, policy checking and fraud review then run as three **parallel, mutually isolated** branches; a consistency check compares their outputs and can send one branch back for another pass (at most twice). Settlement, evaluation and the claimant letter follow. Any risky situation pauses the claim for a human, who approves or denies; the claim then resumes where it paused. **Code decides routing and every number that moves money; the model only judges** (exclusions, damage plausibility, conflict between branches, tone and wording).

## What is enforced in code, not left to the model
- Claim validity, policy active, deductible, coverage limit, depreciation, the payout amount, the approved / partially-approved label.
- Total loss at repair cost >= 75% of the vehicle's value; a total loss pays the vehicle's value (no second depreciation).
- A repair assessment more than 40% away from the repair-estimate total is pulled back; no repair estimate means a physical inspection.
- Fraud score >= 0.80 denies unless a human already approved.
- Per-claim budgets for calls, tokens, cost and time; a hit sends the claim to a human.
- The claimant letter must state the correct amount and decision, and may not mention fraud; otherwise a template is sent.
- Vehicle age is measured at the incident date.

## Human review triggers (regardless of the model's confidence)
Prompt-injection text; a disputed incident type; a policy exclusion or compliance flag; a physical inspection needed; fraud score over threshold; an unresolved material conflict between branches; claim amount >= $84,100; the evaluator judge failing or unable to score; any budget halt or LLM outage.

## Evidence (demo golden set, `data/golden_set/`, scorecard in `SCORECARD.md`)
- 19 scored claims on the primary model `gemini-3.1-flash-lite`: **19/19 behaviour checks, 12/12 payouts match an independent calculation, 0 dangerous errors** (nothing paid that should have been stopped). 95% interval for the behaviour pass rate: 83-100%.
- 3 of 3 fairness pairs (same claim, different claimant name and city) gave identical outcome and payout.
- Repeatability: 19 of 20 claims identical between two runs (the one difference was an intentional prompt fix).
- Cost about $0.005 per claim; about 30 s mean, 72 s p95 (active time, excluding human review).
- The AI graders (fairness, transparency, consistency check) agree with our labels on 12 of 14 cases (86%).

## Known limits and honest caveats
- **Small, self-built test set.** 19 claims written by us; the judgment-call labels are Claude-written (independently blind-reviewed, 21/21 agreement), not human adjuster ground truth. The grader agreement (86%) is below the 90% bar the evaluation literature suggests, and is measured against those same proxy labels.
- **Fraud detection is not evaluated.** The fraud crew is mocked in every test run; nothing here shows it catches fraud.
- **No real document intake.** Claim documents are supplied as structured test data. Real upload and extraction are not built, so on real submissions most claims will pause for a physical inspection.
- **Single line, single country.** US auto-collision only. Other claim types and India are in the code but not validated.
- **Models.** Agents run on `gemini-3.1-flash-lite`; the evaluator's graders and the fallback use `gemini-3.5-flash-lite`. Same vendor and family, so grader self-preference bias is reduced, not removed. Free-tier quotas apply.
- **Uncalibrated thresholds.** Confidence-gate thresholds are reasonable defaults, not calibrated. The $84,100 review threshold comes from one public dataset's 90th percentile.
- **Synthetic claims only.** Behaviour on real claimants, real documents and real volumes is untested; no live traffic, drift monitoring or A/B testing exists.

## Recourse and oversight
Claimants can appeal a decision, which always goes to a human adjuster. Every decision records the model, prompt hashes and configuration in force, and the audit log is hash-chained.
