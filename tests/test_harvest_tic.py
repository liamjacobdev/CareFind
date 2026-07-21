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


# ── completeness guard: a partial fan-out must NOT ship as complete ────────────
def test_partial_fanout_is_not_written_as_complete(tmp_path):
    """THE Rail-2 trust test (mirrors Rail 1): a ToC whose one in-network file reads but a
    second is unreachable is a HOLE. Serving the partial set as complete would make the
    missing file's providers read as a fabricated "no", so nothing is written."""
    good = _write(tmp_path, "good.json", {"in_network": [
        {"negotiated_rates": [{"provider_groups": [{"npi": [int(NPI_A)]}]}]}]})
    missing = str(tmp_path / "does_not_exist.json")
    toc = _write(tmp_path, "toc.json", {"reporting_structure": [
        {"reporting_plans": [{"plan_id": "p"}],
         "in_network_files": [{"location": good}, {"location": missing}]}]})
    entry, stats = harvest_tic.harvest_to_bitmap("aetna", toc, tmp_path / "payers")
    assert stats.unique_npis == 1 and stats.failed_files == 1 and stats.complete is False
    assert entry is None and not (tmp_path / "payers" / "aetna.roaring").exists()
    # An explicit opt-out of the guard may still write the partial set (debugging only).
    entry2, _ = harvest_tic.harvest_to_bitmap(
        "aetna", toc, tmp_path / "payers", complete_only=False)
    assert entry2 is not None and (tmp_path / "payers" / "aetna.roaring").exists()


def test_failed_external_ref_blocks_write(tmp_path):
    """A missing EXTERNAL provider_references location is the same kind of hole."""
    src = _write(tmp_path, "in.json", {
        "in_network": [{"negotiated_rates": [{"provider_references": [1]}]}],
        "provider_references": [{"provider_group_id": 1,
                                 "location": str(tmp_path / "missing_ref.json")}]})
    entry, stats = harvest_tic.harvest_to_bitmap("aetna", src, tmp_path / "payers")
    assert stats.failed_files == 1 and entry is None


def test_max_files_probe_cap_marks_incomplete(tmp_path):
    """`--max-files` recon must never write a partial set as complete."""
    f1 = _write(tmp_path, "f1.json", {"in_network": [
        {"negotiated_rates": [{"provider_groups": [{"npi": [int(NPI_A)]}]}]}]})
    f2 = _write(tmp_path, "f2.json", {"in_network": [
        {"negotiated_rates": [{"provider_groups": [{"npi": [int(NPI_B)]}]}]}]})
    toc = _write(tmp_path, "toc.json", {"reporting_structure": [
        {"reporting_plans": [{"plan_id": "p"}],
         "in_network_files": [{"location": f1}, {"location": f2}]}]})
    h = harvest_tic.TicHarvester(max_files=1)
    h.harvest(toc)
    stats = h.finalize()
    assert stats.files == 1 and stats.complete is False and stats.failures
    # Through the writer, the capped run keeps last-good.
    entry, stats2 = harvest_tic.harvest_to_bitmap("aetna", toc, tmp_path / "payers", max_files=1)
    assert entry is None


def test_cli_partial_reports_not_written(tmp_path, monkeypatch, capsys):
    from app.config import settings
    good = _write(tmp_path, "good.json", {"in_network": [
        {"negotiated_rates": [{"provider_groups": [{"npi": [int(NPI_A)]}]}]}]})
    toc = _write(tmp_path, "toc.json", {"reporting_structure": [
        {"reporting_plans": [{"plan_id": "p"}],
         "in_network_files": [{"location": good}, {"location": str(tmp_path / "gone.json")}]}]})
    monkeypatch.setattr(settings, "membership_dir", str(tmp_path / "payers"))
    harvest_tic.main(["harvest_tic", "aetna", toc])
    out = capsys.readouterr().out
    assert "NOT written" in out and "hole" in out
    assert not (tmp_path / "payers" / "aetna.roaring").exists()


