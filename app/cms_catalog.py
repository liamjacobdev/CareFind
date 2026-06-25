"""Discover the current CMS Medicare enrollment CSV URL from the public DCAT catalog.

The Medicare Fee-For-Service Public Provider Enrollment file is republished quarterly at
a new dated URL. Rather than hard-code (and stale) that URL, resolve it at refresh time
from `https://data.cms.gov/data.json` — the stable DCAT-US catalog every data.cms.gov
dataset is listed in. Used by the seed-refresh GitHub Action (.github/workflows/
refresh-medicare-seed.yml) so the deploy's baked Medicare index updates itself.
"""
from __future__ import annotations

import httpx

from .config import settings

CATALOG_URL = "https://data.cms.gov/data.json"
# The dataset whose CSV every Medicare NPI is read from.
_DATASET_MATCH = ("fee-for-service", "provider enrollment")


def latest_medicare_csv_url(catalog_url: str = CATALOG_URL, timeout: float = 60.0) -> str:
    """Return the current Medicare FFS enrollment CSV download URL, or raise.

    Looks up the dataset by title in the DCAT catalog and returns its CSV distribution's
    downloadURL. Raises LookupError if the dataset or a CSV distribution isn't found, so
    a broken catalog fails the refresh loudly rather than silently ingesting nothing.
    """
    headers = {"Accept": "application/json", "User-Agent": settings.contact_ua}
    r = httpx.get(catalog_url, headers=headers, timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    datasets = r.json().get("dataset", [])
    for d in datasets:
        title = (d.get("title") or "").lower()
        if all(tok in title for tok in _DATASET_MATCH):
            for dist in d.get("distribution", []) or []:
                url = dist.get("downloadURL") or dist.get("accessURL") or ""
                if url.lower().endswith(".csv"):
                    return url
            raise LookupError(
                f"dataset {d.get('title')!r} has no CSV distribution in {catalog_url}"
            )
    raise LookupError(
        f"no Medicare FFS provider-enrollment dataset found in {catalog_url}"
    )


if __name__ == "__main__":
    print(latest_medicare_csv_url())
