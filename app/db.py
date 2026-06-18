"""SQLite persistence: the Medicare enrollment index and the geocode cache.

A connection is opened per operation (cheap for SQLite) so the module is safe to
use from FastAPI's threadpool and from the ingest CLI alike. Writes are guarded
by a process-level lock to avoid 'database is locked' under light concurrency.
"""
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from .config import settings
from .interfaces import Datastore

_write_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def backup(dest_path: str) -> str:
    """Take a consistent online backup of the SQLite DB to `dest_path` (E3). Uses
    SQLite's backup API, so it's safe while the app is running (WAL-consistent). Returns
    the destination path. Prod also runs continuous replication (Litestream); this is the
    programmatic primitive behind the documented restore drill."""
    src = _connect()
    try:
        dest = sqlite3.connect(dest_path)
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()
    return dest_path


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
        # Reverse-geocode cache (coords -> ZIP) for the 'Near me' button. Kept
        # separate from geocache because a ZIP is a TEXT value whose leading zeros
        # (e.g. 02134) would be destroyed by geocache's REAL lat/lon columns.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS revcache ("
            "  key TEXT PRIMARY KEY,"
            "  postcode TEXT,"
            "  created_at TEXT DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
        # FHIR Plan-Net result cache, so a search doesn't make a live HTTP call per
        # payer per provider every time. `value` is one of 'in_network' / 'not_found'
        # / 'unknown'; the caller maps these to True / False / None and applies a TTL,
        # so a cached 'unknown' is NEVER served as a 'no'. fetched_at is epoch seconds.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS fhir_cache ("
            "  payer TEXT NOT NULL,"
            "  npi TEXT NOT NULL,"
            "  value TEXT NOT NULL,"
            "  fetched_at REAL NOT NULL,"
            "  PRIMARY KEY (payer, npi)"
            ")"
        )
        # Provenance for a verified source: where its data came from (a public URL a
        # patient can use to verify) and when it was last refreshed (epoch seconds).
        # Written by each ingest; read into every verified result so a green badge is
        # always traceable to a real source with a fetch date (the A3 trust rule).
        conn.execute(
            "CREATE TABLE IF NOT EXISTS source_meta ("
            "  source_id TEXT PRIMARY KEY,"
            "  source_url TEXT,"
            "  fetched_at REAL NOT NULL"
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


def medicare_has_many(npis: list[str]) -> set[str]:
    """Return the subset of `npis` present in the Medicare index."""
    present: set[str] = set()
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


def medicare_add_many(npis: list[str]) -> int:
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


def tic_has_many(payer: str, npis: list[str]) -> set[str]:
    """Return the subset of `npis` listed in-network for `payer`."""
    present: set[str] = set()
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


def tic_add_many(payer: str, npis: list[str]) -> int:
    rows = [(str(payer), str(n).strip()) for n in npis if str(n).strip()]
    if not rows:
        return 0
    with _write_lock, _conn() as conn:
        conn.executemany("INSERT OR IGNORE INTO tic (payer, npi) VALUES (?, ?)", rows)
    return len(rows)


# ── Geocode cache ───────────────────────────────────────────────────────────
def geocode_get(key: str) -> list[float] | None:
    with _conn() as conn:
        row = conn.execute("SELECT lat, lon FROM geocache WHERE key = ?", (key,)).fetchone()
        if row and row["lat"] is not None:
            return [row["lat"], row["lon"]]
    return None


def geocode_set(key: str, lat: float, lon: float) -> None:
    with _write_lock, _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO geocache (key, lat, lon) VALUES (?, ?, ?)",
            (key, lat, lon),
        )


# ── Reverse-geocode cache (coords -> ZIP) ─────────────────────────────────────
def revgeocode_get(key: str) -> str | None:
    """Return the cached ZIP string for a coordinate key, or None if not cached.
    An empty-string value is a real cached 'no ZIP here' answer and is returned
    as such, so we don't re-hit the network for a known miss."""
    with _conn() as conn:
        row = conn.execute("SELECT postcode FROM revcache WHERE key = ?", (key,)).fetchone()
        if row is not None:
            return row["postcode"] or ""
    return None


def revgeocode_set(key: str, postcode: str) -> None:
    with _write_lock, _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO revcache (key, postcode) VALUES (?, ?)",
            (key, postcode or ""),
        )


