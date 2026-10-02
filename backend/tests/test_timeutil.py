import datetime as dt

import pytest

from app import timeutil as t

UTC = dt.UTC


def at(*args):
    return dt.datetime(*args, tzinfo=UTC)


def test_labels_and_offsets():
    now = at(2026, 10, 2, 6, 0)
    assert t.tz_label("Asia/Kolkata", now) == "IST"
    assert t.format_offset("Asia/Kolkata", now) == "UTC+05:30"
    assert t.format_offset("America/New_York", now) == "UTC-04:00"
    # zones without an alphabetic abbreviation fall back to the numeric offset
    assert t.tz_label("Asia/Dhaka", now) == "UTC+06:00"


def test_timezone_validation_and_aliases():
    assert t.normalize_timezone("Asia/Calcutta") == "Asia/Kolkata"
    assert t.normalize_timezone("Mars/Base") is None
    assert t.normalize_timezone("") is None
    assert t.normalize_timezone(None) is None
    assert t.is_valid_timezone("Europe/London")
    with pytest.raises(ValueError):
        t.get_tz("Nope/Nothing")


def test_country_detection():
    assert t.countries_for_timezone("Asia/Kolkata") == ["IN"]
    assert t.guess_country("Asia/Dhaka") == ("BD", ["BD"])
    assert t.guess_country("UTC") == (None, [])
    assert t.region_hints(["en-IN", "hi", "pt_BR"]) == ["IN", "BR"]
    assert t.is_valid_country("in") and not t.is_valid_country("ZZ")


def test_country_search():
    assert t.find_countries("india") == ["IN"]
    assert t.find_countries("PK") == ["PK"]
    assert t.find_countries("uk") == ["GB"]
    assert "ID" in t.find_countries("indo")
    assert t.find_countries("") == []
    assert t.find_countries("unitd states")[0] == "US"
    assert t.country_flag("IN") == "🇮🇳"


def test_slot_window_local_time_ist():
    # 08-10 IST == 02:30-04:30 UTC
    assert t.active_occurrence(8, 10, "Asia/Kolkata", at(2026, 10, 2, 2, 29)) is None
    start, end = t.active_occurrence(8, 10, "Asia/Kolkata", at(2026, 10, 2, 2, 30))
    assert (start, end) == (at(2026, 10, 2, 2, 30), at(2026, 10, 2, 4, 30))
    assert t.active_occurrence(8, 10, "Asia/Kolkata", at(2026, 10, 2, 4, 29)) is not None
    assert t.active_occurrence(8, 10, "Asia/Kolkata", at(2026, 10, 2, 4, 30)) is None


def test_slot_wraps_midnight():
    # 22-00 IST == 16:30-18:30 UTC
    assert t.active_occurrence(22, 0, "Asia/Kolkata", at(2026, 10, 2, 18, 15)) is not None
    assert t.active_occurrence(22, 0, "Asia/Kolkata", at(2026, 10, 2, 18, 45)) is None
    # 23:00-01:00 style windows spanning midnight in local time
    start, end = t.active_occurrence(23, 1, "UTC", at(2026, 10, 2, 0, 30))
    assert (start, end) == (at(2026, 10, 1, 23, 0), at(2026, 10, 2, 1, 0))
    assert t.active_occurrence(0, 2, "America/New_York", at(2026, 10, 2, 5, 30)) is not None


def test_slot_dst_changes_are_local_wall_clock():
    # spring forward (US, 2026-03-08): local 02-04 still maps to a valid 1h window, never raises
    start, end = t.occurrence_bounds(2, 4, "America/New_York", dt.date(2026, 3, 8))
    assert end > start
    # fall back (2026-11-01): local 00-02 is 3 real hours long
    start, end = t.occurrence_bounds(0, 2, "America/New_York", dt.date(2026, 11, 1))
    assert end - start == dt.timedelta(hours=3)
    # the same local slot lands on a different UTC time after the DST switch
    summer = t.occurrence_bounds(8, 10, "Europe/London", dt.date(2026, 7, 1))[0]
    winter = t.occurrence_bounds(8, 10, "Europe/London", dt.date(2026, 12, 1))[0]
    assert summer.hour == 7 and winter.hour == 8


def test_next_occurrence_and_lengths():
    now = at(2026, 10, 2, 6, 0)
    start, end = t.next_occurrence(8, 10, "Asia/Kolkata", now)
    assert start == at(2026, 10, 3, 2, 30) and end == at(2026, 10, 3, 4, 30)
    # a slot that is running right now is not "next"
    start, _ = t.next_occurrence(8, 10, "Asia/Kolkata", at(2026, 10, 2, 3, 0))
    assert start == at(2026, 10, 3, 2, 30)
    assert t.slot_length_hours(22, 0) == 2 and t.slot_label(22, 0) == "22-00"
    assert t.utc_window_text(8, 10, "Asia/Kolkata", now) == "02:30-04:30 UTC"


def test_format_local():
    assert t.format_local(at(2026, 10, 2, 6, 0), "Asia/Kolkata") == "02 Oct 2026, 11:30 IST"
    assert t.format_local(None, "Asia/Kolkata") == "-"
    assert t.format_local(at(2026, 10, 2, 6, 0), "bogus").endswith("UTC")
