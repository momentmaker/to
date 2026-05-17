from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from bot import db as db_mod
from bot.config import Settings
from bot.digest import fz_state


def _settings(**kw):
    base = dict(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=42,
        DOB="1990-01-15", TIMEZONE="UTC",
        ANTHROPIC_API_KEY="k", WEEK_START="mon",
    )
    base.update(kw)
    return Settings(**base)


async def _insert_weekly(conn, *, fz_week_idx: int, iso_week_key: str,
                          mark: str = "☲", whisper: str = "a line",
                          marked_at: str | None = None,
                          status: str = "processed"):
    marked_at = marked_at or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    await conn.execute(
        """INSERT INTO weekly (fz_week_idx, iso_week_key, mark, whisper, marked_at, status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (fz_week_idx, iso_week_key, mark, whisper, marked_at, status),
    )
    await conn.commit()


@pytest.mark.asyncio
async def test_fz_state_has_required_top_level_shape(conn):
    await _insert_weekly(conn, fz_week_idx=1, iso_week_key="1990-W03", mark="☲")
    state = await fz_state.build_fz_state(conn=conn, settings=_settings())
    assert state["fzAxBackup"] is True
    assert isinstance(state["exportedAt"], str) and state["exportedAt"].endswith("Z")
    inner = state["state"]
    assert inner["version"] == 1
    assert inner["dob"] == "1990-01-15"
    assert isinstance(inner["weeks"], dict)
    assert inner["letters"] == []
    assert isinstance(inner["anchors"], list)
    assert set(inner["prefs"].keys()) >= {"theme", "pushOptIn", "reducedMotion", "weekStart"}
    assert "createdAt" in inner["meta"]


@pytest.mark.asyncio
async def test_fz_state_weeks_map_uses_string_keys_and_week_fields(conn):
    await _insert_weekly(
        conn, fz_week_idx=16, iso_week_key="2026-W16",
        mark="☲", whisper="a week of small ignitions",
        marked_at="2026-04-21T22:00:00Z",
    )
    state = await fz_state.build_fz_state(conn=conn, settings=_settings())
    weeks = state["state"]["weeks"]
    assert "16" in weeks  # string key for JSON compatibility
    assert weeks["16"]["mark"] == "☲"
    assert weeks["16"]["whisper"] == "a week of small ignitions"
    assert weeks["16"]["markedAt"] == "2026-04-21T22:00:00Z"


@pytest.mark.asyncio
async def test_fz_state_is_cumulative_across_weeks(conn):
    await _insert_weekly(conn, fz_week_idx=1, iso_week_key="1990-W03", mark="a")
    await _insert_weekly(conn, fz_week_idx=5, iso_week_key="1990-W07", mark="b")
    await _insert_weekly(conn, fz_week_idx=12, iso_week_key="1990-W14", mark="c")

    state = await fz_state.build_fz_state(conn=conn, settings=_settings())
    assert set(state["state"]["weeks"].keys()) == {"1", "5", "12"}
    assert state["state"]["weeks"]["1"]["mark"] == "a"
    assert state["state"]["weeks"]["5"]["mark"] == "b"
    assert state["state"]["weeks"]["12"]["mark"] == "c"


@pytest.mark.asyncio
async def test_anchors_are_sorted_week_indices(conn):
    # Insert out of order
    await _insert_weekly(conn, fz_week_idx=12, iso_week_key="1990-W14", mark="c")
    await _insert_weekly(conn, fz_week_idx=1, iso_week_key="1990-W03", mark="a")
    await _insert_weekly(conn, fz_week_idx=5, iso_week_key="1990-W07", mark="b")

    state = await fz_state.build_fz_state(conn=conn, settings=_settings())
    assert state["state"]["anchors"] == [1, 5, 12]


@pytest.mark.asyncio
async def test_fz_state_skips_unprocessed_and_markless_weeks(conn):
    await _insert_weekly(conn, fz_week_idx=1, iso_week_key="1990-W03",
                         mark="☲", status="processed")
    await _insert_weekly(conn, fz_week_idx=2, iso_week_key="1990-W04",
                         mark="", status="processed")       # no mark
    await _insert_weekly(conn, fz_week_idx=3, iso_week_key="1990-W05",
                         mark="🌱", status="failed")         # not processed
    state = await fz_state.build_fz_state(conn=conn, settings=_settings())
    assert list(state["state"]["weeks"].keys()) == ["1"]
    assert state["state"]["anchors"] == [1]


@pytest.mark.asyncio
async def test_fz_state_vow_from_kv_or_null(conn):
    # No vow set → null
    state = await fz_state.build_fz_state(conn=conn, settings=_settings())
    assert state["state"]["vow"] is None

    await fz_state.set_vow(conn, "i will pay attention")
    state = await fz_state.build_fz_state(conn=conn, settings=_settings())
    vow = state["state"]["vow"]
    assert vow is not None
    assert vow["text"] == "i will pay attention"
    assert isinstance(vow["writtenAt"], str)


@pytest.mark.asyncio
async def test_fz_state_requires_dob(conn):
    with pytest.raises(RuntimeError, match="DOB"):
        await fz_state.build_fz_state(conn=conn, settings=_settings(DOB=""))


@pytest.mark.asyncio
async def test_fz_state_prefs_defaults_and_week_start(conn):
    state = await fz_state.build_fz_state(conn=conn, settings=_settings(WEEK_START="sun"))
    assert state["state"]["prefs"]["weekStart"] == "sun"
    # Defaults for the others
    assert state["state"]["prefs"]["theme"] == "auto"
    assert state["state"]["prefs"]["pushOptIn"] is False
    assert state["state"]["prefs"]["reducedMotion"] == "auto"


@pytest.mark.asyncio
async def test_fz_state_matches_expected_fixture_shape(conn):
    """Deep-equal against a pinned expected fixture. If fz.ax adds/renames a
    field, this test catches the drift."""
    await _insert_weekly(
        conn, fz_week_idx=1, iso_week_key="1990-W03",
        mark="☲", whisper="a line",
        marked_at="2026-04-21T22:00:00Z",
    )
    await fz_state.set_vow(conn, "i will pay attention")

    state = await fz_state.build_fz_state(conn=conn, settings=_settings())
    # Strip volatile fields (exportedAt, createdAt, writtenAt)
    state_copy = json.loads(json.dumps(state))
    state_copy["exportedAt"] = "<volatile>"
    state_copy["state"]["meta"]["createdAt"] = "<volatile>"
    state_copy["state"]["vow"]["writtenAt"] = "<volatile>"

    expected = {
        "fzAxBackup": True,
        "exportedAt": "<volatile>",
        "state": {
            "version": 1,
            "dob": "1990-01-15",
            "weeks": {
                "1": {
                    "mark": "☲",
                    "whisper": "a line",
                    "markedAt": "2026-04-21T22:00:00Z",
                },
            },
            "vow": {
                "text": "i will pay attention",
                "writtenAt": "<volatile>",
            },
            "letters": [],
            "anchors": [1],
            "prefs": {
                "theme": "auto",
                "pushOptIn": False,
                "reducedMotion": "auto",
                "weekStart": "mon",
            },
            "meta": {
                "createdAt": "<volatile>",
            },
        },
    }
    assert state_copy == expected


@pytest.mark.asyncio
async def test_fz_state_drops_invalid_prefs_values(conn):
    """Regression: a typo or unknown value in kv.prefs (say theme='neon')
    must NOT leak into the exported FzState — fz.ax's import validator would
    reject the whole backup otherwise.
    """
    import json as _j
    bad_prefs = {
        "theme": "neon",              # invalid value
        "pushOptIn": "true",          # wrong type (string not bool)
        "reducedMotion": "auto",      # OK
        "weekStart": "wed",           # invalid value
        "extraKey": "surprise",       # unknown key
    }
    await conn.execute(
        "INSERT INTO kv (key, value, updated_at) VALUES ('prefs', ?, ?)",
        (_j.dumps(bad_prefs), "2026-04-21T00:00:00Z"),
    )
    await conn.commit()

    state = await fz_state.build_fz_state(conn=conn, settings=_settings())
    prefs = state["state"]["prefs"]
    # Bad values replaced with defaults; unknown key dropped.
    assert prefs["theme"] == "auto"
    assert prefs["pushOptIn"] is False
    assert prefs["reducedMotion"] == "auto"
    assert prefs["weekStart"] == "mon"  # from settings fallback
    assert "extraKey" not in prefs


@pytest.mark.asyncio
async def test_fz_state_accepts_valid_stored_prefs(conn):
    import json as _j
    good_prefs = {
        "theme": "dark",
        "pushOptIn": True,
        "reducedMotion": False,
        "weekStart": "sun",
    }
    await conn.execute(
        "INSERT INTO kv (key, value, updated_at) VALUES ('prefs', ?, ?)",
        (_j.dumps(good_prefs), "2026-04-21T00:00:00Z"),
    )
    await conn.commit()

    state = await fz_state.build_fz_state(conn=conn, settings=_settings())
    prefs = state["state"]["prefs"]
    assert prefs == good_prefs


def test_serialize_is_deterministic_and_utf8():
    state = {"fzAxBackup": True, "state": {"weeks": {"1": {"mark": "☲"}}}}
    s1 = fz_state.serialize(state)
    s2 = fz_state.serialize(state)
    assert s1 == s2
    assert s1.endswith("\n")
    assert "☲" in s1  # non-ASCII preserved, not escaped


def _backup(weeks: dict, anchors: list[int]) -> dict:
    return {
        "fzAxBackup": True,
        "exportedAt": "2026-05-01T00:00:00Z",
        "state": {
            "version": 1, "dob": "1990-01-15", "weeks": weeks,
            "vow": None, "letters": [], "anchors": anchors,
            "prefs": {}, "meta": {"createdAt": "2026-01-01T00:00:00Z"},
        },
    }


def test_merge_remote_weeks_preserves_weeks_only_on_remote():
    """Regression: bot rebuilds backup from its DB only. Weeks anchored by
    the user's local /weekly (never in the bot DB) must survive the push."""
    db_state = _backup(
        {"2103": {"mark": "🎨", "whisper": "w19"}}, [2103],
    )
    remote = fz_state.serialize(_backup(
        {
            "2101": {"mark": "🌊", "whisper": "w17"},
            "2102": {"mark": "歩", "whisper": "w18"},
        },
        [2101, 2102],
    ))

    merged = fz_state.merge_remote_weeks(db_state, remote)
    weeks = merged["state"]["weeks"]
    assert set(weeks) == {"2101", "2102", "2103"}
    assert weeks["2101"]["mark"] == "🌊"
    assert merged["state"]["anchors"] == [2101, 2102, 2103]


def test_merge_remote_weeks_db_wins_on_conflict():
    db_state = _backup({"2103": {"mark": "NEW", "whisper": "db"}}, [2103])
    remote = fz_state.serialize(_backup(
        {"2103": {"mark": "OLD", "whisper": "remote"}}, [2103],
    ))
    merged = fz_state.merge_remote_weeks(db_state, remote)
    assert merged["state"]["weeks"]["2103"]["mark"] == "NEW"


def test_merge_remote_weeks_tolerates_missing_or_garbage_remote():
    db_state = _backup({"2103": {"mark": "🎨"}}, [2103])
    for bad in (None, "", "not json", "{}", '{"state": {}}'):
        merged = fz_state.merge_remote_weeks(db_state, bad)
        assert merged["state"]["weeks"] == {"2103": {"mark": "🎨"}}
        assert merged["state"]["anchors"] == [2103]
