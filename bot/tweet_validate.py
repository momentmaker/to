"""Validators for the daily tweet pipeline.

Two functions:
- validate_stitch: bounds the orchurator stitch sentence to its
  declared shape (length, person, vocabulary, punctuation).
- validate_tweet_total_length: enforces X's 280-grapheme hard limit
  on the assembled tweet, with t.co URL accounting.
"""

from __future__ import annotations

import re

import grapheme


_FORBIDDEN_VERBS = {
    "should", "must", "ought", "will", "predict", "recommend",
    "advise", "urge", "encourage", "warn",
}
_FIRST_PERSON_TOKENS = {
    "i", "me", "my", "mine",
    "i'm", "i'd", "i'll", "i've",
}
_WORD_RE = re.compile(r"[\w']+", flags=re.UNICODE)


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def validate_stitch(text: str) -> tuple[bool, str | None]:
    """Return (ok, reason). reason is None when ok is True."""
    s = (text or "").strip()
    if not s:
        return False, "empty stitch"

    # Internal line breaks would split the rendered tweet's first line and
    # eat into the char budget unpredictably. Reject any CR/LF or unicode
    # line/paragraph separators in the stitch body.
    for ch in ("\n", "\r", "\u2028", "\u2029"):
        if ch in s:
            return False, "line break in stitch"

    if "?" in s:
        return False, "punctuation: '?' not allowed"
    if "!" in s:
        return False, "punctuation: '!' not allowed"
    if "#" in s:
        return False, "punctuation: '#' not allowed"
    if "..." in s or "…" in s:
        return False, "punctuation: ellipsis not allowed"

    char_count = grapheme.length(s)
    if char_count > 180:
        return False, f"chars: {char_count} > 180"
    words = _words(s)
    if not words:
        return False, "empty stitch"
    if len(words) > 30:
        return False, f"words: {len(words)} > 30"

    for tok in words:
        if tok in _FIRST_PERSON_TOKENS:
            return False, f"first-person token: {tok!r}"
    if re.search(r"\bto me\b", s.lower()):
        return False, "first-person token: 'to me'"

    for tok in words:
        if tok in _FORBIDDEN_VERBS:
            return False, f"forbidden verb: {tok!r}"

    # Allow 1-2 sentences. Body has the trailing terminator stripped, so
    # one internal period = two-sentence stitch (the max).
    body = s.rstrip(".—")
    if body.count(".") > 1:
        return False, "sentence: more than two"

    return True, None


_TWEET_MAX = 280
_TCO_LEN = 23
_URL_RE = re.compile(r"https?://\S+", flags=re.IGNORECASE)


def validate_tweet_total_length(text: str) -> tuple[bool, str | None]:
    """Enforce X's 280-grapheme hard limit. Each https?:// URL counts as
    23 chars (t.co length) regardless of original length."""
    s = text or ""
    placeholder = "x" * _TCO_LEN
    measured = _URL_RE.sub(placeholder, s)
    n = grapheme.length(measured)
    if n > _TWEET_MAX:
        return False, f"length: {n} > {_TWEET_MAX}"
    return True, None
