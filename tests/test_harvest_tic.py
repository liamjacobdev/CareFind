"""The streaming TiC harvester (Rail 2). The load-bearing tests are the ones the OLD
parser failed: modern files that factor providers into top-level `provider_references`
(inlined AND external `location` files) must be followed, not silently skipped. Plus the
Luhn gate, ToC fan-out, gzip streaming, and end-to-end serve."""
import gzip
import json

import pytest

from app import harvest_tic, membership
from app.insurance import MembershipSource

# Valid NPIs (pass Luhn). NPI_BAD is a Luhn-invalid impostor (the TIN-in-NPI-slot case).
NPI_A, NPI_B, NPI_C, NPI_D = "1003000126", "1003000134", "1003000142", "1003000159"
NPI_BAD = "1234567890"


def _write(tmp_path, name, obj, gz=False):
    p = tmp_path / name
    data = json.dumps(obj).encode()
    if gz:
        p.write_bytes(gzip.compress(data))
    else:
        p.write_bytes(data)
    return str(p)


def _harvest(src):
    h = harvest_tic.TicHarvester()
    h.harvest(src)
    stats = h.finalize()
    # Read the admitted NPIs back out of the bitmap as strings.
    got = {str(v + membership.OFFSET) for v in h.bitmap}
    return got, stats


# ── inlined provider_groups (the old, still-valid form) ───────────────────────
def test_inlined_provider_groups(tmp_path):
    src = _write(tmp_path, "in.json", {"in_network": [
        {"negotiated_rates": [{"provider_groups": [{"npi": [int(NPI_A), int(NPI_B)]}]}]}]})
    got, stats = _harvest(src)
    assert got == {NPI_A, NPI_B} and stats.files == 1


# ── modern form: top-level provider_references, INLINED (old parser missed this) ──
def test_top_level_provider_references_inlined(tmp_path):
    src = _write(tmp_path, "in.json", {
        "in_network": [{"negotiated_rates": [{"provider_references": [1]}]}],
        "provider_references": [{"provider_group_id": 1, "provider_groups": [{"npi": [int(NPI_C)]}]}]})
    got, _ = _harvest(src)
    assert got == {NPI_C}


# ── modern form: EXTERNAL provider_references location (THE key correction) ─────
def test_external_provider_reference_is_dereferenced(tmp_path):
    ext = _write(tmp_path, "ref1.json", {
        "provider_group_id": 1, "provider_groups": [{"npi": [int(NPI_D)]}]})
    src = _write(tmp_path, "in.json", {
        "in_network": [{"negotiated_rates": [{"provider_references": [1]}]}],
        "provider_references": [{"provider_group_id": 1, "location": ext}]})
    got, stats = _harvest(src)
    assert got == {NPI_D}                 # the old inlined-only parser would get nothing
    assert stats.external_refs == 1 and stats.files == 2


# ── Luhn gate: a garbage NPI can't enter the set ──────────────────────────────
def test_luhn_gate_rejects_impostor(tmp_path):
    src = _write(tmp_path, "in.json", {"in_network": [
        {"negotiated_rates": [{"provider_groups": [{"npi": [int(NPI_A), int(NPI_BAD)]}]}]}]})
    got, stats = _harvest(src)
    assert got == {NPI_A} and stats.rejected == 1


def test_string_npis_are_handled(tmp_path):
    src = _write(tmp_path, "in.json", {"in_network": [
        {"negotiated_rates": [{"provider_groups": [{"npi": [NPI_A, NPI_B]}]}]}]})
    got, _ = _harvest(src)
    assert got == {NPI_A, NPI_B}


# ── table-of-contents fan-out ─────────────────────────────────────────────────
def test_toc_fans_out_to_in_network_files(tmp_path):
    f1 = _write(tmp_path, "f1.json", {"in_network": [
        {"negotiated_rates": [{"provider_groups": [{"npi": [int(NPI_A)]}]}]}]})
    f2 = _write(tmp_path, "f2.json", {"in_network": [
        {"negotiated_rates": [{"provider_groups": [{"npi": [int(NPI_B)]}]}]}]})
    toc = _write(tmp_path, "toc.json", {"reporting_structure": [
        {"reporting_plans": [{"plan_id": "p"}],
         "in_network_files": [{"location": f1}, {"location": f2}]}]})
    got, stats = _harvest(toc)
    assert got == {NPI_A, NPI_B} and stats.files == 2


# ── gzip streaming ────────────────────────────────────────────────────────────
def test_gzipped_file_streams(tmp_path):
    src = _write(tmp_path, "in.json.gz", {"in_network": [
        {"negotiated_rates": [{"provider_groups": [{"npi": [int(NPI_A)]}]}]}]}, gz=True)
    got, _ = _harvest(src)
    assert got == {NPI_A}


