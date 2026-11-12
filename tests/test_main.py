"""grab: the `execute` action end to end against a scripted nsud server (month windows, ids, sidecars, idempotent restart) and the Proc-protocol loop."""
from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

import main
from conftest import PDF_BYTES, FakeTime, Server, fixture_bytes
from dp_sk_judikatura_grab.errors import CONFIG_ERROR, SYSTEM_ERROR, GrabError
from dp_sk_judikatura_grab.grab import GrabDeps, run_grab

MONTH: dict[str, Any] = {"key": "nsud:2026-09|v1", "meta": {"kind": "nsud", "itemId": "nsud:2026-09", "version": "1", "from": "2026-09-01", "to": "2026-09-30"}}
SUBOR = "eec/363149.0d1260.pdf"


def api_text(cislo: str) -> bytes:
    return json.dumps({"ID": "", "cislo": cislo, "senat": "1", "ecli": "", "datum": "2026-09-10", "kolegium": "1", "merito": "m", "sudca": "", "obsah": "Text rozhodnutia " * 20, "subor": ""}).encode()


class Nsud:
    """A scripted nsud: a window answers ``window_ids``, every other call is logged; ``bodies`` maps id -> raw body."""

    def __init__(self, window_ids: list[str], bodies: dict[str, bytes]) -> None:
        self.window_ids = window_ids
        self.bodies = bodies
        self.calls: list[str] = []
        self.capped_day: str | None = None

    def route(self, request: httpx.Request) -> httpx.Response:
        query = urlsplit(str(request.url)).query
        path = request.url.path
        self.calls.append(query or path)
        if path.startswith("/data/att/"):
            return httpx.Response(200, content=PDF_BYTES)
        if query.startswith("searchDecision"):
            if self.capped_day is not None:
                return httpx.Response(200, json=[str(n) for n in range(1000)])
            return httpx.Response(200, json=self.window_ids)
        found = re.match(r"getDecision&id=(\d+)$", query)
        assert found, query
        return httpx.Response(200, content=self.bodies.get(found.group(1), b"[]"))

    def count(self, prefix: str) -> int:
        return sum(1 for call in self.calls if call.startswith(prefix))


@pytest.fixture
def world() -> tuple[Nsud, FakeTime, GrabDeps]:
    nsud = Nsud(
        ["150000", "177749", "180001"],
        {"150000": fixture_bytes("decision-150000.json"), "177749": fixture_bytes("decision-177749.json"), "180001": api_text("2Cdo/1/2026")},
    )
    fake = FakeTime()
    deps = GrabDeps(transport=Server(nsud.route, fake).transport(), sleep=fake.sleep, clock=fake.clock)
    return nsud, fake, deps


def params(tmp_path: Path, *items: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"items": list(items or [MONTH]), "dir": str(tmp_path / "raw"), **extra}


def folder(tmp_path: Path, decision_id: str) -> Path:
    return tmp_path / "raw" / "nsud" / decision_id


@pytest.mark.unit
def test_a_month_writes_decisions_one_attachment_sidecars_and_the_item_index(world: tuple[Nsud, FakeTime, GrabDeps], tmp_path: Path) -> None:
    nsud, _, deps = world

    result = run_grab(params(tmp_path), deps, lambda *_: None)

    for decision_id in ("150000", "177749", "180001"):
        assert (folder(tmp_path, decision_id) / "decision.json").is_file()
        assert (folder(tmp_path, decision_id) / "fetch.json").is_file()
    assert (folder(tmp_path, "177749") / "attachment.pdf").read_bytes() == PDF_BYTES
    assert not (folder(tmp_path, "150000") / "attachment.pdf").exists(), "a decision with API text needs no PDF"
    assert (folder(tmp_path, "150000") / "decision.json").read_bytes() == fixture_bytes("decision-150000.json"), "the raw body is written verbatim"
    sidecar = json.loads((folder(tmp_path, "177749") / "fetch.json").read_text(encoding="utf-8"))
    assert sidecar["id"] == "177749"
    assert sidecar["version"] == "1"
    assert sidecar["subor"] == SUBOR
    assert sidecar["pdfUrl"] == f"https://www.nsud.sk/data/att/{SUBOR}"
    assert sidecar["pageUrl"] is None
    assert sidecar["fetchedAt"].endswith("Z")
    index = json.loads((tmp_path / "raw" / "items" / "nsud_2026-09.json").read_text(encoding="utf-8"))
    assert index["ids"] == ["150000", "177749", "180001"]
    assert index["listed"] == 3
    assert index["complete"] is True
    assert result["metrics"] == {"listed": 3, "taken": 3, "skipped_existing": 0, "pdf_only": 1}
    assert nsud.calls[0] == "searchDecision&art_datrozh_od=2026-09-01&art_datrozh_do=2026-09-30"


