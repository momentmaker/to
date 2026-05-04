from bot.tweet_validate import validate_stitch, validate_tweet_total_length


def _ok(text):
    ok, reason = validate_stitch(text)
    assert ok, f"expected pass, got {reason!r}"


def _bad(text, expect_in_reason):
    ok, reason = validate_stitch(text)
    assert not ok, "expected fail"
    assert expect_in_reason in reason, (
        f"reason {reason!r} missing {expect_in_reason!r}"
    )


def test_valid_stitch_passes():
    _ok("you caught the same asymmetry twice.")


def test_empty_fails():
    _bad("", "empty")


def test_whitespace_only_fails():
    _bad("    ", "empty")


def test_too_long_word_count_fails():
    # 16 short words, 47 chars — within char cap, over word cap.
    _bad("a b c d e f g h i j k l m n o p.", "words")


def test_too_long_chars_fails():
    _bad("you noticed " + "x" * 200, "chars")


def test_first_person_singular_fails():
    _bad("i think you caught it.", "first-person")
    _bad("to me this rhymes.", "first-person")
    _bad("my read is you noticed.", "first-person")


def test_forbidden_verb_fails():
    _bad("you should keep going.", "forbidden")
    _bad("you must notice this.", "forbidden")
    _bad("you will see this again.", "forbidden")


def test_question_mark_fails():
    _bad("did you notice this?", "punctuation")


def test_exclamation_fails():
    _bad("you caught it again!", "punctuation")


def test_hashtag_fails():
    _bad("you caught it #privacy.", "punctuation")


def test_ellipsis_fails():
    _bad("you noticed... again.", "punctuation")
    _bad("you noticed … again.", "punctuation")


def test_two_sentences_fails():
    _bad("you caught it. you kept it.", "sentence")


def test_period_terminator_optional():
    _ok("you caught the asymmetry —")
    _ok("you caught the asymmetry")  # no terminator allowed


def test_short_tweet_passes():
    ok, reason = validate_tweet_total_length("hello world")
    assert ok and reason is None


def test_at_280_passes():
    text = "x" * 280
    ok, reason = validate_tweet_total_length(text)
    assert ok


def test_281_fails():
    text = "x" * 281
    ok, reason = validate_tweet_total_length(text)
    assert not ok
    assert "281" in reason


def test_url_counted_as_23():
    # 256 body + space + URL-as-23 = 280 → at the cap, passes.
    body = "x" * 256
    text = (
        body + " "
        + "https://example.com/this-is-much-longer-than-23-chars/foo/bar/baz"
    )
    ok, reason = validate_tweet_total_length(text)
    assert ok, f"got {reason!r}"


def test_url_over_when_combined_with_long_body_fails():
    # 257 body + space + URL-as-23 = 281 → over.
    body = "x" * 257
    text = body + " https://example.com/short"
    ok, reason = validate_tweet_total_length(text)
    assert not ok


def test_emoji_counts_as_one_grapheme():
    text = "🙂" * 280
    ok, reason = validate_tweet_total_length(text)
    assert ok
    text281 = "🙂" * 281
    ok, reason = validate_tweet_total_length(text281)
    assert not ok


def test_newline_in_stitch_fails():
    _bad("you saw it\nand kept it.", "line break")


def test_carriage_return_in_stitch_fails():
    _bad("you saw it\r and kept it.", "line break")


def test_unicode_line_separator_in_stitch_fails():
    _bad("you saw it  and kept it.", "line break")
