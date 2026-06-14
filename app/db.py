"""SQLite persistence: the Medicare enrollment index and the geocode cache.

A connection is opened per operation (cheap for SQLite) so the module is safe to
use from FastAPI's threadpool and from the ingest CLI alike. Writes are guarded
by a process-level lock to avoid 'database is locked' under light concurrency.
"""
import sqlite3
import threading
from contextlib import contextmanager

from .config import settings

_write_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _conn():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _conn() as conn:
        # WAL is a persistent, DB-level setting written to the file header — set it
        # once here rather than re-issuing the pragma on every connection. Later
        # connections inherit it from the file.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS medicare (npi TEXT PRIMARY KEY)")
        # In-network NPIs per commercial payer, from Transparency-in-Coverage ingests.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tic ("
            "  payer TEXT NOT NULL,"
            "  npi TEXT NOT NULL,"
            "  PRIMARY KEY (payer, npi)"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS geocache ("
            "  key TEXT PRIMARY KEY,"
            "  lat REAL,"
            "  lon REAL,"
            "  created_at TEXT DEFAULT CURRENT_TIMESTAMP"
            ")"
        )


# ── Medicare index ──────────────────────────────────────────────────────────
def medicare_count() -> int:
    with _conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM medicare").fetchone()
        return int(row["n"]) if row else 0


def medicare_has(npi: str) -> bool:
    with _conn() as conn:
        row = conn.execute("SELECT 1 FROM medicare WHERE npi = ?", (str(npi),)).fetchone()
        return row is not None


def medicare_has_many(npis: list) -> set:
    """Return the subset of `npis` present in the Medicare index."""
    present: set = set()
    if not npis:
        return present
    with _conn() as conn:
        for i in range(0, len(npis), 500):  # stay under SQLite's variable limit
            chunk = [str(n) for n in npis[i:i + 500]]
            placeholders = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT npi FROM medicare WHERE npi IN ({placeholders})", chunk
            ):
                present.add(row["npi"])
    return present


def medicare_add_many(npis) -> int:
    rows = [(str(n).strip(),) for n in npis if str(n).strip()]
    if not rows:
        return 0
    with _write_lock, _conn() as conn:
        conn.executemany("INSERT OR IGNORE INTO medicare (npi) VALUES (?)", rows)
    return len(rows)


# ── Transparency-in-Coverage commercial index (NPI per payer) ───────────────
def tic_count(payer: str) -> int:
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM tic WHERE payer = ?", (str(payer),)
        ).fetchone()
        return int(row["n"]) if row else 0


def tic_has(payer: str, npi: str) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM tic WHERE payer = ? AND npi = ?", (str(payer), str(npi))
        ).fetchone()
        return row is not None


def tic_has_many(payer: str, npis: list) -> set:
    """Return the subset of `npis` listed in-network for `payer`."""
    present: set = set()
    if not npis:
        return present
    with _conn() as conn:
        for i in range(0, len(npis), 500):
            chunk = [str(n) for n in npis[i:i + 500]]
            placeholders = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT npi FROM tic WHERE payer = ? AND npi IN ({placeholders})",
                [str(payer), *chunk],
            ):
                present.add(row["npi"])
    return present


def tic_add_many(payer: str, npis) -> int:
    rows = [(str(payer), str(n).strip()) for n in npis if str(n).strip()]
    if not rows:
        return 0
    with _write_lock, _conn() as conn:
        conn.executemany("INSERT OR IGNORE INTO tic (payer, npi) VALUES (?, ?)", rows)
    return len(rows)


# ── Geocode cache ───────────────────────────────────────────────────────────
def geocode_get(key: str):
    with _conn() as conn:
        row = conn.execute("SELECT lat, lon FROM geocache WHERE key = ?", (key,)).fetchone()
        if row and row["lat"] is not None:
            return [row["lat"], row["lon"]]
    return None


def geocode_set(key: str, lat, lon) -> None:
    with _write_lock, _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO geocache (key, lat, lon) VALUES (?, ?, ?)",
            (key, lat, lon),
        )
