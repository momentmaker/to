from scripts.normalize_sparks import normalize_sparks_text


BROKEN = """# sparks

2026-04-22 — crazy last of privacy for employees - literally like neo-serfs

2026-04-23 — we are all just arbitrager of tokens now
2026-04-24 — i like things to be automated as much as i can 🙂
2026-04-25 — the contrast between height of intelligence and just simple piece of art
"""


def test_normalize_inserts_blank_lines_between_jammed_entries():
    out = normalize_sparks_text(BROKEN)
    expected = """# sparks

2026-04-22 — crazy last of privacy for employees - literally like neo-serfs

2026-04-23 — we are all just arbitrager of tokens now

2026-04-24 — i like things to be automated as much as i can 🙂

2026-04-25 — the contrast between height of intelligence and just simple piece of art
"""
    assert out == expected


def test_normalize_is_idempotent():
    once = normalize_sparks_text(BROKEN)
    twice = normalize_sparks_text(once)
    assert once == twice


def test_normalize_preserves_header_and_trailing_newline():
    out = normalize_sparks_text(BROKEN)
    assert out.startswith("# sparks\n")
    assert out.endswith("\n")


def test_normalize_handles_empty_file():
    assert normalize_sparks_text("") == ""


def test_normalize_handles_header_only():
    assert normalize_sparks_text("# sparks\n") == "# sparks\n"
