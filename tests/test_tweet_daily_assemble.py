import grapheme

from bot import tweet_daily


def _cap(*, id, raw, kind="text", local_date="2026-05-01", url=None):
    return {
        "id": id, "kind": kind, "raw": raw, "url": url,
        "local_date": local_date,
    }


# ---- shape: insight (default) -------------------------------------------

def test_insight_shape_renders_stitch_only():
    out = tweet_daily.assemble_tweet(
        shape="insight",
        stitch="you caught the asymmetry between what is kept and what keeps.",
        captures=[
            _cap(id=1, raw="a"), _cap(id=2, raw="b"),
        ],
    )
    assert out is not None
    assert out.startswith(
        "you caught the asymmetry between what is kept and what keeps."
    )
    # No dashes, no dates, no quoted bodies in the rendered tweet.
    assert "—" not in out
    assert "(2026-" not in out


def test_insight_shape_appends_url_when_url_capture_present():
    out = tweet_daily.assemble_tweet(
        shape="insight",
        stitch="you keep the link.",
        captures=[
            _cap(id=1, raw="article body", kind="url",
                 local_date="2026-04-22",
                 url="https://example.com/article"),
            _cap(id=2, raw="other thought"),
        ],
    )
    assert out is not None
    assert out.endswith("https://example.com/article")


def test_insight_shape_no_url_when_no_url_capture():
    out = tweet_daily.assemble_tweet(
        shape="insight",
        stitch="you saw it.",
        captures=[
            _cap(id=1, raw="text a"), _cap(id=2, raw="text b"),
        ],
    )
    assert out is not None
    assert "https://" not in out


# ---- shape: quote_led ---------------------------------------------------

def test_quote_led_shape_leads_with_quoted_line():
    out = tweet_daily.assemble_tweet(
        shape="quote_led",
        stitch="the smallest blade is the one that finishes the work.",
        lead_quote="using samurai swords to cut the thoughts",
        captures=[
            _cap(id=1, raw="i learned a few new things too like using samurai swords to cut the thoughts/images with 2 slashes"),
            _cap(id=2, raw="i like things to be automated as much as i can"),
        ],
    )
    assert out is not None
    assert out.startswith('"using samurai swords to cut the thoughts"')
    assert "the smallest blade is the one that finishes the work." in out


def test_quote_led_falls_back_to_insight_when_lead_quote_missing():
    out = tweet_daily.assemble_tweet(
        shape="quote_led",
        stitch="you saw both.",
        lead_quote=None,
        captures=[
            _cap(id=1, raw="a"), _cap(id=2, raw="b"),
        ],
    )
    assert out is not None
    assert out.startswith("you saw both.")
    assert '"' not in out


# ---- shape: temporal ----------------------------------------------------

def test_temporal_renders_like_insight():
    """temporal is content-shape on the stitch text; rendering is the
    same as insight."""
    out = tweet_daily.assemble_tweet(
        shape="temporal",
        stitch="you noticed this once. then again, weeks later.",
        captures=[
            _cap(id=1, raw="recent", local_date="2026-05-01"),
            _cap(id=2, raw="older", local_date="2026-03-15"),
        ],
    )
    assert out is not None
    assert out.startswith("you noticed this once. then again, weeks later.")


# ---- failure modes ------------------------------------------------------

def test_returns_none_when_fewer_than_two_captures():
    out = tweet_daily.assemble_tweet(
        shape="insight",
        stitch="hello.",
        captures=[_cap(id=1, raw="alone")],
    )
    assert out is None


def test_returns_none_when_stitch_empty():
    out = tweet_daily.assemble_tweet(
        shape="insight",
        stitch="",
        captures=[_cap(id=1, raw="a"), _cap(id=2, raw="b")],
    )
    assert out is None


def test_returns_none_when_total_length_overflows():
    out = tweet_daily.assemble_tweet(
        shape="insight",
        stitch="x" * 300,  # over 280 by itself
        captures=[_cap(id=1, raw="a"), _cap(id=2, raw="b")],
    )
    assert out is None


# ---- URL selection ------------------------------------------------------

def test_picks_oldest_url_when_two_url_captures():
    out = tweet_daily.assemble_tweet(
        shape="insight",
        stitch="you keep the link.",
        captures=[
            _cap(id=1, raw="newer", kind="url",
                 local_date="2026-04-22",
                 url="https://example.com/new"),
            _cap(id=2, raw="older", kind="url",
                 local_date="2026-04-20",
                 url="https://example.com/old"),
        ],
    )
    assert out.endswith("https://example.com/old")


def test_total_length_within_280_graphemes():
    out = tweet_daily.assemble_tweet(
        shape="insight",
        stitch="you saw both.",
        captures=[
            _cap(id=1, raw="article body", kind="url",
                 local_date="2026-04-22",
                 url="https://example.com/" + "a" * 100),
            _cap(id=2, raw="other"),
        ],
    )
    assert out is not None
    import re as _re
    measured = _re.sub(r"https?://\S+", "x" * 23, out)
    assert grapheme.length(measured) <= 280
