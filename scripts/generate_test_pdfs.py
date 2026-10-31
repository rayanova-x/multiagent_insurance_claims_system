"""Generate a handful of test PDFs for the (not-yet-built) document agent.

NOT a reusable dataset generator like src/data/generate.py - this is a small,
fixed fixture set for manually exercising specific document-agent behaviors
(OCR fallback, arithmetic check, plausibility check, wrong-slot handling,
corrupt-file handling, sparse-document handling) once that agent exists.

One baseline claim (CLM-DOCTEST-001), 7 cases, each changing exactly ONE
document from the baseline's clean 3-document set - same "change one thing,
compare" discipline as the golden-set fairness pairs.

Layouts are structural mockups of each real document type (letterhead,
tables, boxed fields) built with reportlab's Platypus engine - not copies of
any real agency's or shop's actual form, but realistic enough that a PDF
parser (Docling) faces the same layout conventions (tables, multi-column
fields, letterhead) it would on a real document.

Usage: python scripts/generate_test_pdfs.py
"""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "test_pdfs"

STYLES = getSampleStyleSheet()
AGENCY_TITLE = ParagraphStyle("AgencyTitle", parent=STYLES["Title"], fontSize=16, spaceAfter=2, textColor=colors.HexColor("#1a2744"))
AGENCY_SUB = ParagraphStyle("AgencySub", parent=STYLES["Normal"], fontSize=9, textColor=colors.HexColor("#555555"))
SECTION_HEAD = ParagraphStyle("SectionHead", parent=STYLES["Heading3"], fontSize=11, spaceBefore=10, spaceAfter=4, textColor=colors.HexColor("#1a2744"))
BODY = ParagraphStyle("Body", parent=STYLES["Normal"], fontSize=10, leading=14)
FIELD_LABEL = ParagraphStyle("FieldLabel", parent=STYLES["Normal"], fontSize=8, textColor=colors.HexColor("#666666"))
FIELD_VALUE = ParagraphStyle("FieldValue", parent=STYLES["Normal"], fontSize=10)

BASELINE_CLAIM = {
    "claim_id": "CLM-DOCTEST-001",
    "policy_number": "POL-AUTO-TEST-US",
    "claimant_name": "Alex Rivera",
    "incident_date": "2026-06-10",
    "incident_location": "4410 Birchwood Ave, Denver, CO",
    "estimated_amount": 23100.0,
    "vehicle_year": 2020,
    "vehicle_make": "Toyota",
    "vehicle_model": "Camry",
}


def _field(label: str, value: str) -> list:
    return [Paragraph(label, FIELD_LABEL), Paragraph(value or "-", FIELD_VALUE)]


def _field_grid(rows: list[list[tuple[str, str]]]) -> Table:
    """Each row is a list of (label, value) pairs laid out as a boxed grid,
    like the field boxes on a real intake/accident-report form."""
    data = []
    for row in rows:
        cells = []
        for label, value in row:
            cells.append([Paragraph(label, FIELD_LABEL), Paragraph(value or "-", FIELD_VALUE)])
        data.append(cells)
    col_count = max(len(r) for r in data)
    col_width = 6.5 * inch / col_count
    # flatten each cell's 2-line content into a mini-table so Platypus can lay it out
    flat_data = [[c for c in row] for row in data]
    t = Table(
        [[Table([[cell[0]], [cell[1]]], colWidths=col_width - 8) for cell in row] for row in flat_data],
        colWidths=[col_width] * col_count,
    )
    t.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    return t


def _letterhead(doc_story: list, org_name: str, org_sub: str, doc_title: str) -> None:
    doc_story.append(Paragraph(org_name, AGENCY_TITLE))
    doc_story.append(Paragraph(org_sub, AGENCY_SUB))
    doc_story.append(Spacer(1, 10))
    hr = Table([[""]], colWidths=[6.5 * inch], rowHeights=[2])
    hr.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 1.5, colors.HexColor("#1a2744"))]))
    doc_story.append(hr)
    doc_story.append(Spacer(1, 8))
    doc_story.append(Paragraph(doc_title, SECTION_HEAD))


