"""Golden set for validating this project's three LLM-as-judge calls against
ground truth: consistency_coordinator's conflict judge
(src/agents/consistency_coordinator.py), and evaluator's fairness and
transparency dimensions (src/evaluation/evaluator.py).

Per the baswe.ai evaluation field guide ss3.5 ("validate the judge, non
negotiable") - a judge is itself a system that must be evaluated against
ground truth before its scores are trusted. This project could not find an
honest source of real human-adjuster ground truth for FAIRNESS or
TRANSPARENCY (these are judgments about THIS pipeline's own output, not a
property of any public claims dataset - no external source labels "was this
settlement fair"). Per explicit user decision, Claude is used as a
judgment-proxy labeler for all three judges, with this limitation stated
plainly wherever these labels are used: these are LLM-proxy labels, not
human-validated ground truth, and the resulting agreement numbers mean less
than what the field guide's own bar calls for. Revisit with real human
(adjuster) labels if that ever becomes available.

CONSISTENCY_COORDINATOR cases ARE partly grounded in real, non-proxy ground
truth where possible: every case drawn from data/stratified_claims/ carries
the real `_fraud_reported_ground_truth` field (sourced from the actual
Kaggle/Mendeley insurance_claims.csv `fraud_reported` column), used here
only to judge whether a HYPOTHETICAL constructed conflict between two
branch outputs is the kind of conflict that would matter given what's
really known about that claim - not as a label for the judge's literal
conflict/no-conflict call itself (the conflict scenarios are constructed,
not what the real claim's damage/policy/fraud branches would actually
produce).

Cases are risk-stratified to mirror this project's own real selection
categories (src/data/selection.py): clean, fraud-flagged, high-value
clean, high-value fraud, missing-document, policy-exclusion - so the
golden set's own case mix isn't arbitrary, it tracks the same real
population strata the rest of this project was calibrated against.
"""

from __future__ import annotations