@pytest.mark.unit
def test_a_rerun_with_the_same_version_fetches_nothing(world: tuple[Nsud, FakeTime, GrabDeps], tmp_path: Path) -> None:
    nsud, _, deps = world
    run_grab(params(tmp_path), deps, lambda *_: None)
    before = len(nsud.calls)

    result = run_grab(params(tmp_path), deps, lambda *_: None)

    assert len(nsud.calls) == before, "no HTTP call at all"
    assert result["metrics"] == {"listed": 3, "taken": 3, "skipped_existing": 3, "pdf_only": 1}


@pytest.mark.unit
def test_a_new_version_fetches_again_but_the_window_is_asked_once(world: tuple[Nsud, FakeTime, GrabDeps], tmp_path: Path) -> None:
    nsud, _, deps = world
    run_grab(params(tmp_path), deps, lambda *_: None)
    nsud.calls.clear()
    second = {**MONTH, "key": "nsud:2026-09|v1-abcd1234", "meta": {**MONTH["meta"], "version": "1-abcd1234"}}

    result = run_grab(params(tmp_path, second), deps, lambda *_: None)

    assert nsud.count("searchDecision") == 1
    assert nsud.count("getDecision") == 3
    assert result["metrics"]["skipped_existing"] == 0
    assert json.loads((folder(tmp_path, "150000") / "fetch.json").read_text(encoding="utf-8"))["version"] == "1-abcd1234"


@pytest.mark.unit
def test_a_partial_earlier_run_skips_the_files_of_the_same_version_but_still_lists_the_window(world: tuple[Nsud, FakeTime, GrabDeps], tmp_path: Path) -> None:
    nsud, _, deps = world
    run_grab(params(tmp_path), deps, lambda *_: None)
    (tmp_path / "raw" / "items" / "nsud_2026-09.json").unlink()
    (folder(tmp_path, "180001") / "fetch.json").unlink()
    nsud.calls.clear()

    result = run_grab(params(tmp_path), deps, lambda *_: None)

    assert nsud.count("getDecision") == 1
    assert nsud.count("getDecision&id=180001") == 1
    assert result["metrics"]["skipped_existing"] == 2


@pytest.mark.unit
def test_explicit_ids_make_no_window_call(world: tuple[Nsud, FakeTime, GrabDeps], tmp_path: Path) -> None:
    nsud, _, deps = world
    item = {"key": "nsud:ids|v1", "meta": {"kind": "nsud", "itemId": "nsud:ids", "version": "1", "ids": ["150000"]}}

    result = run_grab(params(tmp_path, item), deps, lambda *_: None)

    assert nsud.count("searchDecision") == 0
    assert nsud.count("getDecision") == 1
    assert result["metrics"] == {"listed": 1, "taken": 1, "skipped_existing": 0, "pdf_only": 0}
    assert (tmp_path / "raw" / "items" / "nsud_ids.json").is_file()