def _save(story: list, path: Path) -> None:
    doc = SimpleDocTemplate(str(path), pagesize=letter, topMargin=0.6 * inch, bottomMargin=0.6 * inch, leftMargin=0.7 * inch, rightMargin=0.7 * inch)
    doc.build(story)


# ── Police report ────────────────────────────────────────────────────────

def build_police_report(path: Path, claim: dict, report_number: str = "PR-2026-00417") -> None:
    story = []
    _letterhead(story, "DENVER POLICE DEPARTMENT", "Traffic Accident Report Division  •  1331 Cherokee St, Denver, CO 80204", "Traffic Accident Report")
    story.append(Spacer(1, 6))
    story.append(_field_grid([
        [("Report Number", report_number), ("Incident Date", claim["incident_date"])],
        [("Location", claim["incident_location"]), ("Jurisdiction", "Denver Police Department")],
    ]))
    story.append(Spacer(1, 10))
    story.append(Paragraph("Narrative", SECTION_HEAD))
    story.append(Paragraph(
        "Two-vehicle collision at intersection. Other driver ran red light and struck "
        "claimant's vehicle on the passenger side. Both vehicles sustained moderate damage. "
        "No injuries reported at the scene.",
        BODY,
    ))
    story.append(Spacer(1, 10))
    story.append(Paragraph("Fault Determination", SECTION_HEAD))
    story.append(Paragraph("Other driver cited as at fault.", BODY))
    story.append(Spacer(1, 10))
    story.append(Paragraph("Citations Issued", SECTION_HEAD))
    story.append(Paragraph("Failure to obey traffic signal (C.R.S. 42-4-603)", BODY))
    story.append(Spacer(1, 10))
    story.append(Paragraph("Parties Involved", SECTION_HEAD))
    story.append(_field_grid([
        [("Party 1", claim["claimant_name"]), ("Party 2", "Morgan Lee")],
    ]))
    _save(story, path)


# ── Other driver info exchange ──────────────────────────────────────────

def build_other_driver_info(path: Path, claim: dict) -> None:
    story = []
    _letterhead(story, "Driver Information Exchange Form", "Completed at scene of incident", f"Incident: {claim['incident_date']}")
    story.append(Spacer(1, 6))
    story.append(_field_grid([
        [("Driver Name", "Morgan Lee"), ("License Number", "CO-88213045")],
        [("Insurer", "Rocky Mountain Mutual"), ("Policy Number", "RMM-552013")],
        [("Vehicle Make/Model", "Honda Civic"), ("Vehicle Year", "2019")],
        [("License Plate", "CO-9DF-221"), ("Contact Phone", "(303) 555-0148")],
    ]))
    _save(story, path)


# ── Repair estimate ──────────────────────────────────────────────────────

def build_repair_estimate(
    path: Path,
    line_items: list[tuple[str, float]],
    stated_total: float,
    shop: str = "Summit Auto Body",
    shop_sub: str = "2210 Larimer St, Denver, CO 80205  •  (303) 555-0199",
    estimate_date: str = "2026-06-12",
    lump_sum_note: str | None = None,
) -> None:
    story = []
    _letterhead(story, shop.upper(), shop_sub, "Repair Estimate")
    story.append(Spacer(1, 6))
    story.append(_field_grid([
        [("Estimate Date", estimate_date), ("Estimate #", "EST-" + str(abs(hash(shop)) % 90000 + 10000))],
    ]))
    story.append(Spacer(1, 12))

    if lump_sum_note:
        story.append(Paragraph(lump_sum_note, BODY))
        story.append(Spacer(1, 10))
    else:
        table_data = [["Description", "Amount"]]
        for desc, amt in line_items:
            table_data.append([desc, f"${amt:,.2f}"])
        t = Table(table_data, colWidths=[4.5 * inch, 2 * inch])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a2744")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ]))
        story.append(t)
        story.append(Spacer(1, 10))

    total_table = Table([["Total", f"${stated_total:,.2f}"]], colWidths=[4.5 * inch, 2 * inch])
    total_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 12),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("LINEABOVE", (0, 0), (-1, 0), 1.5, colors.HexColor("#1a2744")),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(total_table)
    _save(story, path)


