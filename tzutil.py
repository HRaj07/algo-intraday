"""
Timezone helper.

Uses the standard library's zoneinfo (Python 3.9+, and the workflow runs 3.12)
with a pytz fallback so nothing breaks if an old environment picks this up.

Why this exists: v1 called pytz in four places and the paper log shows August
trades recorded in UTC and September trades in IST, so the entry_time field is
not comparable across the file. One helper, one definition of "now", no drift.
"""
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
    UTC = ZoneInfo("UTC")
except Exception:                                    # pragma: no cover
    import pytz
    IST = pytz.timezone("Asia/Kolkata")
    UTC = pytz.utc


def now_ist() -> datetime:
    """The single source of 'what time is it' for the whole system."""
    return datetime.now(IST)


def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(IST)


def stamp(dt: datetime = None) -> str:
    """Canonical timestamp for logs and trade records. Always IST, always labelled."""
    return (dt or now_ist()).strftime("%Y-%m-%d %H:%M:%S IST")