@pytest.mark.unit
def test_ids_from_the_settings_are_used_for_the_ids_item_and_a_hostile_id_is_dropped(world: tuple[Nsud, FakeTime, GrabDeps], tmp_path: Path) -> None:
    nsud, _, deps = world
    item = {"key": "nsud:ids|v1", "meta": {"kind": "nsud", "itemId": "nsud:ids", "version": "1"}}

    run_grab(params(tmp_path, item, ids=["../x", "150000", "12a"]), deps, lambda *_: None)

    assert nsud.count("getDecision") == 1
    assert [p.name for p in (tmp_path / "raw" / "nsud").iterdir()] == ["150000"]
    assert not (tmp_path / "x").exists()
    assert not (tmp_path.parent / "x").exists()


@pytest.mark.unit
def test_max_docs_cuts_after_n_ids_in_ascending_order(world: tuple[Nsud, FakeTime, GrabDeps], tmp_path: Path) -> None:
    nsud, _, deps = world

    result = run_grab(params(tmp_path, maxDocs=2), deps, lambda *_: None)

    assert result["metrics"]["listed"] == 3
    assert result["metrics"]["taken"] == 2
    assert nsud.count("getDecision") == 2
    index = json.loads((tmp_path / "raw" / "items" / "nsud_2026-09.json").read_text(encoding="utf-8"))
    assert index["ids"] == ["150000", "177749"]
    assert index["listed"] == 3


@pytest.mark.unit
def test_include_pdf_only_false_writes_no_pdf(world: tuple[Nsud, FakeTime, GrabDeps], tmp_path: Path) -> None:
    nsud, _, deps = world

    result = run_grab(params(tmp_path, includePdfOnly=False), deps, lambda *_: None)

    assert nsud.count("/data/att") == 0
    assert not (folder(tmp_path, "177749") / "attachment.pdf").exists()
    assert (folder(tmp_path, "177749") / "decision.json").is_file()
    assert result["metrics"]["pdf_only"] == 1


@pytest.mark.unit
def test_a_missing_id_is_not_taken(tmp_path: Path) -> None:
    nsud = Nsud(["1", "2"], {"1": api_text("1Cdo/1/2026")})
    fake = FakeTime()
    deps = GrabDeps(transport=Server(nsud.route, fake).transport(), sleep=fake.sleep, clock=fake.clock)

    result = run_grab(params(tmp_path), deps, lambda *_: None)

    assert result["metrics"]["listed"] == 2
    assert result["metrics"]["taken"] == 1
    assert not folder(tmp_path, "2").exists()


@pytest.mark.unit
def test_an_empty_month_completes_as_an_empty_item(tmp_path: Path) -> None:
    nsud = Nsud([], {})
    fake = FakeTime()
    deps = GrabDeps(transport=Server(nsud.route, fake).transport(), sleep=fake.sleep, clock=fake.clock)

    result = run_grab(params(tmp_path), deps, lambda *_: None)

    assert result["metrics"] == {"listed": 0, "taken": 0, "skipped_existing": 0, "pdf_only": 0}
    index = json.loads((tmp_path / "raw" / "items" / "nsud_2026-09.json").read_text(encoding="utf-8"))
    assert index["ids"] == []
    assert index["complete"] is True


@pytest.mark.unit
def test_null_settings_behave_as_absent(world: tuple[Nsud, FakeTime, GrabDeps], tmp_path: Path) -> None:
    _, _, deps = world
    nulls = {"ids": None, "maxDocs": None, "includePdfOnly": None, "verifyTls": None, "delaySeconds": None, "baseUrl": None, "annoFrom": None, "annoTo": None, "dryRun": None}

    result = run_grab(params(tmp_path, **nulls), deps, lambda *_: None)

    assert result["metrics"]["taken"] == 3


