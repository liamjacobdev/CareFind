"""E3: a tested backup → restore drill. Backups are only real if a restore is verified;
this proves the documented restore (docs/runbook.md) actually recovers the data."""
import os

from app import db
from app.config import settings


def test_backup_then_restore_preserves_the_indexes(temp_db, tmp_path):
    # Seed both indexes.
    db.medicare_add_many(["1003000126", "1112223338"])
    db.tic_add_many("aetna", ["1003000126"])
    db.source_meta_set("medicare", "https://cms.example/file", 1700000000.0)

    # Back up while "running" (online backup API), then restore into a fresh DB path.
    backup_path = str(tmp_path / "backup.db")
    db.backup(backup_path)
    assert os.path.exists(backup_path)

    old = settings.db_path
    settings.db_path = backup_path
    try:
        assert db.medicare_count() == 2          # data survived the round-trip
        assert db.tic_count("aetna") == 1
        assert db.medicare_has("1112223338") is True
        assert db.source_meta_get("medicare")[0] == "https://cms.example/file"
    finally:
        settings.db_path = old


def test_restore_into_a_clean_path_is_independent(temp_db, tmp_path):
    db.medicare_add_many(["1003000126"])
    backup_path = str(tmp_path / "snap.db")
    db.backup(backup_path)
    # Mutating the live DB after the snapshot doesn't change the backup.
    db.medicare_add_many(["9999999999"])
    old = settings.db_path
    settings.db_path = backup_path
    try:
        assert db.medicare_count() == 1          # snapshot is a point-in-time copy
    finally:
        settings.db_path = old
