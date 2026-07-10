"""The Medicare bitmap builder (Phase 1 port). Covers the port-from-sqlite path, the
build-from-CSV path, provenance inheritance, and the CLI."""
import time

import pytest

from app import build_membership, db, membership
from app.config import settings

VALID = ["1003000126", "1003000134", "1003000142"]


def test_build_medicare_ports_sqlite_index(tmp_path, temp_db):
    db.medicare_add_many(VALID)
    db.source_meta_set("medicare", "https://data.cms.gov/enrollment", 1700000000.0)
    entry = build_membership.build_medicare(None, tmp_path)
    assert entry.count == len(VALID)
    assert entry.level == "plan" and entry.method == "cms-enrollment"
    # Provenance is inherited from the sqlite source_meta, not fabricated.
    assert entry.source_url == "https://data.cms.gov/enrollment"
    assert entry.fetched_at == 1700000000.0
    assert entry.max_age_days == settings.medicare_max_age_days

    store = membership.MembershipStore(tmp_path)
    store.load()
    assert store.has("medicare", VALID[0]) is True
    store.close()


def test_build_medicare_empty_index_refuses(tmp_path, temp_db):
    with pytest.raises(SystemExit):
        build_membership.build_medicare(None, tmp_path)


def test_build_medicare_from_csv_ingests_then_ports(tmp_path, temp_db):
    csv = tmp_path / "med.csv"
    csv.write_text("NPI\n" + "\n".join(VALID) + "\n", encoding="utf-8")
    entry = build_membership.build_medicare(str(csv), tmp_path)
    assert entry.count == len(VALID)
    assert db.medicare_count() == len(VALID)  # also landed in the sqlite index


def test_build_medicare_defaults_provenance_when_unrecorded(tmp_path, temp_db, monkeypatch):
    db.medicare_add_many(VALID)  # rows but no source_meta row
    entry = build_membership.build_medicare(None, tmp_path)
    assert entry.source_url == build_membership.CMS_ENROLLMENT_URL
    assert entry.fetched_at <= time.time()


def test_cli_builds_medicare(tmp_path, temp_db, monkeypatch, capsys):
    db.medicare_add_many(VALID)
    db.source_meta_set("medicare", "https://cms.example/file", 1700000000.0)
    monkeypatch.setattr(settings, "membership_dir", str(tmp_path))
    build_membership.main(["build_membership", "medicare"])
    assert (tmp_path / "medicare.roaring").exists()
    assert "admitted" in capsys.readouterr().out


def test_cli_usage_and_unknown_command(temp_db):
    with pytest.raises(SystemExit):
        build_membership.main(["build_membership"])          # no command -> usage
    with pytest.raises(SystemExit):
        build_membership.main(["build_membership", "nope"])  # unknown command
