"""
Damage Calculator Tools - used by the Damage Assessment Agent.

Provides depreciation tables and total-loss threshold calculations.

A hardcoded "repair cost baseline" table (10 UNSOURCED made-up cost ranges,
matched to a free-text description by fragile keyword search) used to live
here as get_repair_estimate_range(). It was retired: there is no trustworthy
external database of typical repair costs available to this system (the
real industry tools - Mitchell, CCC ONE, Audatex - are paid/contract-based,
not accessible here; checked during the damage_assessor redesign). A
plausible-looking but invented number is not better grounding than letting
the model invent one directly - it just hides the fabrication one layer
deeper (see CLAUDE.md "schema validity is not correctness"). The real fix:
src/agents/damage_assessor.py now grounds its assessment in the
repair-estimate DOCUMENT's own real, claim-specific line items when one was
submitted, and requires physical inspection when none exists, rather than
guessing from either a lookup table or free-text keywords.
"""

from __future__ import annotations

from datetime import date
from typing import Optional


# Depreciation rates by asset type and age
DEPRECIATION_RATES = {
    "auto": {
        "age_0_1": 0.20,
        "age_1_2": 0.15,
        "age_2_3": 0.12,
        "age_3_5": 0.09,
        "age_5_plus": 0.07,
    },
    "property": {
        "age_0_5": 0.04,
        "age_5_10": 0.06,
        "age_10_20": 0.08,
        "age_20_plus": 0.10,
    },
}

# Total loss threshold: if repair cost > X% of ACV, declare total loss
TOTAL_LOSS_THRESHOLD = 0.75


_MIN_ACV_USD = 500      # scrap/resale floor - even a very old car retains some value
_MIN_ACV_INR = 25000

def calculate_vehicle_acv(
    year: int,
    make: str,
    model: str,
    mileage: Optional[int] = None,
    as_of=None,
) -> float:
    """
    Estimate Actual Cash Value (ACV) of a vehicle.
    Uses simplified model - production would use NADA/KBB API.

    `as_of` (a date or ISO string, normally the claim's incident date) is when the age is
    measured; it defaults to today. ACV is the value at the time of loss, so a claim
    processed months later must not be valued as an older car.

    FIXED (was double-counting age): the base-value bracket used to flatten
    at "age > 10 -> $7,000" and then the depreciation loop below was applied
    AGAIN on top of that already-old-car base, for however many years past
    the bracket's own threshold - so an 11-year-old car and a 30-year-old
    car ended up at nearly the same (near-zero) ACV, and both were
    compressed into that outcome by compounding depreciation on an already
    depreciated base. Verified against real auto_collision claims in
    data/stratified_claims/ (vehicles 11-31 years old after the +11-year
    date shift in src/data/generate.py) - see project memory
    project-smart-claims-dataset-calibration.

    Fix: base brackets now extend through the vehicle's full age (no
    flat tail), each bracket representing the value AT that age milestone
    without needing further depreciation re-applied for years already
    covered by bracket selection. depreciation_years is the age LEFT OVER
    past the oldest bracket - only those years get the compounding
    depreciation loop, so no year of age is counted twice. A floor
    (_MIN_ACV_USD / _MIN_ACV_INR) prevents compounding from driving very
    old vehicles toward near-zero, matching real-world practice that even
    old cars retain scrap/resale value.
    """
    from src.utils import year_of
    age = year_of(as_of) - year

    # Base vehicle value by age - country-aware
    try:
        from src.config import get_country_meta
        is_india = get_country_meta().get("code", "US") == "IN"
    except Exception:
        is_india = False

    if is_india:
        # Indian market values (INR) - typical mid-segment car
        _BRACKETS = [
            (1, 800000), (3, 600000), (6, 400000), (10, 250000),
            (15, 120000), (20, 60000), (30, 30000),
        ]
        floor = _MIN_ACV_INR
    else:
        # US market values (USD)
        _BRACKETS = [
            (1, 35000), (3, 28000), (6, 20000), (10, 13000),
            (15, 7000), (20, 3500), (30, 1500),
        ]
        floor = _MIN_ACV_USD

    base = _BRACKETS[-1][1]
    bracket_ceiling = _BRACKETS[-1][0]
    for ceiling, value in _BRACKETS:
        if age <= ceiling:
            base = value
            bracket_ceiling = ceiling
            break

    # Only years PAST the matched bracket's own ceiling get the
    # compounding depreciation loop below - years already reflected in the
    # bracket choice are not depreciated a second time.
    depreciation_years = max(0, age - bracket_ceiling)

    # Apply cumulative depreciation for any age not already covered by the
    # matched bracket (e.g. a 33-year-old car: the 30-year bracket covers
    # the first 30 years of aging, these 3 extra years still compound).
    rates = DEPRECIATION_RATES["auto"]
    acv = base
    remaining_age = depreciation_years
    deductions = {
        1: rates["age_0_1"],
        2: rates["age_1_2"],
        3: rates["age_2_3"],
        5: rates["age_3_5"],
    }
    for threshold, rate in sorted(deductions.items()):
        if remaining_age > 0:
            years_in_bracket = min(remaining_age, threshold)
            acv *= (1 - rate) ** years_in_bracket
            remaining_age -= years_in_bracket
    if remaining_age > 0:
        # Any age beyond all deduction brackets: apply the oldest rate.
        acv *= (1 - rates["age_5_plus"]) ** remaining_age

    # Mileage adjustment
    if mileage:
        expected_mileage = age * 12000
        if mileage > expected_mileage * 1.5:
            acv *= 0.90  # High mileage penalty
        elif mileage < expected_mileage * 0.5:
            acv *= 1.08  # Low mileage premium

    return round(max(acv, floor), 2)


