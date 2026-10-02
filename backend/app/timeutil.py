"""Time-zone, country and slot-window helpers.

Rules of the platform:
* every timestamp in the database is UTC (``timestamptz``);
* scanner slots are *local wall-clock windows* (e.g. 08-10 in ``Asia/Kolkata``) so they keep
  meaning the same thing across DST changes - they are converted to UTC instants on demand;
* date arithmetic uses :mod:`zoneinfo` (correct by construction); :mod:`pytz` is used for the
  country <-> time-zone tables it ships.
"""

from __future__ import annotations

import datetime as dt
import difflib
import re
from collections import defaultdict
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import pytz

UTC = dt.UTC

# Legacy IANA names that browsers / older devices still report -> canonical names.
_TZ_ALIASES = {
    "Asia/Calcutta": "Asia/Kolkata",
    "Asia/Saigon": "Asia/Ho_Chi_Minh",
    "Asia/Katmandu": "Asia/Kathmandu",
    "Asia/Rangoon": "Asia/Yangon",
    "Asia/Dacca": "Asia/Dhaka",
    "Asia/Thimbu": "Asia/Thimphu",
    "Asia/Ulan_Bator": "Asia/Ulaanbaatar",
    "Europe/Kiev": "Europe/Kyiv",
    "America/Buenos_Aires": "America/Argentina/Buenos_Aires",
    "America/Indianapolis": "America/Indiana/Indianapolis",
    "Atlantic/Faeroe": "Atlantic/Faroe",
    "Pacific/Samoa": "Pacific/Pago_Pago",
    "Africa/Asmera": "Africa/Asmara",
    "US/Eastern": "America/New_York",
    "US/Central": "America/Chicago",
    "US/Mountain": "America/Denver",
    "US/Pacific": "America/Los_Angeles",
}


def utcnow() -> dt.datetime:
    return dt.datetime.now(UTC)