# ── cycle guard ───────────────────────────────────────────────────────────────
def test_self_referencing_index_terminates(tmp_path):
    p = tmp_path / "loop.json"
    p.write_bytes(json.dumps({"provider_references": [{"provider_group_id": 1, "location": str(p)}]}).encode())
    got, _ = _harvest(str(p))         # must not infinite-loop
    assert got == set()


# ── end-to-end: harvest -> bitmap -> served verified (payer-level) ─────────────
@pytest.mark.asyncio
async def test_harvest_to_bitmap_served_verified(tmp_path):
    src = _write(tmp_path, "in.json", {"in_network": [
        {"negotiated_rates": [{"provider_groups": [{"npi": [int(NPI_A), int(NPI_B)]}]}]}]})
    out_dir = tmp_path / "payers"
    entry, stats = harvest_tic.harvest_to_bitmap("aetna", src, out_dir)
    assert entry is not None and entry.method == "tic" and entry.level == "payer"
    assert entry.count == 2

    store = membership.MembershipStore(out_dir)
    store.load()
    src_obj = MembershipSource(store.entry("aetna"), store)
    assert src_obj.confidence == "verified" and src_obj.requires_network is False
    got = await src_obj.check_many([NPI_A, NPI_B, NPI_C])
    assert got[NPI_A] is True and got[NPI_B] is True and got[NPI_C] is False
    store.close()


def test_empty_harvest_writes_nothing(tmp_path):
    src = _write(tmp_path, "in.json", {"in_network": []})
    out_dir = tmp_path / "payers"
    entry, stats = harvest_tic.harvest_to_bitmap("aetna", src, out_dir)
    assert entry is None and not (out_dir / "aetna.roaring").exists()


# ── helpers + CLI + URL streaming coverage ────────────────────────────────────
def test_resolve_ref_absolute_relative_and_url():
    import os
    # absolute path passes through
    assert harvest_tic._resolve_ref("/tmp/in.json", os.path.abspath("/x/ref.json")) == os.path.abspath("/x/ref.json")
    # URL ref passes through; a relative ref resolves against a parent URL
    assert harvest_tic._resolve_ref("https://p/in.json", "https://q/ref.json") == "https://q/ref.json"
    assert harvest_tic._resolve_ref("https://p/dir/in.json", "ref.json") == "https://p/dir/ref.json"
    # relative ref against a local parent path
    got = harvest_tic._resolve_ref(str(pytest.__file__), "ref.json")
    assert got.endswith("ref.json")


def test_non_catalog_payer_warns_but_harvests(tmp_path, capsys):
    src = _write(tmp_path, "in.json", {"in_network": [
        {"negotiated_rates": [{"provider_groups": [{"npi": [int(NPI_A)]}]}]}]})
    entry, _ = harvest_tic.harvest_to_bitmap("not_a_catalog_id", src, tmp_path / "p")
    assert entry is not None
    assert "not in app/catalog.py" in capsys.readouterr().out


def test_cli_harvests_and_reports(tmp_path, monkeypatch, capsys):
    from app.config import settings
    src = _write(tmp_path, "in.json", {"in_network": [
        {"negotiated_rates": [{"provider_groups": [{"npi": [int(NPI_A), int(NPI_B)]}]}]}]})
    monkeypatch.setattr(settings, "membership_dir", str(tmp_path / "payers"))
    harvest_tic.main(["harvest_tic", "aetna", src])
    out = capsys.readouterr().out
    assert "wrote" in out and (tmp_path / "payers" / "aetna.roaring").exists()


def test_cli_usage_when_missing_args():
    with pytest.raises(SystemExit):
        harvest_tic.main(["harvest_tic", "aetna"])   # missing src


def test_cli_empty_harvest_reports_not_written(tmp_path, monkeypatch, capsys):
    from app.config import settings
    src = _write(tmp_path, "in.json", {"in_network": []})
    monkeypatch.setattr(settings, "membership_dir", str(tmp_path / "payers"))
    harvest_tic.main(["harvest_tic", "aetna", src])
    assert "NOT written" in capsys.readouterr().out


def test_open_binary_streams_a_url(tmp_path, monkeypatch):
    # The URL path goes through download.stream_to_spool; stub it to return a local spool.
    import io

    payload = json.dumps({"in_network": [
        {"negotiated_rates": [{"provider_groups": [{"npi": [int(NPI_A)]}]}]}]}).encode()
    monkeypatch.setattr("app.download.stream_to_spool", lambda src: io.BytesIO(payload))
    got, stats = _harvest("https://payer.example/in-network.json")
    assert got == {NPI_A} and stats.files == 1
