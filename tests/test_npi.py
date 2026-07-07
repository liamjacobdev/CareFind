"""The NPI Luhn gate — trust-critical: a garbage NPI must never enter a verified set.

`len == 10 and isdigit()` is the naive test that let TINs and mistyped ids slip into
harvested payer files. luhn_valid() is the check-digit gate that rejects them, so a
membership bitmap can't fabricate a "yes" for a provider that isn't really in the set.
"""
from app.npi import luhn_valid

# Real, in-service NPIs (valid Luhn check digit under the 80840 prefix).
_VALID = ["1003000126", "1003000134", "1003000142", "1992999874"]


def test_real_npis_pass():
    for npi in _VALID:
        assert luhn_valid(npi), npi


def test_check_digit_off_by_one_fails():
    # Flipping only the check digit must fail — that's the whole point of the check digit.
    for npi in _VALID:
        bad = npi[:9] + str((int(npi[9]) + 1) % 10)
        assert not luhn_valid(bad), bad


def test_shape_rejections():
    for bad in ["", "0000000000", "123456789", "12345678901", "1a3000126x",
                " 1003000126", "1003000126 ", "abcdefghij"]:
        assert not luhn_valid(bad), bad


def test_non_string_is_rejected_not_raised():
    for bad in [None, 1003000126, 3.14, ["1003000126"]]:
        assert luhn_valid(bad) is False  # type: ignore[arg-type]


def test_tin_shaped_impostor_is_rejected():
    # A 10-digit TIN-like value that isn't a valid NPI must not pass (the exact failure
    # mode the gate exists for: Anthem's TiN-in-NPI-field garbage).
    assert not luhn_valid("1234567890")
