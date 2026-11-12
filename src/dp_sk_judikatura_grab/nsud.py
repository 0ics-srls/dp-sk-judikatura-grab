"""Client of the OpenData API of the Supreme Court of the Slovak Republic (https://www.nsud.sk/ws/opendata.php), port of the legacy NsudApi.

Politeness (P81-13): a pause of at least ``MIN_PAUSE`` between requests (default 0.4 s), a browser User-Agent, one request at a time, three attempts
with a growing back-off on 500/502/503/504/timeouts. The windowed enumeration is ``searchDecision&art_datrozh_od/do`` (P81-04, P81-06): the documented
``getLastDecision`` answers 504 on every call and the documented ``art_datum_*`` names are ignored by the API.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

from dp_sk_judikatura_grab.errors import CONFIG_ERROR, TRANSIENT_ERROR, GrabError
from dp_sk_judikatura_grab.http import DEFAULT_ALLOWED_HOSTS, status_error

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date

DEFAULT_BASE_URL = "https://www.nsud.sk"
API_PATH = "/ws/opendata.php"
MIN_PAUSE = 0.3
DEFAULT_PAUSE = 0.4
MAX_ATTEMPTS = 3
BACKOFF_SEC = 5.0
_SUBOR = re.compile(r"^[0-9A-Za-z]+/[0-9A-Za-z._-]+\.pdf$")


class Pacer:
    """At least ``interval`` seconds (never below ``MIN_PAUSE``) between the starts of two requests; clock and sleep are injected."""

    def __init__(self, interval: float = DEFAULT_PAUSE, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        self._interval = max(interval, MIN_PAUSE)
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            remaining = self._interval - (self._clock() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()


def digits_only(value: str) -> bool:
    """A decision id is ASCII digits only: it ends up in file paths (path-traversal guard of the legacy NsudJson)."""
    return bool(value) and value.isascii() and value.isdigit()


class NsudClient:
    """The three calls the grab needs. Network errors are GrabErrors with a protocol code."""

    def __init__(self, http: httpx.Client, pacer: Pacer, base_url: str = DEFAULT_BASE_URL, sleep: Callable[[float], None] = time.sleep) -> None:
        host = (urlsplit(base_url).hostname or "").lower()
        if host not in DEFAULT_ALLOWED_HOSTS:
            msg = f"baseUrl host {host or base_url!r} is not allowed; use {DEFAULT_ALLOWED_HOSTS[0]} (or host.docker.internal for a replay server)"
            raise GrabError(CONFIG_ERROR, msg)
        self._http = http
        self._pacer = pacer
        self._base = base_url.rstrip("/")
        self._sleep = sleep

    def _get(self, url: str) -> httpx.Response:
        last: GrabError | None = None
        for attempt in range(MAX_ATTEMPTS):
            if attempt:
                self._sleep(BACKOFF_SEC * attempt)
            self._pacer.wait()
            try:
                response = self._http.get(url)
            except httpx.TransportError as error:  # includes timeouts and connection errors
                last = GrabError(TRANSIENT_ERROR, f"{type(error).__name__} for {url}")
                continue
            if response.status_code == httpx.codes.OK:
                return response
            failure = status_error(url, response.status_code)
            if failure.code != TRANSIENT_ERROR:
                raise failure
            last = failure
        assert last is not None
        raise last

    def search_ids(self, date_from: date, date_to: date) -> list[str]:
        """Decision ids with a decision date in the window (the list is capped at 1000 by the source); anything that is not digits is dropped."""
        url = f"{self._base}{API_PATH}?searchDecision&art_datrozh_od={date_from.isoformat()}&art_datrozh_do={date_to.isoformat()}"
        try:
            body = json.loads(self._get(url).content)
        except ValueError:
            return []
        if not isinstance(body, list):
            return []
        ids = (str(element) for element in body if element is not None and not isinstance(element, bool))
        return [item for item in ids if digits_only(item)]

    def get_decision(self, decision_id: str) -> bytes | None:
        """The raw JSON body of a decision, or None when the source answers an empty list (a missing id)."""
        if not digits_only(decision_id):
            msg = f"decision id must be digits only: {decision_id!r}"
            raise GrabError(CONFIG_ERROR, msg)
        body = self._get(f"{self._base}{API_PATH}?getDecision&id={decision_id}").content
        return None if body.strip() in {b"", b"[]"} else body

    def download_attachment(self, subor: str) -> bytes:
        """The PDF attachment ``/data/att/<subor>``; the path must have the shape ``<hex dir>/<file>.pdf`` and the answer must be a PDF."""
        if not _SUBOR.match(subor) or ".." in subor:
            msg = f"attachment path has an unexpected shape: {subor!r}"
            raise GrabError(CONFIG_ERROR, msg)
        body = self._get(f"{self._base}/data/att/{subor}").content
        if not body.startswith(b"%PDF"):
            msg = f"attachment {subor} is not a PDF"
            raise GrabError(CONFIG_ERROR, msg)
        return body


def page_slug(cislo: str) -> str:
    """Slug of the decision page ``/rozhodnutia/<slug>/`` (live probe, P81-08): the case number folded to ASCII, lower-case, without punctuation."""
    folded = unicodedata.normalize("NFKD", cislo)
    return re.sub(r"[^0-9a-z]", "", "".join(ch for ch in folded if not unicodedata.combining(ch)).lower())