GOLDEN_SET: list[dict] = [

    # ══════════════════════════════════════════════════════════════════════
    # CONSISTENCY COORDINATOR - conflict judge (has_conflicts, type, severity)
    # ══════════════════════════════════════════════════════════════════════
    # Each case gives the coordinator a constructed damage_output/policy_
    # output/fraud_output triple and asks: does it correctly detect the
    # conflict (or correctly detect NONE)? Drawn from real stratified claims
    # where noted, with real fraud_reported ground truth used to sanity-check
    # the constructed scenario's plausibility, not as the judge's label.

    {
        "case_id": "CC-01",
        "judge": "consistency_coordinator",
        "source_claim": "CLM-STRAT-001 (real, clean, fraud_reported=N)",
        "risk_stratum": "clean",
        "inputs": {
            "claim_amount": 34650.0,
            "damage_output": {
                "assessed_damage_usd": 23100.0, "repair_vs_replace": "repair",
                "assessment_notes": "Minor damage, three-vehicle collision, repair cost consistent with description.",
            },
            "policy_output": {
                "coverage_status": "covered", "exclusions_triggered": [],
                "coverage_notes": "Standard collision coverage applies, no exclusions.",
            },
            "fraud_output": {
                "fraud_risk_level": "low", "fraud_score": 0.08,
                "crew_summary": "No anomalies. Multi-vehicle collision consistent with police report and repair estimate.",
            },
        },
        "proxy_label": {"has_conflicts": False, "conflict_type": None, "severity": None},
        "proxy_reasoning": (
            "All three branches independently describe the same minor, routine multi-vehicle "
            "collision with no contradicting facts. Real fraud_reported=N for this claim is "
            "consistent with the fraud branch's own low score - nothing here should read as a "
            "missed fact or a severity disagreement. A correct judge finds no conflict."
        ),
    },
    {
        "case_id": "CC-02",
        "judge": "consistency_coordinator",
        "source_claim": "CLM-STRAT-024 (real, fraud_flagged, fraud_reported=Y)",
        "risk_stratum": "fraud_flagged",
        "inputs": {
            "claim_amount": 46200.0,
            "damage_output": {
                "assessed_damage_usd": 33600.0, "repair_vs_replace": "total_loss",
                "assessment_notes": "Single-vehicle collision, total loss per repair-to-ACV ratio.",
            },
            "policy_output": {
                "coverage_status": "covered", "exclusions_triggered": [],
                "coverage_notes": "No exclusions triggered by the stated facts.",
            },
            "fraud_output": {
                "fraud_risk_level": "high", "fraud_score": 0.78,
                "crew_summary": "Single-vehicle total-loss claim with no witnesses or third party; "
                                 "anomaly detector flags amount significantly above baseline for this claimant's history.",
            },
        },
        "proxy_label": {"has_conflicts": False, "conflict_type": None, "severity": None},
        "proxy_reasoning": (
            "The fraud branch's HIGH risk read and the damage branch's total_loss read are not "
            "in conflict with each other - they're independently consistent with the same single-"
            "vehicle, no-witness scenario (real fraud_reported=Y for this claim supports that a "
            "high fraud score here is a correct read, not a false alarm the coordinator should "
            "second-guess). SEVERITY_MISMATCH would require damage and fraud to disagree about "
            "how severe the incident itself was - they don't; they agree it's severe, and "
            "separately the fraud branch is suspicious of why. A correct judge finds no conflict "
            "(the fraud suspicion itself is fraud_crew's job to flag via its own score/HITL "
            "trigger, not something consistency_coordinator should relabel as a cross-branch "
            "conflict)."
        ),
    },
    {
        "case_id": "CC-03",
        "judge": "consistency_coordinator",
        "source_claim": "constructed (severity mismatch, same shape as SC-10 fixture)",
        "risk_stratum": "clean",
        "inputs": {
            "claim_amount": 38000.0,
            "damage_output": {
                "assessed_damage_usd": 20000.0, "repair_vs_replace": "total_loss",
                "assessment_notes": "Repair-to-ACV ratio of 190% forces total_loss classification per hard rule, "
                                     "despite narrative describing this as minor.",
            },
            "policy_output": {
                "coverage_status": "covered", "exclusions_triggered": [],
                "coverage_notes": "Standard coverage, no exclusions.",
            },
            "fraud_output": {
                "fraud_risk_level": "low", "fraud_score": 0.15,
                "crew_summary": "Narrative-consistency check: claimant describes 'just a small bump, "
                                 "nothing serious at all' - this reads as a low-severity incident.",
            },
        },
        "proxy_label": {"has_conflicts": True, "conflict_type": "severity_mismatch", "severity": "material"},
        "proxy_reasoning": (
            "damage_assessor's own total_loss conclusion (code-forced by the repair-to-ACV rule) "
            "directly contradicts fraud_crew's independent narrative-consistency read of the same "
            "incident as low-severity/minor. This is exactly the real severity-mismatch scenario "
            "this project's own SC-10 fixture was built around. The disagreement is MATERIAL: it "
            "could affect the fraud classification (why does the claimant describe a $38k total-"
            "loss collision as 'just a small bump'?) even though it doesn't change the payout "
            "itself. A correct judge flags this as a MATERIAL severity_mismatch, retry target "
            "damage_assessor or policy_checker depending on which agent's read should be "
            "reconsidered with the other's context."
        ),
    },
    {
        "case_id": "CC-04",
        "judge": "consistency_coordinator",
        "source_claim": "CLM-STRAT-045 (real, policy_exclusion, commercial_use)",
        "risk_stratum": "policy_exclusion",
        "inputs": {
            "claim_amount": 81800.0,
            "damage_output": {
                "assessed_damage_usd": 57260.0, "repair_vs_replace": "repair",
                "assessment_notes": "Multi-vehicle collision, minor damage. Claimant mentioned they were "
                                     "making a delivery at the time of the incident.",
            },
            "policy_output": {
                "coverage_status": "covered", "exclusions_triggered": [],
                "coverage_notes": "No stated facts in the incident description trigger an exclusion.",
            },
            "fraud_output": {
                "fraud_risk_level": "low", "fraud_score": 0.12,
                "crew_summary": "Narrative consistent, no anomalies detected.",
            },
        },
        "proxy_label": {"has_conflicts": True, "conflict_type": "missed_fact", "severity": "material"},
        "proxy_reasoning": (
            "damage_assessor's own notes surface that the claimant was making a delivery at the "
            "time of the incident - a fact that matches this policy's real commercial_use "
            "exclusion criterion exactly (transporting goods/people FOR PAYMENT), but "
            "policy_checker's own coverage_notes show no awareness of it ('no stated facts... "
            "trigger an exclusion'), because policy_checker runs isolated from damage_assessor by "
            "design. This is the textbook MISSED_FACT case the coordinator exists to catch: one "
            "branch's notes contain a detail the other branch's judgment never got to weigh, and "
            "it's MATERIAL because it could flip coverage_status entirely. A correct judge flags "
            "this as a MATERIAL missed_fact, retry target policy_checker."
        ),
    },
    {
        "case_id": "CC-05",
        "judge": "consistency_coordinator",
        "source_claim": "CLM-STRAT-031 (real, high_value_clean, fraud_reported=N)",
        "risk_stratum": "high_value_clean",
        "inputs": {
            "claim_amount": 87010.0,
            "damage_output": {
                "assessed_damage_usd": 63280.0, "repair_vs_replace": "total_loss",
                "assessment_notes": "Single-vehicle collision, total loss per repair-to-ACV ratio, consistent "
                                     "with the described severity.",
            },
            "policy_output": {
                "coverage_status": "covered", "exclusions_triggered": [],
                "coverage_notes": "No exclusions apply, standard high-value claim.",
            },
            "fraud_output": {
                "fraud_risk_level": "low", "fraud_score": 0.18,
                "crew_summary": "High amount is within normal range for a total-loss single-vehicle claim; "
                                 "no anomalies, narrative fully consistent across documents.",
            },
        },
        "proxy_label": {"has_conflicts": False, "conflict_type": None, "severity": None},
        "proxy_reasoning": (
            "All three branches agree: total_loss severity, no exclusions, low fraud risk, high "
            "amount explained by the real total-loss scenario (real fraud_reported=N supports "
            "this is a genuinely clean high-value claim, not a case the coordinator should invent "
            "a conflict for). A correct judge finds no conflict - a high dollar amount alone is "
            "not itself a cross-branch disagreement."
        ),
    },
    {
        "case_id": "CC-06",
        "judge": "consistency_coordinator",
        "source_claim": "constructed (NOTABLE-only conflict, should NOT trigger a retry)",
        "risk_stratum": "clean",
        "inputs": {
            "claim_amount": 5200.0,
            "damage_output": {
                "assessed_damage_usd": 4800.0, "repair_vs_replace": "repair",
                "assessment_notes": "Side-swipe damage along the driver side, moderate scope.",
            },
            "policy_output": {
                "coverage_status": "covered", "exclusions_triggered": [],
                "coverage_notes": "Standard coverage, no exclusions.",
            },
            "fraud_output": {
                "fraud_risk_level": "low", "fraud_score": 0.1,
                "crew_summary": "Claimant narrative describes 'scraped the whole driver side' - slightly "
                                 "more extensive wording than damage assessor's 'moderate scope,' but no "
                                 "underlying disagreement about what happened or what it costs.",
            },
        },
        "proxy_label": {"has_conflicts": True, "conflict_type": "severity_mismatch", "severity": "notable"},
        "proxy_reasoning": (
            "There's a real but trivial wording difference (moderate scope vs. scraped the whole "
            "side) that doesn't change the payout, coverage, or fraud classification - both "
            "readings land on the same repair scope and the same low-stakes claim. A correct "
            "judge should surface this as NOTABLE at most, never MATERIAL, and critically must "
            "NOT trigger a retry over it - this is exactly the case that tests whether the judge "
            "over-flags stylistic differences as real conflicts (the project's own severity scale "
            "was designed specifically to prevent this)."
        ),
    },

    # ══════════════════════════════════════════════════════════════════════
    # EVALUATOR - fairness dimension (would a human adjuster reach the same
    # conclusion on this specific claim, given the facts shown)
    # ══════════════════════════════════════════════════════════════════════

    {
        "case_id": "FAIR-01",
        "judge": "fairness",
        "source_claim": "CLM-STRAT-001 (real, clean)",
        "risk_stratum": "clean",
        "inputs": {
            "claim_summary": "Multi-vehicle collision, minor damage, $34,650 estimated, no exclusions, low fraud risk.",
            "settlement_decision": "APPROVED, $22,600 (gross damage $23,100 minus $500 deductible), "
                                    "within policy limits.",
            "calculation_breakdown": [
                "Assessed damage: $23,100", "Deductible: $500",
                "Payable: $23,100 - $500 = $22,600", "Within coverage limit of $50,000: approved in full",
            ],
        },
        "proxy_label": {"score": 1.0},
        "proxy_reasoning": (
            "Standard clean claim, arithmetic is transparent, nothing disputed, no red flags. A "
            "real adjuster reviewing this would approve it exactly as shown - this is the "
            "textbook straightforward case a fairness judge should score at or near 1.0 with high "
            "confidence, not flag as uncertain."
        ),
    },
    {
        "case_id": "FAIR-02",
        "judge": "fairness",
        "source_claim": "CLM-STRAT-042 (real, deductible_exceeds_damage)",
        "risk_stratum": "deductible_exceeds_damage",
        "inputs": {
            "claim_summary": "Minor collision, $19,080 estimated damage, policy deductible is $15,000 "
                              "(the project's deliberately high-deductible test policy).",
            "settlement_decision": "APPROVED_PARTIAL, $4,080 (damage $19,080 minus $15,000 deductible).",
            "calculation_breakdown": [
                "Assessed damage: $19,080", "Deductible: $15,000",
                "Payable: $19,080 - $15,000 = $4,080", "Approved at the reduced amount.",
            ],
        },
        "proxy_label": {"score": 1.0},
        "proxy_reasoning": (
            "The payout looks small relative to the claim amount, which could superficially read "
            "as unfair, but the math is exactly what the claimant's own policy terms require - a "
            "real adjuster bound by the same policy would reach the identical number. A good "
            "fairness judge should recognize that 'low payout' and 'unfair payout' are not the "
            "same thing when the deductible genuinely explains the gap, and should NOT penalize "
            "this decision for the small payout alone."
        ),
    },
    {
        "case_id": "FAIR-03",
        "judge": "fairness",
        "source_claim": "CLM-STRAT-044 (real, policy_exclusion, racing)",
        "risk_stratum": "policy_exclusion",
        "inputs": {
            "claim_summary": "Single-vehicle collision, total loss, $76,600 estimated. Claimant's own "
                              "narrative states the vehicles were racing on a closed stretch of road at "
                              "the time of the collision.",
            "settlement_decision": "DENIED. Racing exclusion applies - claimant's own stated facts match "
                                    "the policy's racing exclusion criteria exactly.",
            "calculation_breakdown": ["Coverage status: NOT_COVERED (racing exclusion triggered)", "Settlement amount: $0"],
        },
        "proxy_label": {"score": 1.0},
        "proxy_reasoning": (
            "A full denial on a $76,600 claim could look harsh in isolation, but the claimant's "
            "own words describe exactly the scenario the policy's racing exclusion is written to "
            "exclude - a real adjuster has no discretion to pay this out once the claimant's own "
            "narrative admits the triggering fact. This case specifically tests whether the "
            "fairness judge conflates 'large denial' with 'unfair denial' - it should not; a "
            "denial that correctly applies an unambiguous, self-admitted exclusion is fair."
        ),
    },
    {
        "case_id": "FAIR-04",
        "judge": "fairness",
        "source_claim": "constructed (settlement amount that ignores a real contradicting fact)",
        "risk_stratum": "fraud_flagged",
        "inputs": {
            "claim_summary": "Single-vehicle total-loss claim, $46,200 estimated. Fraud branch flagged HIGH "
                              "risk (0.78) citing an anomalously high amount for this claimant's history and "
                              "no witnesses; this was never escalated or resolved before settlement.",
            "settlement_decision": "APPROVED, $33,600 in full, no HITL review, no mention of the fraud "
                                    "concern anywhere in the settlement reasoning.",
            "calculation_breakdown": [
                "Assessed damage: $33,600", "Deductible: $500 (not actually subtracted - arithmetic error)",
                "Settlement: $33,600 (full amount, no deduction applied)",
            ],
        },
        "proxy_label": {"score": 0.1},
        "proxy_reasoning": (
            "Two real problems a fairness judge should catch: (1) a HIGH fraud-risk claim (0.78, "
            "above this project's own 0.45 HITL review threshold) was auto-approved without any "
            "human review or even acknowledgment in the reasoning - a competent adjuster would "
            "never silently approve a case their own fraud system flagged as high risk; (2) the "
            "deductible was never actually subtracted, an arithmetic error that overpays the "
            "claimant. A real adjuster would not reach this same conclusion. This case tests "
            "whether the judge actually reasons about the claim-specific facts shown rather than "
            "defaulting to a lenient high score (the field guide's 'leniency/sycophancy' bias, "
            "ss3.3) - a judge that scores this near 1.0 has failed the calibration test."
        ),
    },

    # ══════════════════════════════════════════════════════════════════════
    # EVALUATOR - transparency dimension (is the reasoning explainable and
    # traceable step by step, without guessing or inferring missing logic)
    # ══════════════════════════════════════════════════════════════════════

    {
        "case_id": "TRANS-01",
        "judge": "transparency",
        "source_claim": "CLM-STRAT-001 (real, clean)",
        "risk_stratum": "clean",
        "inputs": {
            "calculation_breakdown": [
                "Assessed damage: $23,100", "Deductible: $500",
                "Payable: $23,100 - $500 = $22,600", "Within coverage limit of $50,000: approved in full",
            ],
            "denial_reasons": [],
        },
        "proxy_label": {"score": 1.0},
        "proxy_reasoning": (
            "Every number traces to a stated input, every step shows its own arithmetic, nothing "
            "is asserted without a visible basis. A claimant or regulator could follow this "
            "exactly with no guessing. Clear, complete, auditable."
        ),
    },
    {
        "case_id": "TRANS-02",
        "judge": "transparency",
        "source_claim": "CLM-STRAT-044 (real, policy_exclusion, racing)",
        "risk_stratum": "policy_exclusion",
        "inputs": {
            "calculation_breakdown": ["Coverage status: NOT_COVERED (racing exclusion triggered)", "Settlement amount: $0"],
            "denial_reasons": [
                "Claimant's narrative states the vehicles were racing on a closed stretch of road "
                "at the time of the collision, matching the policy's racing exclusion criteria.",
            ],
        },
        "proxy_label": {"score": 1.0},
        "proxy_reasoning": (
            "The denial reason cites the exact fact (racing on a closed stretch) and the exact "
            "exclusion it matches - a claimant reading this knows precisely why they were denied "
            "and what specific statement triggered it. Nothing is left for the reader to infer."
        ),
    },
    {
        "case_id": "TRANS-03",
        "judge": "transparency",
        "source_claim": "constructed (vague breakdown, real failure pattern this metric should catch)",
        "risk_stratum": "high_value_fraud",
        "inputs": {
            "calculation_breakdown": [
                "Damage assessed", "Deductions applied", "Settlement calculated per policy terms",
                "Final amount: $58,000",
            ],
            "denial_reasons": [],
        },
        "proxy_label": {"score": 0.15},
        "proxy_reasoning": (
            "Every step names an action but shows none of its actual numbers or logic - 'damage "
            "assessed' to what figure, 'deductions applied' of what amount and why, 'per policy "
            "terms' which terms. A claimant or regulator cannot verify a single step here; they'd "
            "have to take the $58,000 on faith. This is the exact gap the transparency dimension "
            "exists to catch - vague procedural language standing in for real traceability. A "
            "judge that scores this highly because it 'looks like' a normal breakdown (format "
            "bias, field guide ss3.3) has failed the calibration test."
        ),
    },
    {
        "case_id": "TRANS-04",
        "judge": "transparency",
        "source_claim": "constructed (partially transparent - one real gap)",
        "risk_stratum": "fraud_flagged",
        "inputs": {
            "calculation_breakdown": [
                "Assessed damage: $33,600", "Deductible: $500",
                "Payable: $33,600 - $500 = $33,100",
                "Fraud review adjustment applied", "Final settlement: $28,000",
            ],
            "denial_reasons": [],
        },
        "proxy_label": {"score": 0.4},
        "proxy_reasoning": (
            "The first three steps are fully traceable, but the jump from $33,100 to $28,000 via "
            "an unexplained 'fraud review adjustment' breaks the chain - a $5,100 reduction with "
            "no stated basis. Mostly transparent with one real, consequential unexplained step - "
            "this should score meaningfully below a fully clean case like TRANS-01/02, not be "
            "rounded up to 'basically fine.' Tests whether the judge can distinguish a case with "
            "ONE real gap from both a fully clean case and a fully opaque one, rather than "
            "collapsing everything into 'mostly good' (leniency bias)."
        ),
    },
]


def summarize() -> None:
    from collections import Counter
    by_judge = Counter(c["judge"] for c in GOLDEN_SET)
    by_stratum = Counter(c["risk_stratum"] for c in GOLDEN_SET)
    print(f"Total cases: {len(GOLDEN_SET)}")
    print(f"By judge: {dict(by_judge)}")
    print(f"By risk stratum: {dict(by_stratum)}")


if __name__ == "__main__":
    summarize()
