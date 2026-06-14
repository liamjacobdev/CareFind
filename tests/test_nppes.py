"""NPPES query construction — the mapping the registry depends on."""
import pytest

from app import nppes


def test_npi_lookup_shortcuts_other_params():
    p = nppes.build_params({"npi": "1003000126", "zip": "32536"})
    assert p["number"] == "1003000126"
    assert "postal_code" not in p


def test_invalid_npi_rejected():
    with pytest.raises(ValueError):
        nppes.build_params({"npi": "123"})


def test_empty_query_rejected():
    with pytest.raises(ValueError):
        nppes.build_params({})


def test_zip_exact_for_small_radius():
    assert nppes.build_params({"zip": "32536", "radius": 10})["postal_code"] == "32536"
    assert nppes.build_params({"zip": "32536"})["postal_code"] == "32536"


def test_zip_prefix_widened_for_large_radius():
    assert nppes.build_params({"zip": "32536", "radius": 25})["postal_code"] == "325*"
    assert nppes.build_params({"zip": "32536", "radius": 100})["postal_code"] == "325*"


def test_name_split_into_first_last_with_wildcards():
    p = nppes.build_params({"name": "John Smith", "city": "Crestview", "state": "FL"})
    assert p["first_name"] == "John*"
    assert p["last_name"] == "Smith*"


def test_org_name_used_for_org_type():
    p = nppes.build_params({"name": "Gulf Coast", "type": "NPI-2", "state": "FL"})
    assert p["organization_name"] == "Gulf Coast*"


def test_limit_clamped():
    assert nppes.build_params({"zip": "32536", "limit": 9999})["limit"] == 200
    # 0/None are falsy -> default 25 (before the 1..200 clamp).
    assert nppes.build_params({"zip": "32536", "limit": 0})["limit"] == 25
    assert nppes.build_params({"zip": "32536", "limit": 5})["limit"] == 5
