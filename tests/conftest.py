"""Shared fixtures: every test runs against an isolated temp SQLite DB."""
import os
import tempfile

import pytest

from app import db
from app.config import settings


@pytest.fixture(autouse=True)
def _hermetic_planet_registry():
    """Keep the suite hermetic: don't auto-wire the live public Plan-Net endpoints (no
    real network calls / 12s timeouts during tests). Tests that exercise the registry
    set settings.use_planet_registry = True explicitly."""
    old = settings.use_planet_registry
    settings.use_planet_registry = False
    try:
        yield
    finally:
        settings.use_planet_registry = old


@pytest.fixture()
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    old = settings.db_path
    settings.db_path = path
    db.init_db()
    try:
        yield path
    finally:
        settings.db_path = old
        for p in (path, path + "-wal", path + "-shm"):
            try:
                os.remove(p)
            except OSError:
                pass
