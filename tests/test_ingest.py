"""Ingest download caps (T1.1) and the local-file happy path.

The remote ingest paths must abort an over-limit body without reading it fully
into memory; a normal local file must still ingest.
"""
import httpx
import pytest
import respx

from app import db, ingest_medicare, ingest_tic
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
