"""ACV and depreciation age are measured at the incident date, not the processing date:
a claim processed months after the loss must not be valued as an older car."""

from datetime import date

from src.tools.damage_calculator import calculate_vehicle_acv
from src.utils import calculate_asset_age, year_of


def test_age_is_measured_at_the_incident_date():
    assert calculate_asset_age(2019, "2026-03-01") == 7
    assert calculate_asset_age(2019, date(2026, 12, 31)) == 7
    assert calculate_asset_age(2019, "2025-01-01") == 6


def test_missing_or_bad_incident_date_falls_back_to_today():
    assert calculate_asset_age(2019, None) == date.today().year - 2019
    assert calculate_asset_age(2019, "not-a-date") == date.today().year - 2019
    assert year_of(None) == date.today().year
    assert calculate_asset_age(None, "2026-03-01") == 0


def test_acv_depends_on_the_incident_year_not_when_it_is_processed():
    # age 6 -> 20,000 bracket; age 7 -> 13,000 bracket
    assert calculate_vehicle_acv(2019, "Honda", "Civic", as_of="2025-06-01") == 20_000.0
    assert calculate_vehicle_acv(2019, "Honda", "Civic", as_of="2026-06-01") == 13_000.0
