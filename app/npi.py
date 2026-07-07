"""NPI validation — the Luhn check-digit gate for every NPI admitted to a membership set.

An NPI is a 10-digit number whose last digit is a Luhn check digit computed over the
fixed ISO issuer prefix "80840" plus the first 9 digits (the CMS NPI check-digit spec).
`len == 10 and isdigit()` is NOT enough: harvested payer files (notably TiC) routinely
carry garbage in the NPI field — a TIN, a zero-padded internal id, a truncated value.
Admitting those would fabricate a "yes" for a provider that isn't really in the set,
which violates InNetwork's never-overclaim invariant at the data layer.

So every NPI crossing into a Roaring membership bitmap passes `luhn_valid()` first.
"""
from __future__ import annotations

# The ISO 7812 issuer identifier NPIs are namespaced under; prepended before the Luhn
# sum per the official CMS NPI check-digit algorithm.
_NPI_PREFIX = "80840"


def luhn_valid(npi: str) -> bool:
    """True iff `npi` is a 10-digit string with a valid NPI Luhn check digit.

    Rejects the common impostors that pass a naive length/digit test: a 10-digit TIN,
    a mistyped NPI, a zero-padded internal id. Only a value that satisfies the real
    check-digit relation is admitted, so a fabricated NPI can never enter a verified set.
    """
    if not isinstance(npi, str) or len(npi) != 10 or not npi.isdigit():
        return False
    total = 0
    # Luhn over "80840" + npi, doubling every second digit counting from the right (the
    # check digit itself sits at position 0 and is not doubled). A valid NPI makes the
    # running total a multiple of 10.
    for i, ch in enumerate(reversed(_NPI_PREFIX + npi)):
        d = ord(ch) - 48
        if i & 1:
            d += d
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0
