"""Window slicing: the API caps a list at 1000 ids, so a window that returns 1000 is halved until it fits (a day is the floor)."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from dp_sk_judikatura_grab.windows import CAP, WindowCapped, month_bounds, slice_window


class FakeSearch:
    """A source holding ``per_day`` ids per day; an answer is cut at CAP (like the real list) and every call is recorded."""

    def __init__(self, first: date, last: date, per_day: int = 1, cap_days: int = 3) -> None:
        self.calls: list[tuple[date, date]] = []
        self._by_day: dict[date, list[str]] = {}
        self._cap_days = cap_days
        next_id = 100000
        day = first
        while day <= last:
            self._by_day[day] = [str(next_id + n) for n in range(per_day)]
            next_id += per_day
            day += timedelta(days=1)

    def search_ids(self, start: date, end: date) -> list[str]:
        """A window longer than cap_days answers CAP ids (the cap), a shorter one answers everything it holds."""
        self.calls.append((start, end))
        days = (end - start).days + 1
        everything = [i for d, ids in self._by_day.items() if start <= d <= end for i in ids]
        if days > self._cap_days:
            return everything[:CAP] if len(everything) >= CAP else [*everything, *(f"9{n:08d}" for n in range(CAP - len(everything)))][:CAP]
        return everything[:CAP]


@pytest.mark.unit
def test_a_window_below_the_cap_is_asked_once() -> None:
    source = FakeSearch(date(2026, 9, 1), date(2026, 9, 30), per_day=1, cap_days=10_000)

    ids = slice_window(source, date(2026, 9, 1), date(2026, 9, 30))

    assert len(ids) == 30
    assert len(source.calls) == 1


class CappedAboveThreeDays:
    """Answers CAP ids for any window longer than three days (each of those ids is a real id of that window) and the real ids otherwise."""

    def __init__(self, first: date, last: date) -> None:
        self.calls: list[tuple[date, date]] = []
        self.truth: dict[date, list[str]] = {}
        self.first, self.last = first, last
        number = 500000
        day = first
        while day <= last:
            # 400 ids a day: a 3-day window holds 1200 (cap), a 2-day window holds 800
            self.truth[day] = [str(number + n) for n in range(400)]
            number += 400
            day += timedelta(days=1)

    def search_ids(self, start: date, end: date) -> list[str]:
        self.calls.append((start, end))
        everything = [i for d, ids in sorted(self.truth.items()) if start <= d <= end for i in ids]
        return everything[:CAP]


@pytest.mark.unit
def test_a_capped_month_is_split_into_windows_whose_union_is_the_month_with_no_id_lost_and_none_twice() -> None:
    source = CappedAboveThreeDays(date(2026, 3, 1), date(2026, 3, 31))

    ids = slice_window(source, date(2026, 3, 1), date(2026, 3, 31))

    expected = sorted((i for ids_ in source.truth.values() for i in ids_), key=int)
    assert ids == expected
    assert len(ids) == len(set(ids)) == 31 * 400
    answered = [c for c in list(source.calls) if len(source.search_ids(*c)) < CAP]
    covered = sorted({d for start, end in answered for d in (start + timedelta(days=n) for n in range((end - start).days + 1))})
    assert covered[0] == date(2026, 3, 1)
    assert covered[-1] == date(2026, 3, 31)
    assert len(covered) == 31


@pytest.mark.unit
def test_a_day_that_alone_reaches_the_cap_raises_window_capped_naming_the_day() -> None:
    class AlwaysCapped:
        def search_ids(self, _start: date, _end: date) -> list[str]:
            return [str(n) for n in range(CAP)]

    with pytest.raises(WindowCapped) as caught:
        slice_window(AlwaysCapped(), date(2026, 3, 1), date(2026, 3, 4))

    assert caught.value.day.year == 2026
    assert caught.value.code == "system_error"
    assert caught.value.day.isoformat() in str(caught.value)


@pytest.mark.unit
def test_ids_are_deduplicated_and_in_ascending_numeric_order() -> None:
    class Overlapping:
        def search_ids(self, start: date, end: date) -> list[str]:
            if start != end:
                return ["1", "2", "3", "4"]  # the cap: split
            return ["200", "30", "4"] if start.day == 1 else ["30", "1000", "9"]

    ids = slice_window(Overlapping(), date(2026, 5, 1), date(2026, 5, 2), cap=4)

    assert ids == ["4", "9", "30", "200", "1000"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("item_id", "first", "last"),
    [
        ("nsud:2026-09", date(2026, 9, 1), date(2026, 9, 30)),
        ("nsud:2026-01", date(2026, 1, 1), date(2026, 1, 31)),
        ("nsud:2024-02", date(2024, 2, 1), date(2024, 2, 29)),
        ("nsud:2025-02", date(2025, 2, 1), date(2025, 2, 28)),
        ("nsud:2026-12", date(2026, 12, 1), date(2026, 12, 31)),
    ],
)
def test_month_bounds_handle_31_day_months_and_leap_years(item_id: str, first: date, last: date) -> None:
    assert month_bounds(item_id) == (first, last)


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["nsud:ids", "nsud:2026-13", "nsud:2026-9", "2026-09", "nsud:abcd-01", ""])
def test_month_bounds_reject_an_item_id_that_is_not_a_month(bad: str) -> None:
    with pytest.raises(ValueError, match="month"):
        month_bounds(bad)
