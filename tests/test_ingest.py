"""Ingest download caps (T1.1) and the local-file happy path.

The remote ingest paths must abort an over-limit body without reading it fully
into memory; a normal local file must still ingest.
"""
import json

import httpx
import pytest
import respx

from app import db, ingest_medicare, ingest_tic, ingest_tic_job
from app.config import settings
from app.download import DownloadTooLarge, stream_to_bytes, stream_to_spool

CMS = "https://data.cms.gov/enrollment.csv"
TIC = "https://payer.example/in-network.json"


@respx.mock
def test_stream_to_bytes_aborts_over_cap(monkeypatch):
    monkeypatch.setattr(settings, "ingest_max_bytes", 100)
    # 1 KiB body with no declared Content-Length -> caught mid-stream by the byte cap.
    respx.get(CMS).mock(return_value=httpx.Response(200, content=b"x" * 1024))
    with pytest.raises(DownloadTooLarge):
        stream_to_bytes(CMS)


@respx.mock
def test_stream_rejects_declared_content_length(monkeypatch):
    monkeypatch.setattr(settings, "ingest_max_bytes", 100)
    respx.get(CMS).mock(return_value=httpx.Response(
        200, headers={"Content-Length": "999999"}, content=b"x"))
    with pytest.raises(DownloadTooLarge):
        stream_to_spool(CMS)


@respx.mock
def test_tic_ingest_rejects_oversized_remote(temp_db, monkeypatch):
    monkeypatch.setattr(settings, "ingest_max_bytes", 50)
    body = b"\n".join(b"%010d" % n for n in range(1000000000, 1000000050))  # > 50 bytes
    respx.get(TIC).mock(return_value=httpx.Response(200, content=body))
    with pytest.raises(DownloadTooLarge):
        ingest_tic.ingest("aetna", TIC)


@respx.mock
def test_medicare_ingest_streams_remote_under_cap(temp_db, monkeypatch):
    monkeypatch.setattr(settings, "ingest_max_bytes", 10 * 1024 * 1024)
    csv_body = b"NPI,Name\n1234567893,A\n1987654320,B\n"
    respx.get(CMS).mock(return_value=httpx.Response(200, content=csv_body))
    added = ingest_medicare.ingest(CMS)
    assert added == 2
    assert db.medicare_count() == 2


def test_medicare_ingest_local_file_still_works(temp_db):
    # The shipped sample must ingest unchanged (cap only governs remote downloads).
    added = ingest_medicare.ingest("sample_medicare.csv")
    assert added > 0
    assert db.medicare_count() == added


# ── A3: ingests record provenance (source URL + fetch date) ───────────────────
def test_medicare_ingest_records_provenance(temp_db):
    """A local-file ingest must still record a verifiable public source URL + date,
    so verified Medicare answers are traceable (the A3 trust rule)."""
    ingest_medicare.ingest("sample_medicare.csv")
    meta = db.source_meta_get("medicare")
    assert meta is not None
    url, fetched_at = meta
    assert url == ingest_medicare.CMS_ENROLLMENT_URL  # local file -> canonical URL
    assert fetched_at > 0


@respx.mock
def test_medicare_ingest_records_remote_url(temp_db, monkeypatch):
    monkeypatch.setattr(settings, "ingest_max_bytes", 10 * 1024 * 1024)
    respx.get(CMS).mock(return_value=httpx.Response(200, content=b"NPI\n1234567893\n"))
    ingest_medicare.ingest(CMS)
    url, _ = db.source_meta_get("medicare")
    assert url == CMS  # ingesting from a live URL records that URL


def test_tic_ingest_records_provenance(temp_db, tmp_path):
    npi_file = tmp_path / "aetna_npis.txt"
    npi_file.write_text("1003000126\n", encoding="utf-8")
    ingest_tic.ingest("aetna", str(npi_file))
    meta = db.source_meta_get("aetna")
    assert meta is not None
    url, fetched_at = meta
    assert url == str(npi_file) and fetched_at > 0


# ── T3.3: scheduled TiC ingestion job ─────────────────────────────────────────
def test_tic_job_flips_payer_to_verified_and_is_idempotent(temp_db, tmp_path):
    """Running the job for a payer ingests its in-network NPIs and flips it to a
    verified filter; re-running it changes nothing (idempotent — safe on a cron)."""
    npi_file = tmp_path / "aetna_npis.txt"
    npi_file.write_text("1003000126\n1003000134\n1003000142\n", encoding="utf-8")
    sources = tmp_path / "tic_sources.json"
    sources.write_text(json.dumps({"sources": [
        {"payer": "aetna", "url": str(npi_file)}]}), encoding="utf-8")

    first = ingest_tic_job.run(only_payer="aetna", sources_path=str(sources))
    assert first[0]["verified"] is True
    assert first[0]["total"] == 3

    # Re-run: same source, no duplicates, still verified — idempotent.
    second = ingest_tic_job.run(only_payer="aetna", sources_path=str(sources))
    assert second[0]["total"] == 3
    assert second[0]["verified"] is True
    assert db.tic_count("aetna") == 3


def test_tic_job_missing_source_errors_clearly(temp_db, tmp_path):
    sources = tmp_path / "tic_sources.json"
    sources.write_text(json.dumps({"sources": []}), encoding="utf-8")
    with pytest.raises(SystemExit):
        ingest_tic_job.run(only_payer="cigna", sources_path=str(sources))
