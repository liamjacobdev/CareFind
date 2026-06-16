"""C2: auto-discover in-network files from a TiC table-of-contents and ingest at scale."""
import json

import pytest

from app import db, ingest_tic, tic_index
from app.insurance import Registry

_TOC = {
    "reporting_entity_name": "Example Payer",
    "reporting_structure": [
        {
            "reporting_plans": [
                {"plan_name": "Gold PPO", "plan_id_type": "EIN", "plan_id": "123456789"},
            ],
            "in_network_files": [
                {"description": "in-network rates", "location": "https://cdn.example/aetna/in-network-1.json"},
                {"description": "in-network rates", "location": "https://cdn.example/aetna/in-network-2.json"},
            ],
        },
        {
            "reporting_plans": [{"plan_name": "Silver HMO", "plan_id": "987654321"}],
            "in_network_files": [
                {"location": "https://cdn.example/aetna/in-network-1.json"},  # dup -> collapsed
                {"location": "https://cdn.example/aetna/in-network-3.json"},
            ],
        },
    ],
}


def test_parse_index_extracts_dedup_file_refs_with_plan_ids():
    refs = tic_index.parse_index(json.dumps(_TOC).encode())
    locs = [r.location for r in refs]
    assert locs == [
        "https://cdn.example/aetna/in-network-1.json",
        "https://cdn.example/aetna/in-network-2.json",
        "https://cdn.example/aetna/in-network-3.json",
    ]  # order preserved, duplicate location collapsed
    assert refs[0].plan_ids == ("123456789",)  # plan granularity captured


def test_parse_index_returns_empty_for_a_plain_in_network_file():
    in_network = {"in_network": [{"provider_groups": [{"npi": [1003000126]}]}]}
    assert tic_index.parse_index(json.dumps(in_network).encode()) == []


def test_parse_index_tolerates_malformed_input():
    assert tic_index.parse_index(b"not json") == []
    assert tic_index.parse_index(b'{"reporting_structure": "nope"}') == []


def _write(path, obj):
    path.write_text(json.dumps(obj), encoding="utf-8")
    return str(path)


@pytest.mark.asyncio
async def test_index_ingest_fans_out_dedups_and_flips_to_verified(temp_db, tmp_path):
    """A ToC index is auto-discovered, every in-network file is ingested with NPIs
    deduped across files, and the payer flips to a verified ('Confirmed') filter."""
    f1 = _write(tmp_path / "in-network-1.json",
                {"in_network": [{"provider_groups": [{"npi": ["1003000126", "1112223338"]}]}]})
    f2 = _write(tmp_path / "in-network-2.json",
                {"in_network": [{"provider_groups": [{"npi": ["1112223338", "1999999984"]}]}]})  # overlap
    index = _write(tmp_path / "index.json", {
        "reporting_structure": [
            {"reporting_plans": [{"plan_id": "X"}],
             "in_network_files": [{"location": f1}, {"location": f2}]},
        ]})

    added = ingest_tic.ingest("aetna", index)
    assert added == 3  # 4 NPIs across 2 files, 1 overlap -> 3 unique
    assert db.tic_count("aetna") == 3

    reg = Registry()
    reg.build()
    ann = await reg.annotate([{"npi": "1999999984", "stateAb": "CA"}], only=["aetna"])
    assert ann["1999999984"]["aetna"]["confidence"] == "verified"
    assert ann["1999999984"]["aetna"]["value"] is True
