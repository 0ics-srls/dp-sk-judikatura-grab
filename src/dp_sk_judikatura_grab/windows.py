"""Window slicing (P81-04): the source caps a list at 1000 ids, so a window that answers 1000 is split at its middle day and both halves are asked again;
a single day at the cap is a declared failure, never a silent cut."""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from typing import Protocol

from dp_sk_judikatura_grab.errors import SYSTEM_ERROR, GrabError

CAP = 1000
_MONTH_ITEM = re.compile(r"^nsud:(\d{4})-(0[1-9]|1[0-2])$")


class SearchPort(Protocol):
    def search_ids(self, date_from: date, date_to: date) -> list[str]: ...


class WindowCapped(GrabError):  # noqa: N818 - the name is the declared failure `window_capped`
    """One day holds at least CAP decisions: the list would be cut, so the item fails and names the day."""

    def __init__(self, day: date) -> None:
        super().__init__(SYSTEM_ERROR, f"window_capped: {day.isoformat()} has {CAP} or more decisions, the source list is cut")
        self.day = day


def slice_window(source: SearchPort, first: date, last: date, cap: int = CAP) -> list[str]:
    """All ids of ``first``..``last`` (inclusive), de-duplicated, ascending by numeric value."""
    found: set[str] = set()
    pending = [(first, last)]
    while pending:
        start, end = pending.pop()
        ids = source.search_ids(start, end)
        if len(ids) < cap:
            found.update(ids)
            continue
        if start == end:
            raise WindowCapped(start)
        middle = start + (end - start) // 2
        pending.extend([(middle + timedelta(days=1), end), (start, middle)])
    return sorted(found, key=int)


def month_bounds(item_id: str) -> tuple[date, date]:
    """First and last day of a ``nsud:YYYY-MM`` item."""
    match = _MONTH_ITEM.match(item_id)
    if match is None:
        msg = f"not a month item id: {item_id!r}"
        raise ValueError(msg)
    year, month = int(match.group(1)), int(match.group(2))
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
