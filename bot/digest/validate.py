"""Validators for the weekly-digest LLM output.

- Mark must be a single Unicode grapheme (fz.ax's import rejects otherwise).
- Whisper must be <= 240 chars.
- Essay must be composed of sentences that are (near-verbatim) substrings of
  the provided corpus — no invented prose.
"""

from __future__ import annotations

import re

import grapheme


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WS_RE = re.compile(r"\s+")


def is_single_grapheme(s: str) -> bool:
    return grapheme.length((s or "").strip()) == 1


def whisper_ok(s: str) -> bool:
    # Count graphemes for an accurate visual length, not code units.
    return 0 < grapheme.length((s or "").strip()) <= 240


def normalize_for_quote_check(text: str) -> str:
    """Lower-case, punctuation-stripped, single-space-collapsed."""
    s = (text or "").lower()
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def split_sentences(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    # Split on sentence enders, flatten across newlines. Paragraphs are
    # joined so multi-line quotes still count as single sentences when the
    # source fragment happens to wrap.
    flat = _WS_RE.sub(" ", text).strip()
    pieces = _SENTENCE_SPLIT.split(flat)
    return [p.strip() for p in pieces if p.strip()]


def validate_quote_only(
    essay: str, corpus_texts: list[str]
) -> tuple[bool, list[str]]:
    """Return (ok, offending_sentences).

    `ok` is True iff the essay has at least one substantive (post-normalization)
    sentence AND every such sentence is a substring of the normalized corpus
    (all corpus_texts concatenated).
    """
    sentences = split_sentences(essay)
    if not sentences:
        return False, ["[empty essay]"]

    # Normalize up-front. A sentence that normalizes to "" is pure
    # punctuation/whitespace — if EVERY sentence does, the essay is
    # effectively empty and must fail validation.
    real_sentences = [
        (s, normalize_for_quote_check(s)) for s in sentences
    ]
    real_sentences = [(s, ns) for s, ns in real_sentences if ns]
    if not real_sentences:
        return False, ["[empty essay]"]

    combined = " ".join(t for t in corpus_texts if isinstance(t, str))
    norm_corpus = normalize_for_quote_check(combined)
    offenders: list[str] = []
    for sentence, norm_s in real_sentences:
        if norm_s not in norm_corpus:
            offenders.append(sentence)
    return (not offenders), offenders
