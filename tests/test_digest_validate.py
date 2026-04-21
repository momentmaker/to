from bot.digest.validate import (
    is_single_grapheme,
    normalize_for_quote_check,
    split_sentences,
    validate_quote_only,
    whisper_ok,
)


def test_is_single_grapheme_accepts_one_char_or_emoji():
    assert is_single_grapheme("a")
    assert is_single_grapheme("☲")
    assert is_single_grapheme("🌱")
    # Multi-codepoint emoji that renders as a single grapheme
    assert is_single_grapheme("👨‍👩‍👧")  # family ZWJ sequence


def test_is_single_grapheme_rejects_multi():
    assert not is_single_grapheme("ab")
    assert not is_single_grapheme("a b")
    assert not is_single_grapheme("☲🌱")
    assert not is_single_grapheme("")


def test_whisper_ok_boundary():
    assert whisper_ok("x")
    assert whisper_ok("a" * 240)
    assert not whisper_ok("")
    assert not whisper_ok("a" * 241)


def test_normalize_strips_punctuation_and_case():
    assert normalize_for_quote_check("The Impediment, to Action!") == "the impediment to action"
    assert normalize_for_quote_check("  many   spaces ") == "many spaces"


def test_split_sentences_handles_various_punctuation():
    assert split_sentences("First. Second? Third!") == ["First.", "Second?", "Third!"]
    assert split_sentences("") == []
    assert split_sentences("no punctuation yet") == ["no punctuation yet"]


def test_validate_quote_only_passes_verbatim_essay():
    corpus = ["the impediment to action advances action", "what stands in the way becomes the way"]
    essay = "The impediment to action advances action. What stands in the way becomes the way."
    ok, offenders = validate_quote_only(essay, corpus)
    assert ok, offenders
    assert offenders == []


def test_validate_quote_only_catches_hallucinated_sentence():
    corpus = ["the impediment to action advances action"]
    essay = "The impediment to action advances action. I like pizza."
    ok, offenders = validate_quote_only(essay, corpus)
    assert not ok
    assert len(offenders) == 1
    assert "pizza" in offenders[0].lower()


def test_validate_quote_only_tolerates_case_and_punctuation_drift():
    corpus = ["the way light held the room in amber"]
    essay = "The way, light held the room in amber."  # extra comma
    ok, _ = validate_quote_only(essay, corpus)
    assert ok


def test_validate_quote_only_accepts_truncation_of_source():
    """A sentence can be a substring of a longer source (drops leading/trailing
    words). This matches what QUOTE_ONLY_RULES allows.
    """
    corpus = [
        "it was a short conversation about the way afternoons settle into evening"
    ]
    essay = "The way afternoons settle into evening."
    ok, _ = validate_quote_only(essay, corpus)
    assert ok


def test_validate_quote_only_rejects_mashup_across_fragments():
    """Stitching words from different fragments into one sentence must NOT
    pass — even if every word individually exists in the corpus."""
    corpus = ["a small ignition in the morning", "the afternoon settled"]
    # Words from both, none of which appears as a single substring
    essay = "a small ignition settled."
    ok, offenders = validate_quote_only(essay, corpus)
    assert not ok
    assert offenders


def test_validate_quote_only_rejects_empty_essay():
    """Regression: previously `validate_quote_only("", [...])` returned
    (True, []) because `split_sentences("")` is empty so the loop never ran.
    An empty digest is never valid — we don't want to commit one to the repo.
    """
    ok, offenders = validate_quote_only("", ["some corpus"])
    assert not ok
    assert offenders

    ok, offenders = validate_quote_only("   \n  ", ["some corpus"])
    assert not ok
    assert offenders


def test_validate_quote_only_rejects_punctuation_only_essay():
    """Regression: an essay like '...' has one 'sentence' that normalizes to
    empty. Previously the loop skipped it, offenders stayed empty, and the
    essay was accepted as valid — a pure-punctuation digest would have been
    committed to the repo.
    """
    ok, offenders = validate_quote_only("...", ["some corpus"])
    assert not ok

    ok, offenders = validate_quote_only("??? !!! ...", ["some corpus"])
    assert not ok

    ok, offenders = validate_quote_only("---", ["some corpus"])
    assert not ok
