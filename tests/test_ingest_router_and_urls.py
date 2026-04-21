from bot.ingest.router import classify_text
from bot.ingest.urls import classify_url, extract_url


def test_classify_url_recognizes_hn():
    assert classify_url("https://news.ycombinator.com/item?id=123") == "hn"
    assert classify_url("https://hn.algolia.com/api/v1/items/123") == "hn"


def test_classify_url_recognizes_reddit():
    assert classify_url("https://reddit.com/r/foo/comments/1/bar") == "reddit"
    assert classify_url("https://www.reddit.com/r/foo") == "reddit"
    assert classify_url("https://old.reddit.com/r/foo") == "reddit"


def test_classify_url_recognizes_x_and_twitter():
    assert classify_url("https://x.com/handle/status/1") == "x"
    assert classify_url("https://twitter.com/handle/status/1") == "x"
    assert classify_url("https://mobile.twitter.com/handle") == "x"


def test_classify_url_defaults_to_generic():
    assert classify_url("https://example.com/article") == "generic"
    assert classify_url("https://blog.acme.io/post") == "generic"


def test_extract_url_finds_first_http_url():
    assert extract_url("check this out https://example.com/a cool!") == "https://example.com/a"
    assert extract_url("no url here") is None
    assert extract_url("") is None


def test_extract_url_strips_trailing_punctuation():
    assert extract_url("see https://example.com/path.") == "https://example.com/path"
    assert extract_url("see https://example.com/path,") == "https://example.com/path"
    assert extract_url("see https://example.com/path)") == "https://example.com/path"


def test_classify_text_text_vs_url():
    assert classify_text("just a plain line i heard") == ("text", None)
    k, u = classify_text("https://example.com/a is worth a read")
    assert k == "url" and u == "https://example.com/a"