# ── FHIR Plan-Net result cache ────────────────────────────────────────────────
def fhir_cache_get(payer: str, npi: str) -> tuple[str, float] | None:
    """Return (value_str, fetched_at) for a cached FHIR result, or None if absent.
    Freshness/TTL is the caller's call (it knows the per-state TTLs); this just
    reads the row."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT value, fetched_at FROM fhir_cache WHERE payer = ? AND npi = ?",
            (str(payer), str(npi)),
        ).fetchone()
        if row is not None:
            return row["value"], row["fetched_at"]
    return None


def fhir_cache_get_many(payer: str, npis: list[str]) -> dict[str, tuple[str, float]]:
    """Batch read: {npi: (value_str, fetched_at)} for the cached subset of `npis`."""
    out: dict[str, tuple[str, float]] = {}
    npis = [str(n) for n in npis]
    if not npis:
        return out
    with _conn() as conn:
        for i in range(0, len(npis), 500):
            chunk = npis[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT npi, value, fetched_at FROM fhir_cache "
                f"WHERE payer = ? AND npi IN ({placeholders})",
                [str(payer), *chunk],
            ):
                out[row["npi"]] = (row["value"], row["fetched_at"])
    return out


def fhir_cache_set(payer: str, npi: str, value: str, fetched_at: float) -> None:
    with _write_lock, _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO fhir_cache (payer, npi, value, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            (str(payer), str(npi), value, float(fetched_at)),
        )


def fhir_cache_set_many(payer: str, items: list[tuple[str, str]], fetched_at: float) -> int:
    """items: iterable of (npi, value_str). Bulk upsert with one timestamp."""
    rows = [(str(payer), str(n), v, float(fetched_at)) for n, v in items]
    if not rows:
        return 0
    with _write_lock, _conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO fhir_cache (payer, npi, value, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
    return len(rows)


# ── Source provenance (where a verified source's data came from + when) ────────
def source_meta_set(source_id: str, source_url: str, fetched_at: float) -> None:
    """Record (or refresh) a verified source's provenance. Called by each ingest."""
    with _write_lock, _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO source_meta (source_id, source_url, fetched_at) "
            "VALUES (?, ?, ?)",
            (str(source_id), source_url or "", float(fetched_at)),
        )


def source_meta_get(source_id: str) -> tuple[str, float] | None:
    """Return (source_url, fetched_at) for a source, or None if never recorded."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT source_url, fetched_at FROM source_meta WHERE source_id = ?",
            (str(source_id),),
        ).fetchone()
        if row is not None:
            return row["source_url"] or "", row["fetched_at"]
    return None


def source_meta_all() -> dict[str, tuple[str, float]]:
    """All recorded source provenance: {source_id: (source_url, fetched_at)} — the basis
    for the data-age SLOs in /healthz."""
    with _conn() as conn:
        rows = conn.execute("SELECT source_id, source_url, fetched_at FROM source_meta").fetchall()
    return {r["source_id"]: (r["source_url"] or "", r["fetched_at"]) for r in rows}


# ── Datastore seam ────────────────────────────────────────────────────────────
# The module functions above ARE the SQLite implementation. SqliteDatastore wraps
# them as an object satisfying the Datastore protocol (app/interfaces.py), so a future
# multi-worker backend (e.g. Postgres, D4) is a config swap rather than a rewrite. The
# module-level functions remain the default call path; new code that needs to honor
# the swap goes through get_datastore().
class SqliteDatastore:
    """The default $0 datastore: SQLite via the module functions in this file."""

    init_db = staticmethod(init_db)
    medicare_count = staticmethod(medicare_count)
    medicare_has = staticmethod(medicare_has)
    medicare_has_many = staticmethod(medicare_has_many)
    medicare_add_many = staticmethod(medicare_add_many)
    tic_count = staticmethod(tic_count)
    tic_has = staticmethod(tic_has)
    tic_has_many = staticmethod(tic_has_many)
    tic_add_many = staticmethod(tic_add_many)
    geocode_get = staticmethod(geocode_get)
    geocode_set = staticmethod(geocode_set)
    revgeocode_get = staticmethod(revgeocode_get)
    revgeocode_set = staticmethod(revgeocode_set)
    fhir_cache_get = staticmethod(fhir_cache_get)
    fhir_cache_get_many = staticmethod(fhir_cache_get_many)
    fhir_cache_set = staticmethod(fhir_cache_set)
    fhir_cache_set_many = staticmethod(fhir_cache_set_many)
    source_meta_set = staticmethod(source_meta_set)
    source_meta_get = staticmethod(source_meta_get)
    source_meta_all = staticmethod(source_meta_all)


def build_datastore() -> Datastore:
    """Select the datastore from config. Defaults to SQLite (the only $0 option today;
    a Postgres impl satisfying the Datastore protocol slots in here for D4)."""
    # settings.datastore is validated to a known value; unknown -> SQLite default.
    return SqliteDatastore()


_active: Datastore = build_datastore()


def get_datastore() -> Datastore:
    """The active datastore. Swappable via use_datastore() (e.g. in tests or D4)."""
    return _active


def use_datastore(ds: Datastore) -> None:
    """Swap the active datastore. The default SQLite impl is restored with
    use_datastore(build_datastore())."""
    global _active
    _active = ds
