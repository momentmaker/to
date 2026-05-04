import grapheme

from bot import tweet_daily


def _cap(*, id, raw, kind="text", local_date="2026-05-01", url=None):
    return {
        "id": id, "kind": kind, "raw": raw, "url": url,
        "local_date": local_date,
    }


def test_assemble_no_url_basic():
    out = tweet_daily.assemble_tweet(
        stitch="you caught the asymmetry.",
        captures=[
            _cap(id=1, raw="crazy last of privacy",
                 local_date="2026-04-22"),
            _cap(id=2, raw="someone kept this data",
                 local_date="2026-04-21"),
        ],
    )
    assert out is not None
    assert "you caught the asymmetry." in out
    assert '"crazy last of privacy" (2026-04-22)' in out
    assert '"someone kept this data" (2026-04-21)' in out
    assert "https://" not in out


def test_assemble_with_url():
    out = tweet_daily.assemble_tweet(
        stitch="you keep the link.",
        captures=[
            _cap(id=1, raw="article body", kind="url",
                 local_date="2026-04-22",
                 url="https://example.com/article"),
            _cap(id=2, raw="other thought",
                 local_date="2026-04-21"),
        ],
    )
    assert out is not None
    assert out.endswith("https://example.com/article")


def test_assemble_picks_oldest_url_when_two_url_captures():
    out = tweet_daily.assemble_tweet(
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


def test_assemble_keeps_naturally_short_body_verbatim():
    long_body = "x " * 200  # 399 chars
    out = tweet_daily.assemble_tweet(
        stitch="you saw both.",
        captures=[
            _cap(id=1, raw=long_body, local_date="2026-04-22"),
            _cap(id=2, raw="short", local_date="2026-04-21"),
        ],
    )
    # Long body truncated, short ("short", 5 chars) preserved verbatim
    # because allocator skips truncation on bodies that fit naturally.
    assert out is not None
    assert grapheme.length(out) <= 280
    assert '"short" (2026-04-21)' in out


def test_assemble_truncates_long_quote_keeps_short_quote_intact():
    long_body = ("xx " * 200).strip()  # 599 chars
    short_body = "thirty character verbatim line."  # 31 chars
    out = tweet_daily.assemble_tweet(
        stitch="you saw both.",
        captures=[
            _cap(id=1, raw=long_body, local_date="2026-04-22"),
            _cap(id=2, raw=short_body, local_date="2026-04-21"),
        ],
    )
    assert out is not None
    assert grapheme.length(out) <= 280
    assert short_body in out


def test_assemble_returns_none_when_fewer_than_two_captures():
    out = tweet_daily.assemble_tweet(
        stitch="hello.",
        captures=[_cap(id=1, raw="alone", local_date="2026-04-22")],
    )
    assert out is None


def test_assemble_returns_none_when_stitch_empty():
    out = tweet_daily.assemble_tweet(
        stitch="",
        captures=[
            _cap(id=1, raw="crazy last of privacy",
                 local_date="2026-04-22"),
            _cap(id=2, raw="someone kept this data",
                 local_date="2026-04-21"),
        ],
    )
    assert out is None
