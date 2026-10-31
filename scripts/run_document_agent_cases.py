"""Run each data/test_pdfs/ case through the real run_document_agent (real
Docling extraction, real LLM classify+extract calls) and print the result,
checked against data/test_pdfs/README.md's documented expectations.

Not a pytest suite - this spends real LLM quota, same as the other
scripts/run_*.py live-batch scripts in this repo. Run manually:
    python scripts/run_document_agent_cases.py
"""
from __future__ import annotations

import json
from pathlib import Path

from src.agents.document_agent import run_document_agent

PDF_DIR = Path(__file__).resolve().parent.parent / "data" / "test_pdfs"

CASES = [
    ("case1_clean", {
        "police_report": PDF_DIR / "case1_clean_police_report.pdf",
        "other_driver_info": PDF_DIR / "case1_clean_other_driver_info.pdf",
        "repair_estimate": PDF_DIR / "case1_clean_repair_estimate.pdf",
    }, "expect: no findings, all 3 slots populated"),
    ("case2_scanned", {
        "repair_estimate": PDF_DIR / "case2_scanned_repair_estimate.pdf",
    }, "expect: OCR fallback extracts the same content as case1's repair estimate"),
    ("case3_arithmetic_mismatch", {
        "repair_estimate": PDF_DIR / "case3_arithmetic_mismatch_repair_estimate.pdf",
    }, "expect: line_items_sum_mismatch finding"),
    ("case4_wrong_slot", {
        "police_report": PDF_DIR / "case1_clean_repair_estimate.pdf",  # deliberately wrong slot
    }, "expect: wrong_document_type finding"),
    ("case5_corrupt", {
        "repair_estimate": PDF_DIR / "case5_corrupt.pdf",
    }, "expect: could_not_parse finding, no crash"),
    ("case6_plausibility_mismatch", {
        "repair_estimate": PDF_DIR / "case6_plausibility_mismatch_repair_estimate.pdf",
    }, "expect: amount_implausible_vs_claim finding (claim estimated_amount=23100)"),
    ("case7_sparse", {
        "repair_estimate": PDF_DIR / "case7_sparse_repair_estimate.pdf",
    }, "expect: NO findings - lump sum is legitimately sparse"),
]


def main() -> None:
    for name, slots, expectation in CASES:
        uploaded = {slot: {"path": str(path)} for slot, path in slots.items()}
        state = {"claim": {"claim_id": f"CLM-{name}", "estimated_amount": 23100.0}, "uploaded_documents": uploaded}
        print(f"\n=== {name} ({expectation}) ===")
        result = run_document_agent(state)
        findings = result.get("document_verification_findings", [])
        print(f"  agent_call_count: {result.get('agent_call_count')}")
        print(f"  findings: {json.dumps(findings, indent=2) if findings else '[]'}")
        for slot, data in (result.get("claim_documents") or {}).items():
            print(f"  {slot}: {'populated' if data else 'None'}")


if __name__ == "__main__":
    main()