@pytest.mark.unit
def test_a_day_at_the_cap_fails_the_item_with_system_error_naming_the_day(world: tuple[Nsud, FakeTime, GrabDeps], tmp_path: Path) -> None:
    nsud, _, deps = world
    nsud.capped_day = "2026-09-01"

    with pytest.raises(GrabError) as caught:
        run_grab(params(tmp_path), deps, lambda *_: None)

    assert caught.value.code == SYSTEM_ERROR
    assert "window_capped" in str(caught.value)
    assert "2026-09-01" in str(caught.value)
    assert not (tmp_path / "raw" / "items" / "nsud_2026-09.json").exists()


@pytest.mark.unit
def test_a_failing_item_does_not_stop_the_next_one(tmp_path: Path) -> None:
    bad = {"key": "nsud:2026-08|v1", "meta": {"kind": "nsud", "itemId": "nsud:2026-08", "version": "1", "from": "2026-08-01", "to": "2026-08-31"}}
    nsud = Nsud(["150000"], {"150000": fixture_bytes("decision-150000.json")})
    call_count = {"n": 0}

    def route(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if "art_datrozh_od=2026-08" in str(request.url):
            return httpx.Response(404)
        return nsud.route(request)

    fake = FakeTime()
    custom = GrabDeps(transport=Server(route, fake).transport(), sleep=fake.sleep, clock=fake.clock)

    with pytest.raises(GrabError) as caught:
        run_grab(params(tmp_path, bad, MONTH), custom, lambda *_: None)

    assert (tmp_path / "raw" / "items" / "nsud_2026-09.json").is_file(), "the good item was written"
    assert "nsud:2026-08" in str(caught.value)
    assert caught.value.code == CONFIG_ERROR


@pytest.mark.unit
def test_a_host_outside_the_allow_list_is_a_config_error(world: tuple[Nsud, FakeTime, GrabDeps], tmp_path: Path) -> None:
    _, _, deps = world

    with pytest.raises(GrabError) as caught:
        run_grab(params(tmp_path, baseUrl="https://evil.example"), deps, lambda *_: None)

    assert caught.value.code == CONFIG_ERROR


@pytest.mark.unit
@pytest.mark.parametrize("bad", [{"items": []}, {"dir": ""}, {"items": [{"key": "k", "meta": {"kind": "other", "itemId": "x"}}]}, {"items": [{"key": "k", "meta": {"kind": "nsud", "itemId": "nsud:abc"}}]}])
def test_bad_parameters_are_config_errors(world: tuple[Nsud, FakeTime, GrabDeps], tmp_path: Path, bad: dict[str, Any]) -> None:
    _, _, deps = world

    with pytest.raises(GrabError) as caught:
        run_grab({**params(tmp_path), **bad}, deps, lambda *_: None)

    assert caught.value.code == CONFIG_ERROR


@pytest.mark.unit
def test_the_month_window_comes_from_the_item_id_when_the_meta_has_no_bounds(world: tuple[Nsud, FakeTime, GrabDeps], tmp_path: Path) -> None:
    nsud, _, deps = world
    item = {"key": "k", "meta": {"kind": "nsud", "itemId": "nsud:2024-02", "version": "1"}}

    run_grab(params(tmp_path, item), deps, lambda *_: None)

    assert nsud.calls[0] == "searchDecision&art_datrozh_od=2024-02-01&art_datrozh_do=2024-02-29"


@pytest.mark.unit
def test_the_proc_protocol_loop(world: tuple[Nsud, FakeTime, GrabDeps], tmp_path: Path) -> None:
    _, _, deps = world
    lines = [
        json.dumps({"_action": "execute", "_meta": {"executionId": "e1"}, **params(tmp_path)}),
        json.dumps({"_action": "bogus", "_meta": {"executionId": "e1"}}),
        "not json",
        json.dumps({"_action": "terminate", "_meta": {"executionId": "e1"}}),
    ]
    stdout = io.StringIO()

    code = main.main(io.StringIO("\n".join(lines) + "\n"), stdout, deps)

    events = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert code == 1
    assert [e["type"] for e in events].count("result") == 1
    assert [e["code"] for e in events if e["type"] == "error"] == [CONFIG_ERROR, CONFIG_ERROR]