# ── Scanned-document simulation (no text layer) ─────────────────────────

def _render_as_scanned_pdf(path: Path, title: str, lines: list[str]) -> None:
    """Draw the same text directly onto a raster image (Pillow), then wrap
    that image in a PDF with no text layer - simulates a scanned/photographed
    document for testing Docling's OCR fallback path."""
    width, height = 1700, 2200  # ~8.5x11in at 200dpi
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    font_title = ImageFont.load_default(size=36)
    font_body = ImageFont.load_default(size=26)

    y = 100
    draw.text((100, y), title, fill="black", font=font_title)
    y += 80
    for line in lines:
        draw.text((100, y), line, fill="black", font=font_body)
        y += 50

    img.save(path, "PDF")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    claim = BASELINE_CLAIM

    # Case 1: clean baseline - all 3 documents, internally consistent.
    clean_items = [("Parts and materials", 13860.00), ("Labor", 9240.00)]
    clean_total = sum(a for _, a in clean_items)  # 23100.00, matches claim.estimated_amount

    build_police_report(OUT_DIR / "case1_clean_police_report.pdf", claim)
    build_other_driver_info(OUT_DIR / "case1_clean_other_driver_info.pdf", claim)
    build_repair_estimate(OUT_DIR / "case1_clean_repair_estimate.pdf", clean_items, clean_total)

    # Case 2: same repair_estimate content as case 1, rendered as a scanned
    # image (no text layer) - tests Docling's OCR fallback.
    _render_as_scanned_pdf(
        OUT_DIR / "case2_scanned_repair_estimate.pdf",
        "Repair Estimate",
        [
            "Summit Auto Body - 2210 Larimer St, Denver, CO",
            "Estimate Date: 2026-06-12",
            "Parts and materials: $13,860.00",
            "Labor: $9,240.00",
            f"Total: ${clean_total:,.2f}",
        ],
    )

    # Case 3: line items sum to 23,100 but the printed total says 28,500 -
    # tests the line_items_sum vs stated_total arithmetic check.
    build_repair_estimate(OUT_DIR / "case3_arithmetic_mismatch_repair_estimate.pdf", clean_items, stated_total=28500.00)

    # Case 4: wrong document in wrong slot - no new file, reuses
    # case1_clean_repair_estimate.pdf uploaded into the police_report slot
    # during manual testing. Documented in README, not generated here.

    # Case 5: corrupt / unreadable file - not a real PDF at all.
    (OUT_DIR / "case5_corrupt.pdf").write_bytes(b"%PDF-1.4 this is not actually a valid pdf stream \x00\x01\x02 truncated")

    # Case 6: repair estimate total wildly higher than claim.estimated_amount
    # (23,100) - tests the cross-field plausibility check.
    mismatch_items = [("Frame replacement", 50000.00), ("Labor", 35000.00)]
    build_repair_estimate(
        OUT_DIR / "case6_plausibility_mismatch_repair_estimate.pdf",
        mismatch_items,
        stated_total=85000.00,
        shop="Premier Collision Center",
        shop_sub="9800 E Colfax Ave, Aurora, CO 80010  •  (720) 555-0132",
    )

    # Case 7: legitimately sparse - no itemized line items, just a lump sum.
    # Tests that empty line_items (confirmed zero, not unchecked) isn't
    # wrongly flagged as a verification failure.
    build_repair_estimate(
        OUT_DIR / "case7_sparse_repair_estimate.pdf",
        line_items=[],
        stated_total=23100.00,
        lump_sum_note="Lump-sum estimate per attached shop invoice (itemized breakdown not provided).",
    )

    (OUT_DIR / "baseline_claim.json").write_text(json.dumps(claim, indent=2))

    print(f"Generated test PDFs in {OUT_DIR}")
    for f in sorted(OUT_DIR.glob("*.pdf")):
        print(f"  {f.name} ({f.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
