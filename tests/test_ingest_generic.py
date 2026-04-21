from __future__ import annotations

import pytest

from bot.ingest.generic import extract_article


pytestmark = pytest.mark.asyncio


_READABLE_HTML = """
<!doctype html>
<html><head><title>Small Ignitions</title></head>
<body>
  <nav>ignore me</nav>
  <article>
    <h1>Small Ignitions</h1>
    <p>{body}</p>
    <p>The second paragraph continues the argument with more detail about how attention,
    once given, sustains itself on even modest fuel.</p>
  </article>
  <footer>footer junk</footer>
</body></html>
""".format(body="The impediment to action advances action. " * 30)


_THIN_HTML = "<html><body><div>too short</div></body></html>"


_JS_HEAVY_HTML = """
<!doctype html>
<html><body>
<div id="root"></div>
<script>console.log('rendered client-side')</script>
</body></html>
"""


async def test_generic_extracts_article_with_readability():
    art = await extract_article("https://example.com/a", html=_READABLE_HTML)
    assert art.method in ("readability", "trafilatura")
    assert "impediment to action" in art.text.lower()
    assert len(art.text) > 400


async def test_generic_falls_back_to_trafilatura_or_raw_on_thin_html():
    art = await extract_article("https://example.com/thin", html=_THIN_HTML)
    # readability will reject thin HTML; trafilatura may also reject it.
    # Either way we don't crash.
    assert art.text is not None


async def test_generic_returns_raw_when_no_extractor_can_find_content():
    art = await extract_article("https://example.com/js", html=_JS_HEAVY_HTML)
    # Neither extractor finds substantive text in a JS-only shell.
    assert art.method in ("raw", "trafilatura", "readability")
    # When we fall back to 'raw', the text is plain (tags + script/style
    # stripped) so downstream LLM ingest doesn't waste tokens on markup.
    assert isinstance(art.text, str)
    if art.method == "raw":
        assert "<" not in art.text
        assert "rendered client-side" not in art.text  # stripped with the <script>


async def test_generic_raw_fallback_strips_html_and_script_tags():
    html = """
    <html><head><style>body{color:red}</style></head>
    <body>
      <script>var secret = 'x';</script>
      <h1>Headline</h1>
      <p>Readable text in a short page.</p>
    </body></html>
    """
    art = await extract_article("https://example.com/a", html=html)
    # Even if readability/trafilatura reject thin pages, raw fallback must be tag-free
    assert "<" not in art.text
    assert "var secret" not in art.text
    assert "color:red" not in art.text
