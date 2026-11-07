"""Fixed clock. Services never call now(); they take `as_of` (default AS_OF).

All stored timestamps are UTC ISO-8601 with a trailing Z. Pacific time is for display only.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
PACIFIC = ZoneInfo("America/Los_Angeles")

# 2026-11-07 12:00 PST (DST ended 2026-11-01) == 20:00 UTC.
AS_OF = datetime(2026, 11, 7, 20, 0, 0, tzinfo=UTC)


def iso_z(dt: datetime) -> str:
    """datetime -> 'YYYY-MM-DDTHH:MM:SSZ' (UTC)."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_z(s: str) -> datetime:
    """'...Z' string -> aware UTC datetime."""
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def to_pacific(dt: datetime) -> str:
    """Display string with the zone shown, e.g. '2026-11-07 12:00 PST'."""
    return dt.astimezone(PACIFIC).strftime("%Y-%m-%d %H:%M %Z")


def to_pacific_iso(dt: datetime) -> str:
    """Pacific ISO string with offset, e.g. '2026-11-07T12:00:00-08:00'."""
    return dt.astimezone(PACIFIC).isoformat()


def pacific_date(iso_z_str: str) -> str:
    """'2026-10-14T20:00:00Z' -> 'October 14, 2026' (Pacific calendar date), used in prose."""
    d = parse_z(iso_z_str).astimezone(PACIFIC)
    return f"{d.strftime('%B')} {d.day}, {d.year}"
