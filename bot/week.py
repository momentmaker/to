"""Week math: ISO week keys + fz.ax DOB-based week index.

Stage 3 may promote this to `bot/digest/week.py` when digest logic arrives.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo


def local_now(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name))


def local_date_for(dt: datetime, tz_name: str) -> date:
    return dt.astimezone(ZoneInfo(tz_name)).date()


def iso_week_key(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def fz_week_idx(d: date, dob: date) -> int:
    """Weeks since DOB — fz.ax's week index.

    DOB itself is week 0. Each subsequent 7-day block is the next week.
    Uses local-date math; verify against fz.ax's exact DOB math before production
    (see plan's open questions).
    """
    delta_days = (d - dob).days
    return delta_days // 7


def parse_dob(dob_str: str) -> date:
    return datetime.strptime(dob_str, "%Y-%m-%d").date()
