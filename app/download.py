"""Bounded streaming downloads for the ingest paths.

The TiC and Medicare ingest commands accept a remote URL. Reading an arbitrary
remote file fully into memory is an OOM vector: a hostile or mistyped URL pointing
at a multi-gigabyte (or endless) body would exhaust RAM. Every download here is
streamed in chunks and aborted the instant the running total exceeds the configured
ceiling (settings.ingest_max_bytes), so an over-limit body is never materialized.

Two shapes are provided:
  - stream_to_spool(): for the CSV path — streams into a SpooledTemporaryFile that
    stays in memory only up to a small threshold and then rolls to disk, so the CSV
    is parsed incrementally without ever holding the whole file in RAM.
  - stream_to_bytes(): for the TiC JSON path, which must parse the full document at
    once; the cap still guarantees we never buffer more than the ceiling.
"""
import tempfile

import httpx

from .config import settings


class DownloadTooLarge(Exception):
    """Raised when a remote download exceeds settings.ingest_max_bytes."""


def _cap(max_bytes):
    return settings.ingest_max_bytes if max_bytes is None else max_bytes


def _reject_if_declared_over(resp: httpx.Response, src: str, cap: int) -> None:
    """Fast-fail on a declared Content-Length over the cap, before reading a byte."""
    cl = resp.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > cap:
        raise DownloadTooLarge(
            f"{src}: declared Content-Length {int(cl)} exceeds cap of {cap} bytes")


def stream_to_bytes(src: str, max_bytes=None, timeout: float = 600) -> bytes:
    """Download `src` into memory, aborting past the cap. Chunks accumulate only
    while under the ceiling; an over-limit body raises before it is fully read."""
    cap = _cap(max_bytes)
    chunks, total = [], 0
    with httpx.stream("GET", src, follow_redirects=True, timeout=timeout) as resp:
        resp.raise_for_status()
        _reject_if_declared_over(resp, src, cap)
        for chunk in resp.iter_bytes():
            total += len(chunk)
            if total > cap:
                raise DownloadTooLarge(f"{src}: exceeded cap of {cap} bytes")
            chunks.append(chunk)
    return b"".join(chunks)


def stream_to_spool(src: str, max_bytes=None, timeout: float = 600,
                    spool_max: int = 8 * 1024 * 1024) -> tempfile.SpooledTemporaryFile:
    """Stream `src` into a rewound SpooledTemporaryFile (RAM up to `spool_max`, then
    disk), aborting past the cap. The caller wraps/reads it incrementally so a large
    file is parsed without ever being held whole in memory."""
    cap = _cap(max_bytes)
    spool = tempfile.SpooledTemporaryFile(max_size=spool_max, mode="w+b")
    total = 0
    try:
        with httpx.stream("GET", src, follow_redirects=True, timeout=timeout) as resp:
            resp.raise_for_status()
            _reject_if_declared_over(resp, src, cap)
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > cap:
                    raise DownloadTooLarge(f"{src}: exceeded cap of {cap} bytes")
                spool.write(chunk)
    except BaseException:
        spool.close()
        raise
    spool.seek(0)
    return spool