def ensure_utc(value: dt.datetime) -> dt.datetime:
    """Treat naive datetimes as UTC and convert aware ones."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


# ── time zones ──────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _valid_names() -> frozenset[str]:
    # NB: ``pytz.all_timezones_set`` is a lazy set that copies as empty; use the list instead.
    return frozenset(list(pytz.all_timezones)) & frozenset(available_timezones())


def normalize_timezone(name: str | None) -> str | None:
    """Return a canonical, usable IANA name or ``None`` when ``name`` is not a time zone."""
    if not name or not isinstance(name, str):
        return None
    name = name.strip()
    name = _TZ_ALIASES.get(name, name)
    return name if name in _valid_names() else None


def is_valid_timezone(name: str | None) -> bool:
    return normalize_timezone(name) is not None


@lru_cache(maxsize=512)
def get_tz(name: str) -> ZoneInfo:
    canonical = normalize_timezone(name)
    if canonical is None:
        raise ValueError(f"Unknown time zone: {name!r}")
    try:
        return ZoneInfo(canonical)
    except ZoneInfoNotFoundError as exc:  # pragma: no cover - guarded by normalize_timezone
        raise ValueError(f"Unknown time zone: {name!r}") from exc


def to_local(moment: dt.datetime, tzname: str) -> dt.datetime:
    return ensure_utc(moment).astimezone(get_tz(tzname))


def utc_offset(tzname: str, at: dt.datetime | None = None) -> dt.timedelta:
    return to_local(at or utcnow(), tzname).utcoffset() or dt.timedelta(0)


def format_offset(tzname: str, at: dt.datetime | None = None) -> str:
    """``UTC+05:30`` / ``UTC-08:00`` / ``UTC+00:00``."""
    total = int(utc_offset(tzname, at).total_seconds() // 60)
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"UTC{sign}{total // 60:02d}:{total % 60:02d}"


def tz_label(tzname: str, at: dt.datetime | None = None) -> str:
    """Short label shown to people: ``IST``, ``PKT``, ``EST`` ... or ``UTC+03:00`` when the zone
    has no alphabetic abbreviation."""
    abbr = to_local(at or utcnow(), tzname).tzname() or ""
    if abbr.isalpha() and 2 <= len(abbr) <= 5:
        return abbr
    return format_offset(tzname, at)


def format_local(moment: dt.datetime | None, tzname: str | None, fmt: str = "%d %b %Y, %H:%M") -> str:
    if moment is None:
        return "-"
    if not tzname or not is_valid_timezone(tzname):
        return ensure_utc(moment).strftime(fmt) + " UTC"
    return f"{to_local(moment, tzname).strftime(fmt)} {tz_label(tzname, moment)}"


# ── countries ───────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _tz_to_countries() -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = defaultdict(list)
    for code in pytz.country_timezones:
        for tz in pytz.country_timezones[code]:
            mapping[tz].append(code)
    return {tz: sorted(codes) for tz, codes in mapping.items()}


def countries_for_timezone(tzname: str) -> list[str]:
    canonical = normalize_timezone(tzname)
    if not canonical:
        return []
    return list(_tz_to_countries().get(canonical, []))


def timezones_for_country(code: str) -> list[str]:
    try:
        return [tz for tz in pytz.country_timezones[code.upper()] if tz in _valid_names()]
    except KeyError:
        return []


# pytz's names are terse / inverted for a few countries; show the names people actually use
_NAME_OVERRIDES = {
    "GB": "United Kingdom",
    "KP": "North Korea",
    "KR": "South Korea",
    "CD": "DR Congo",
    "CG": "Republic of the Congo",
    "MM": "Myanmar",
    "SZ": "Eswatini",
    "WS": "Samoa",
    "AS": "American Samoa",
}


def is_valid_country(code: str | None) -> bool:
    return bool(code) and code.upper() in pytz.country_names


def country_name(code: str | None) -> str:
    if not code:
        return "-"
    code = code.upper()
    return _NAME_OVERRIDES.get(code) or pytz.country_names.get(code, code)


def country_flag(code: str | None) -> str:
    if not code or len(code) != 2 or not code.isalpha():
        return "🌐"
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code.upper())


@lru_cache(maxsize=1)
def _country_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for code, name in pytz.country_names.items():
        index[code.lower()] = code
        index[_norm(name)] = code
        index[_norm(country_name(code))] = code
    # common short names / alternates
    extra = {
        "uk": "GB",
        "britain": "GB",
        "england": "GB",
        "usa": "US",
        "america": "US",
        "uae": "AE",
        "emirates": "AE",
        "russia": "RU",
        "south korea": "KR",
        "korea": "KR",
        "vietnam": "VN",
        "turkey": "TR",
        "iran": "IR",
        "czech republic": "CZ",
        "ivory coast": "CI",
    }
    for key, code in extra.items():
        index[key] = code
    return index


def _norm(text: str) -> str:
    return re.sub(r"[^a-z ]", "", text.lower()).strip()


def find_countries(query: str, limit: int = 5) -> list[str]:
    """Match a free-text country name / ISO code to ISO alpha-2 codes (best first)."""
    query = (query or "").strip()
    if not query:
        return []
    index = _country_index()
    exact = index.get(query.lower()) or index.get(_norm(query))
    if exact:
        return [exact]
    names = [k for k in index if len(k) > 2]
    hits = difflib.get_close_matches(_norm(query), names, n=limit * 2, cutoff=0.8)
    out: list[str] = []
    for hit in hits:
        code = index[hit]
        if code not in out:
            out.append(code)
    # prefix / substring matches are helpful for partial typing ("indo" -> Indonesia)
    if len(out) < limit:
        q = _norm(query)
        for key, code in index.items():
            if len(key) > 2 and q and (key.startswith(q) or q in key) and code not in out:
                out.append(code)
    return out[:limit]


def guess_country(tzname: str, hints: list[str] | None = None) -> tuple[str | None, list[str]]:
    """Pick the country for a time zone.

    Returns ``(country, candidates)``. ``country`` is ``None`` when it is ambiguous (the zone
    is shared by several countries and no hint disambiguates) - the caller should ask.
    ``hints`` are region subtags from the device locale (``["IN", "GB"]``).
    """
    candidates = countries_for_timezone(tzname)
    if len(candidates) == 1:
        return candidates[0], candidates
    for hint in hints or []:
        if hint.upper() in candidates:
            return hint.upper(), candidates
    return None, candidates


def region_hints(languages: list[str] | None) -> list[str]:
    """``["en-IN", "hi"]`` -> ``["IN"]``."""
    hints: list[str] = []
    for lang in languages or []:
        parts = re.split(r"[-_]", str(lang))
        for part in parts[1:]:
            if len(part) == 2 and part.isalpha():
                hints.append(part.upper())
    return hints


# ── slot windows ────────────────────────────────────────────────────────────


def slot_label(start: int, end: int) -> str:
    return f"{start:02d}-{end:02d}"


def slot_length_hours(start: int, end: int) -> int:
    return (end - start) % 24 or 24


def occurrence_bounds(start_hour: int, end_hour: int, tzname: str, local_date: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """UTC instants for the occurrence of the local window that *starts* on ``local_date``."""
    tz = get_tz(tzname)
    start_local = dt.datetime.combine(local_date, dt.time(start_hour), tzinfo=tz)
    end_date = local_date + dt.timedelta(days=1) if end_hour <= start_hour else local_date
    end_local = dt.datetime.combine(end_date, dt.time(end_hour % 24), tzinfo=tz)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def active_occurrence(start_hour: int, end_hour: int, tzname: str, now: dt.datetime) -> tuple[dt.datetime, dt.datetime] | None:
    """The occurrence of the slot that covers ``now`` (UTC instants), if any."""
    now = ensure_utc(now)
    today = to_local(now, tzname).date()
    for day in (today - dt.timedelta(days=1), today):
        start, end = occurrence_bounds(start_hour, end_hour, tzname, day)
        if start <= now < end:
            return start, end
    return None


def next_occurrence(start_hour: int, end_hour: int, tzname: str, now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """The first occurrence that starts strictly after ``now``."""
    now = ensure_utc(now)
    today = to_local(now, tzname).date()
    best: tuple[dt.datetime, dt.datetime] | None = None
    for offset in range(0, 3):
        start, end = occurrence_bounds(start_hour, end_hour, tzname, today + dt.timedelta(days=offset))
        if start > now and (best is None or start < best[0]):
            best = (start, end)
    assert best is not None  # a start strictly after now always exists within 3 days
    return best


def utc_window_text(start_hour: int, end_hour: int, tzname: str, at: dt.datetime | None = None) -> str:
    """``02:30-04:30 UTC`` for the occurrence around ``at`` (used by the admin panel)."""
    at = ensure_utc(at or utcnow())
    occ = active_occurrence(start_hour, end_hour, tzname, at) or next_occurrence(start_hour, end_hour, tzname, at)
    return f"{occ[0].strftime('%H:%M')}-{occ[1].strftime('%H:%M')} UTC"