def test_cli_allow_partial_writes(tmp_path, monkeypatch, capsys):
    from app.config import settings
    good = _write(tmp_path, "good.json", {"in_network": [
        {"negotiated_rates": [{"provider_groups": [{"npi": [int(NPI_A)]}]}]}]})
    toc = _write(tmp_path, "toc.json", {"reporting_structure": [
        {"reporting_plans": [{"plan_id": "p"}],
         "in_network_files": [{"location": good}, {"location": str(tmp_path / "gone.json")}]}]})
    monkeypatch.setattr(settings, "membership_dir", str(tmp_path / "payers"))
    harvest_tic.main(["harvest_tic", "aetna", toc, "--allow-partial"])
    assert (tmp_path / "payers" / "aetna.roaring").exists()


# ── ToC top-N union with convergence (giant-ToC path, e.g. Aetna national) ─────
def test_toc_top_n_unions_largest_files(tmp_path):
    a = _write(tmp_path, "a.json", {"in_network": [{"negotiated_rates": [
        {"provider_groups": [{"npi": [int(NPI_A), int(NPI_B), int(NPI_C)]}]}]}]})
    b = _write(tmp_path, "b.json", {"in_network": [{"negotiated_rates": [
        {"provider_groups": [{"npi": [int(NPI_A), int(NPI_B)]}]}]}]})
    c = _write(tmp_path, "c.json", {"in_network": [{"negotiated_rates": [
        {"provider_groups": [{"npi": [int(NPI_D)]}]}]}]})
    toc = _write(tmp_path, "toc.json", {"reporting_structure": [
        {"reporting_plans": [{"plan_id": "p"}],
         "in_network_files": [{"location": a}, {"location": b}, {"location": c}]}]})
    entry, stats, conv = harvest_tic.harvest_toc_top_n(
        "aetna", toc, tmp_path / "payers", top_n=3, plateau=0.0)  # plateau 0 => harvest all
    assert entry is not None and entry.count == 4        # {A,B,C,D} unioned
    assert conv[0].file == a and conv[0].added == 3      # largest file harvested first
    assert conv[-1].total == 4


def test_toc_top_n_plateau_stops_early(tmp_path):
    big = _write(tmp_path, "big.json", {"in_network": [{"negotiated_rates": [
        {"provider_groups": [{"npi": [int(NPI_A), int(NPI_B), int(NPI_C)]}]}]}]})
    mid = _write(tmp_path, "mid.json", {"in_network": [{"negotiated_rates": [
        {"provider_groups": [{"npi": [int(NPI_A), int(NPI_B)]}]}]}]})  # adds nothing new
    small = _write(tmp_path, "small.json", {"in_network": [{"negotiated_rates": [
        {"provider_groups": [{"npi": [int(NPI_D)]}]}]}]})
    toc = _write(tmp_path, "toc.json", {"reporting_structure": [
        {"reporting_plans": [{"plan_id": "p"}], "in_network_files": [
            {"location": big}, {"location": mid}, {"location": small}]}]})
    entry, stats, conv = harvest_tic.harvest_toc_top_n(
        "aetna", toc, tmp_path / "payers", top_n=3, plateau=0.5)
    assert len(conv) == 2                                # stopped after the 2nd (0% new < 50%)
    assert entry is not None and entry.count == 3        # never harvested small.json's NPI_D


def test_toc_top_n_skips_files_over_the_download_cap(tmp_path, monkeypatch):
    """REGRESSION (production, 2026-07-20): ranking largest-first hit Aetna's 8.5 GB file,
    which blew the download cap -> DownloadTooLarge -> counted as a hole -> nothing written.
    Oversized children must be SKIPPED (they can't be spooled) so the union proceeds using
    the largest files that actually fit."""
    from app.config import settings

    # `big` is padded so its on-disk size exceeds the cap we set below; `small` fits.
    big = _write(tmp_path, "big.json", {"pad": "x" * 5000, "in_network": [
        {"negotiated_rates": [{"provider_groups": [{"npi": [int(NPI_C)]}]}]}]})
    small = _write(tmp_path, "small.json", {"in_network": [
        {"negotiated_rates": [{"provider_groups": [{"npi": [int(NPI_A), int(NPI_B)]}]}]}]})
    toc = _write(tmp_path, "toc.json", {"reporting_structure": [
        {"reporting_plans": [{"plan_id": "p"}],
         "in_network_files": [{"location": big}, {"location": small}]}]})
    monkeypatch.setattr(settings, "ingest_max_bytes", 1000)   # big > 1000 bytes, small < 1000

    entry, stats, conv = harvest_tic.harvest_toc_top_n(
        "aetna", toc, tmp_path / "payers", top_n=8, plateau=0.0)
    assert entry is not None                      # proceeded instead of dying on the big file
    assert entry.count == 2 and stats.failed_files == 0
    assert [c.file for c in conv] == [small]      # oversized file skipped, not attempted


