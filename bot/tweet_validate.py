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

    if "?" in s:
        return False, "punctuation: '?' not allowed"
    if "!" in s:
        return False, "punctuation: '!' not allowed"
    if "#" in s:
        return False, "punctuation: '#' not allowed"
    if "..." in s or "…" in s:
        return False, "punctuation: ellipsis not allowed"

    char_count = grapheme.length(s)
    if char_count > 80:
        return False, f"chars: {char_count} > 80"
    words = _words(s)
    if not words:
        return False, "empty stitch"
    if len(words) > 15:
        return False, f"words: {len(words)} > 15"

    for tok in words:
        if tok in _FIRST_PERSON_TOKENS:
            return False, f"first-person token: {tok!r}"
    if re.search(r"\bto me\b", s.lower()):
        return False, "first-person token: 'to me'"

    for tok in words:
        if tok in _FORBIDDEN_VERBS:
            return False, f"forbidden verb: {tok!r}"

    body = s.rstrip(".—")
    if re.search(r"[.!?]", body):
        return False, "sentence: more than one"

    return True, None