def should_total_loss(repair_estimate: float, acv: float) -> tuple[bool, float]:
    """
    Determine if a vehicle should be declared a total loss.
    Returns (is_total_loss, repair_to_acv_ratio).
    """
    if acv <= 0:
        return False, 0.0
    ratio = repair_estimate / acv
    return ratio >= TOTAL_LOSS_THRESHOLD, round(ratio, 3)


def apply_depreciation(
    damage_amount: float,
    asset_type: str,
    asset_age_years: int,
) -> tuple[float, float]:
    """
    Apply depreciation to a damage amount.
    Returns (depreciated_amount, depreciation_applied).
    """
    rates = DEPRECIATION_RATES.get(asset_type, DEPRECIATION_RATES["auto"])

    if asset_type == "auto":
        if asset_age_years <= 1:
            rate = rates["age_0_1"]
        elif asset_age_years <= 2:
            rate = rates["age_1_2"]
        elif asset_age_years <= 3:
            rate = rates["age_2_3"]
        elif asset_age_years <= 5:
            rate = rates["age_3_5"]
        else:
            rate = rates["age_5_plus"]
    else:  # property
        if asset_age_years <= 5:
            rate = rates["age_0_5"]
        elif asset_age_years <= 10:
            rate = rates["age_5_10"]
        elif asset_age_years <= 20:
            rate = rates["age_10_20"]
        else:
            rate = rates["age_20_plus"]

    depreciation = damage_amount * rate * asset_age_years
    depreciation = min(depreciation, damage_amount * 0.70)  # Max 70% depreciation
    depreciated = max(0, damage_amount - depreciation)

    return round(depreciated, 2), round(depreciation, 2)


def apply_depreciation_country_aware(
    damage_amount: float,
    asset_type: str,
    asset_age_years: int,
) -> tuple[float, float, str]:
    """
    Country-aware depreciation. Returns (depreciated_amount, depreciation_applied, method_used).

    US: year-based (existing logic above).
    India: part-wise IRDAI rates. Since we don't have an itemized surveyor's
    parts list, we apply a blended average across typical part categories.
    In production a surveyor's report would provide exact part breakdowns.
    """
    try:
        from src.config import get_depreciation_config
        dep_cfg = get_depreciation_config()
    except Exception:
        dep_cfg = {}

    method = dep_cfg.get("method", "year_based")

    if method == "part_wise":
        parts = dep_cfg.get("parts", {})
        if parts:
            # Typical auto claim part mix weights (approximate)
            _PART_WEIGHTS = {
                "metal_body": 0.35,
                "paint": 0.15,
                "rubber_nylon_plastic": 0.15,
                "glass": 0.10,
                "electrical_parts": 0.10,
                "mechanical_parts": 0.10,
                "batteries": 0.03,
                "air_bags": 0.02,
            }
            blended_rate = sum(
                parts.get(part, 0.0) * weight
                for part, weight in _PART_WEIGHTS.items()
            )
        else:
            blended_rate = 0.15
        depreciation = damage_amount * blended_rate
        depreciation = min(depreciation, damage_amount * 0.70)
        depreciated = max(0, damage_amount - depreciation)
        return round(depreciated, 2), round(depreciation, 2), "part_wise (IRDAI blended)"

    # Default: year-based (US)
    depreciated, dep_applied = apply_depreciation(damage_amount, asset_type, asset_age_years)
    return depreciated, dep_applied, "year_based"