def test_toc_top_n_all_files_over_cap_reports_cleanly(tmp_path, monkeypatch):
    from app.config import settings
    big = _write(tmp_path, "big.json", {"pad": "x" * 5000, "in_network": [
        {"negotiated_rates": [{"provider_groups": [{"npi": [int(NPI_A)]}]}]}]})
    toc = _write(tmp_path, "toc.json", {"reporting_structure": [
        {"reporting_plans": [{"plan_id": "p"}], "in_network_files": [{"location": big}]}]})
    monkeypatch.setattr(settings, "ingest_max_bytes", 10)
    entry, stats, conv = harvest_tic.harvest_toc_top_n(
        "aetna", toc, tmp_path / "payers", top_n=8)
    assert entry is None and stats.error and "cap" in stats.error


def test_toc_top_n_failed_file_writes_nothing(tmp_path):
    good = _write(tmp_path, "good.json", {"in_network": [{"negotiated_rates": [
        {"provider_groups": [{"npi": [int(NPI_A)]}]}]}]})
    toc = _write(tmp_path, "toc.json", {"reporting_structure": [
        {"reporting_plans": [{"plan_id": "p"}], "in_network_files": [
            {"location": good}, {"location": str(tmp_path / "gone.json")}]}]})
    entry, stats, conv = harvest_tic.harvest_toc_top_n(
        "aetna", toc, tmp_path / "payers", top_n=5, plateau=0.0)
    assert entry is None and stats.failed_files >= 1
    assert not (tmp_path / "payers" / "aetna.roaring").exists()


def test_cli_toc_top_files_writes(tmp_path, monkeypatch, capsys):
    from app.config import settings
    a = _write(tmp_path, "a.json", {"in_network": [{"negotiated_rates": [
        {"provider_groups": [{"npi": [int(NPI_A), int(NPI_B)]}]}]}]})
    toc = _write(tmp_path, "toc.json", {"reporting_structure": [
        {"reporting_plans": [{"plan_id": "p"}], "in_network_files": [{"location": a}]}]})
    monkeypatch.setattr(settings, "membership_dir", str(tmp_path / "payers"))
    harvest_tic.main(["harvest_tic", "aetna", toc, "--toc-top-files", "3"])
    assert "wrote" in capsys.readouterr().out and (tmp_path / "payers" / "aetna.roaring").exists()


def test_gzipped_url_streams_from_a_spooled_download(monkeypatch):
    """REGRESSION (production failure, 2026-07-20): a gzipped URL must be readable.

    `stream_to_spool` returns a SpooledTemporaryFile opened "w+b", and
    `gzip.GzipFile(fileobj=...)` with NO mode INFERS the mode from `fileobj.mode` — so it
    opened for WRITING and every gzipped URL died with "read() on write-only GzipFile
    object". The pre-existing URL test missed it because io.BytesIO has no `.mode`, so
    GzipFile defaulted to read; this test uses a real SpooledTemporaryFile like production.
    """
    import tempfile

    payload = gzip.compress(json.dumps({"in_network": [
        {"negotiated_rates": [{"provider_groups": [{"npi": [int(NPI_A)]}]}]}]}).encode())

    def fake_spool(src, *a, **kw):
        sp = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
        sp.write(payload)
        sp.seek(0)
        return sp

    monkeypatch.setattr("app.download.stream_to_spool", fake_spool)
    got, stats = _harvest("https://payer.example/in-network.json.gz")
    assert got == {NPI_A} and stats.files == 1


def test_open_binary_streams_a_url(tmp_path, monkeypatch):
    # The URL path goes through download.stream_to_spool; stub it to return a local spool.
    import io

    payload = json.dumps({"in_network": [
        {"negotiated_rates": [{"provider_groups": [{"npi": [int(NPI_A)]}]}]}]}).encode()
    monkeypatch.setattr("app.download.stream_to_spool", lambda src: io.BytesIO(payload))
    got, stats = _harvest("https://payer.example/in-network.json")
    assert got == {NPI_A} and stats.files == 1
