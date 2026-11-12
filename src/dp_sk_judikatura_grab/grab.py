"""The ``execute`` action of the grab (P81-04): per item (a month of decision dates, or the explicit-ids item) write

* ``<dir>/nsud/<id>/decision.json``: the raw API body, verbatim;
* ``<dir>/nsud/<id>/attachment.pdf``: the PDF of a decision without API text (``obsah`` empty, ``subor`` set) when ``includePdfOnly``;
* ``<dir>/nsud/<id>/fetch.json``: sidecar ``{id, version, fetchedAt, pdfUrl, subor, pageUrl}``;
* ``<dir>/items/<item>.json``: index ``{itemId, version, listed, ids, complete}`` that convert reads.

Idempotent restart: an item whose index is complete for the same version is not touched (zero requests); otherwise the window is listed again and each id whose
``decision.json`` and ``fetch.json`` already carry the same version are skipped. An item that fails (window at the cap, unreachable source) does not stop the
next one; the run ends with one error naming the failed items. Null settings are absent (P79-30).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dp_sk_judikatura_grab import http, windows
from dp_sk_judikatura_grab.errors import CONFIG_ERROR, SYSTEM_ERROR, GrabError
from dp_sk_judikatura_grab.nsud import DEFAULT_BASE_URL, DEFAULT_PAUSE, NsudClient, Pacer, digits_only

if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx

KIND = "nsud"
IDS_ITEM = "nsud:ids"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class GrabDeps:
    """The ports of a run: transport (None = real network), sleep and clock (pacing)."""

    transport: httpx.BaseTransport | None = None
    sleep: Callable[[float], None] = field(default=time.sleep)
    clock: Callable[[], float] = field(default=time.monotonic)


def safe_name(item_id: str) -> str:
    """File name of an item id (identical to the convert and ingest classes)."""
    return _UNSAFE.sub("_", item_id.replace(" ", "-"))


def _validate_items(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list) or not items:
        raise GrabError(CONFIG_ERROR, "items must be a non-empty array")
    for entry in items:
        meta = entry.get("meta") if isinstance(entry, dict) else None
        if not isinstance(entry, dict) or not entry.get("key") or not isinstance(meta, dict):
            raise GrabError(CONFIG_ERROR, f"item needs a key and a meta: {entry!r}")
        item_id = meta.get("itemId")
        if meta.get("kind") != KIND or not isinstance(item_id, str) or not item_id:
            raise GrabError(CONFIG_ERROR, f"item {entry['key']}: meta.kind must be '{KIND}' and meta.itemId is required")
        if item_id != IDS_ITEM and not (meta.get("from") and meta.get("to")):
            try:
                windows.month_bounds(item_id)
            except ValueError as error:
                raise GrabError(CONFIG_ERROR, f"item {entry['key']}: {error}") from None
    return items


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_bytes(data)
    temporary.replace(path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@dataclass
class Tally:
    listed: int = 0
    taken: int = 0
    skipped_existing: int = 0
    pdf_only: int = 0

    def add(self, other: Tally) -> None:
        self.listed += other.listed
        self.taken += other.taken
        self.skipped_existing += other.skipped_existing
        self.pdf_only += other.pdf_only

    def as_dict(self) -> dict[str, int]:
        return {"listed": self.listed, "taken": self.taken, "skipped_existing": self.skipped_existing, "pdf_only": self.pdf_only}


@dataclass
class Settings:
    ids: list[str] | None
    max_docs: int | None
    include_pdf_only: bool
    base_url: str


def _is_current(folder: Path, version: str, *, need_pdf: bool) -> bool:
    sidecar = _read_json(folder / "fetch.json")
    ok = (folder / "decision.json").is_file() and isinstance(sidecar, dict) and sidecar.get("version") == version
    return ok and (not need_pdf or (folder / "attachment.pdf").is_file())


def _fetch_one(client: NsudClient, root: Path, decision_id: str, version: str, settings: Settings) -> tuple[bool, bool]:
    """(taken, pdf_only): writes the decision of one id; a missing id (empty list) is not taken."""
    body = client.get_decision(decision_id)
    if body is None:
        return False, False
    folder = root / KIND / decision_id
    _write_atomic(folder / "decision.json", body)
    try:
        parsed = json.loads(body)
        obj = parsed[0] if isinstance(parsed, list) and parsed else parsed
    except ValueError:
        obj = None
    obj = obj if isinstance(obj, dict) else {}
    values = {key.lower(): value for key, value in obj.items()}
    subor = values.get("subor") if isinstance(values.get("subor"), str) and values.get("subor") else None
    has_text = isinstance(values.get("obsah"), str) and bool(str(values.get("obsah")).strip())
    pdf_only = not has_text and subor is not None
    if pdf_only and settings.include_pdf_only and subor is not None:
        _write_atomic(folder / "attachment.pdf", client.download_attachment(subor))
    sidecar = {
        "id": decision_id,
        "version": version,
        "fetchedAt": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pdfUrl": f"{settings.base_url.rstrip('/')}/data/att/{subor}" if subor else None,
        "subor": subor,
        "pageUrl": None,
    }
    _write_atomic(folder / "fetch.json", json.dumps(sidecar, ensure_ascii=False).encode("utf-8"))
    return True, pdf_only


def _item_ids(client: NsudClient, meta: dict[str, Any], settings: Settings) -> tuple[list[str], int]:
    """(ids to take in ascending order, number listed): explicit ids make no window call."""
    if meta["itemId"] == IDS_ITEM:
        explicit: list[Any] = meta["ids"] if isinstance(meta.get("ids"), list) else (settings.ids or [])
        ids = sorted({str(i) for i in explicit if digits_only(str(i))}, key=int)
        return ids, len(ids)
    if meta.get("from") and meta.get("to"):
        start, end = (datetime.fromisoformat(meta["from"]).date(), datetime.fromisoformat(meta["to"]).date())
    else:
        start, end = windows.month_bounds(meta["itemId"])
    return windows.slice_window(client, start, end), -1


def _grab_item(client: NsudClient, root: Path, entry: dict[str, Any], settings: Settings) -> Tally:
    meta = entry["meta"]
    version = str(meta.get("version", "1"))
    index_path = root / "items" / f"{safe_name(meta['itemId'])}.json"
    index = _read_json(index_path)
    if isinstance(index, dict) and index.get("complete") is True and index.get("version") == version:
        done = index.get("ids", [])
        pdf_only = sum(1 for i in done if _is_pdf_only(root / KIND / str(i)))
        return Tally(listed=int(index.get("listed", len(done))), taken=len(done), skipped_existing=len(done), pdf_only=pdf_only)
    ids, listed = _item_ids(client, meta, settings)
    listed = len(ids) if listed < 0 else listed
    limit = meta.get("maxDocs") or settings.max_docs
    wanted = ids[: int(limit)] if limit else ids
    tally = Tally(listed=listed)
    taken: list[str] = []
    for decision_id in wanted:
        folder = root / KIND / decision_id
        decision_pdf_only = _is_pdf_only(folder)
        if _is_current(folder, version, need_pdf=settings.include_pdf_only and decision_pdf_only):
            tally.skipped_existing += 1
            tally.taken += 1
            tally.pdf_only += 1 if decision_pdf_only else 0
            taken.append(decision_id)
            continue
        got, pdf_only = _fetch_one(client, root, decision_id, version, settings)
        if got:
            tally.taken += 1
            tally.pdf_only += 1 if pdf_only else 0
            taken.append(decision_id)
    document = {"itemId": meta["itemId"], "version": version, "listed": listed, "ids": taken, "complete": True}
    _write_atomic(index_path, json.dumps(document, ensure_ascii=False).encode("utf-8"))
    return tally


def _is_pdf_only(folder: Path) -> bool:
    obj = _read_json(folder / "decision.json")
    obj = obj[0] if isinstance(obj, list) and obj else obj
    if not isinstance(obj, dict):
        return False
    values = {key.lower(): value for key, value in obj.items()}
    return not str(values.get("obsah") or "").strip() and bool(values.get("subor"))


def _settings(params: dict[str, Any]) -> tuple[Settings, float, bool]:
    ids = params.get("ids")
    max_docs = params.get("maxDocs")
    delay = params.get("delaySeconds", DEFAULT_PAUSE)
    if ids is not None and not (isinstance(ids, list) and all(isinstance(i, str) for i in ids)):
        raise GrabError(CONFIG_ERROR, "ids must be an array of strings")
    if max_docs is not None and (not isinstance(max_docs, int) or isinstance(max_docs, bool) or max_docs < 1):
        raise GrabError(CONFIG_ERROR, "maxDocs must be an integer >= 1")
    if not isinstance(delay, (int, float)) or isinstance(delay, bool):
        raise GrabError(CONFIG_ERROR, "delaySeconds must be a number")
    base = params.get("baseUrl", DEFAULT_BASE_URL)
    if not isinstance(base, str) or not base:
        raise GrabError(CONFIG_ERROR, "baseUrl must be a string")
    return Settings(ids, max_docs, bool(params.get("includePdfOnly", True)), base), float(delay), bool(params.get("verifyTls", True))


def run_grab(raw_params: dict[str, Any], deps: GrabDeps, on_progress: Callable[[int, str], None]) -> dict[str, Any]:
    """Grabs every item; failed items are reported together as one error after the others were written."""
    params = {name: value for name, value in raw_params.items() if value is not None}
    items = _validate_items(params.get("items"))
    directory = params.get("dir")
    if not isinstance(directory, str) or not directory:
        raise GrabError(CONFIG_ERROR, "dir is required")
    settings, delay, verify_tls = _settings(params)
    root = Path(directory)
    client = NsudClient(
        http.build_client(http.DEFAULT_ALLOWED_HOSTS, deps.transport, verify_tls=verify_tls),
        Pacer(delay, clock=deps.clock, sleep=deps.sleep), settings.base_url, sleep=deps.sleep,
    )
    total = Tally()
    reported: list[dict[str, Any]] = []
    failures: list[tuple[str, GrabError]] = []
    for position, entry in enumerate(items):
        try:
            tally = _grab_item(client, root, entry, settings)
        except GrabError as error:
            failures.append((entry["key"], error))
        else:
            total.add(tally)
            reported.append({"key": entry["key"], **tally.as_dict()})
        on_progress(int((position + 1) * 100 / len(items)), f"{position + 1}/{len(items)} {entry['key']}")
    if failures:
        codes = {error.code for _, error in failures}
        code = next(iter(codes)) if len(codes) == 1 else SYSTEM_ERROR
        detail = "; ".join(f"{key} ({error})" for key, error in failures)
        raise GrabError(code, f"{len(failures)} of {len(items)} items failed: {detail}")
    return {"dir": directory, "items": reported, "metrics": total.as_dict()}
