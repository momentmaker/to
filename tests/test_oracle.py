from __future__ import annotations

import json
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot import db as db_mod
from bot import oracle
from bot.config import Settings
from bot.llm.base import LlmResponse
from bot.llm.router import Providers


def _settings(**kw):
    base = dict(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_OWNER_ID=42, DOB="1990-01-01",
        TIMEZONE="UTC", ANTHROPIC_API_KEY="k", OPENAI_API_KEY="",
    )
    base.update(kw)
    return Settings(**base)


class _SeqProv:
    """Returns LLM responses from a queue. Records every call."""
    name = "anthropic"
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        text = self._responses.pop(0) if self._responses else ""
        return LlmResponse(
            text=text, model=kwargs["model"], provider=self.name,
            input_tokens=20, output_tokens=10,
        )


# ---- modifier parsing ----------------------------------------------------

def test_parse_ask_args_extracts_since_and_limit():
    req = oracle.parse_ask_args("since:2026-04-01 limit:3 what have I been circling?")
    assert req.since == "2026-04-01"
    assert req.limit == 3
    assert req.question == "what have I been circling?"


def test_parse_ask_args_malformed_modifiers_kept_in_question():
    req = oracle.parse_ask_args("since:not-a-date limit:9999 tell me")
    assert req.since is None
    assert req.limit == 8
    assert "since:not-a-date" in req.question
    assert "limit:9999" in req.question


def test_parse_ask_args_no_modifiers():
    req = oracle.parse_ask_args("what keeps coming back?")
    assert req.since is None
    assert req.limit == 8
    assert req.question == "what keeps coming back?"


def test_parse_ask_args_limit_range():
    assert oracle.parse_ask_args("limit:1 x").limit == 1
    assert oracle.parse_ask_args("limit:25 x").limit == 25
    assert oracle.parse_ask_args("limit:0 x").limit == 8
    assert oracle.parse_ask_args("limit:26 x").limit == 8


# ---- FTS5 query sanitization --------------------------------------------

def test_fts_query_strips_stopwords_and_specials():
    assert oracle._fts_query("what do I really want?") == "really want"
    assert oracle._fts_query('"hello" * world -x') == "hello world x"
    assert oracle._fts_query("the a an") == ""
    assert oracle._fts_query("") == ""


def test_fts_query_lowercases():
    assert oracle._fts_query("Desire LONGING") == "desire longing"


# ---- query expansion -----------------------------------------------------

@pytest.mark.asyncio
async def test_expand_query_parses_json_array(conn):
    prov = _SeqProv(['["desire","longing","yearning"]'])
    providers = Providers(prov, None)
    qs = await oracle.expand_query(
        "what do I really want?",
        settings=_settings(), providers=providers, conn=conn,
    )
    assert qs == ["desire", "longing", "yearning"]


@pytest.mark.asyncio
async def test_expand_query_tolerates_code_fences(conn):
    prov = _SeqProv(["```json\n[\"a\", \"b\"]\n```"])
    providers = Providers(prov, None)
    qs = await oracle.expand_query(
        "x", settings=_settings(), providers=providers, conn=conn,
    )
    assert qs == ["a", "b"]


@pytest.mark.asyncio
async def test_expand_query_caps_at_five(conn):
    prov = _SeqProv(['["a","b","c","d","e","f","g","h"]'])
    providers = Providers(prov, None)
    qs = await oracle.expand_query(
        "x", settings=_settings(), providers=providers, conn=conn,
    )
    assert len(qs) == 5


@pytest.mark.asyncio
async def test_expand_query_falls_back_on_junk_response(conn):
    prov = _SeqProv(["not json at all sorry"])
    providers = Providers(prov, None)
    qs = await oracle.expand_query(
        "the question", settings=_settings(), providers=providers, conn=conn,
    )
    assert qs == ["the question"]


@pytest.mark.asyncio
async def test_expand_query_falls_back_on_llm_exception(conn):
    class _Broken:
        name = "anthropic"
        async def chat(self, **kwargs):
            raise RuntimeError("API down")
    providers = Providers(_Broken(), None)
    qs = await oracle.expand_query(
        "the question", settings=_settings(), providers=providers, conn=conn,
    )
    assert qs == ["the question"]


# ---- retrieval -----------------------------------------------------------

async def _insert(conn, *, raw: str, local_date: str = "2026-04-21", kind: str = "text", msg_id: int = 0):
    cid = await db_mod.insert_capture(
        conn, kind=kind, raw=raw, source="telegram",
        dob=date(1990, 1, 1), tz_name="UTC", telegram_msg_id=msg_id,
    )
    if local_date:
        await conn.execute(
            "UPDATE captures SET local_date = ? WHERE id = ?",
            (local_date, cid),
        )
        await conn.commit()
    return cid


@pytest.mark.asyncio
async def test_oracle_fts_returns_ranked_captures(conn):
    a = await _insert(conn, raw="the impediment to action advances action", msg_id=1)
    await _insert(conn, raw="a small ignition in the morning", msg_id=2)
    await _insert(conn, raw="the afternoon settled into dusk", msg_id=3)

    fragments = await oracle.retrieve(conn=conn, queries=["impediment"], limit=5)
    assert len(fragments) == 1
    assert fragments[0].capture_id == a


@pytest.mark.asyncio
async def test_oracle_query_expansion_widens_recall(conn):
    await _insert(conn, raw="i felt a small ignition this morning", msg_id=1)
    await _insert(conn, raw="a quiet yearning for the afternoon", msg_id=2)
    await _insert(conn, raw="something kept pulling me back", msg_id=3)

    single = await oracle.retrieve(conn=conn, queries=["ignition"], limit=10)
    expanded = await oracle.retrieve(
        conn=conn, queries=["ignition", "yearning", "pulling"], limit=10,
    )
    assert len(single) == 1
    assert len(expanded) >= 2


@pytest.mark.asyncio
async def test_oracle_retrieve_dedupes_by_capture_id(conn):
    await _insert(conn, raw="desire and longing and wanting", msg_id=1)
    fragments = await oracle.retrieve(
        conn=conn, queries=["desire", "longing", "wanting"], limit=10,
    )
    assert len(fragments) == 1


@pytest.mark.asyncio
async def test_oracle_retrieve_respects_since_filter(conn):
    await _insert(conn, raw="the old thought", local_date="2026-03-01", msg_id=1)
    await _insert(conn, raw="the new thought", local_date="2026-04-21", msg_id=2)

    recent = await oracle.retrieve(
        conn=conn, queries=["thought"], since="2026-04-01", limit=10,
    )
    ids = [f.capture_id for f in recent]
    async with conn.execute(
        "SELECT id FROM captures WHERE raw = 'the new thought'"
    ) as cur:
        new_id = (await cur.fetchone())[0]
    assert ids == [new_id]


@pytest.mark.asyncio
async def test_oracle_retrieve_returns_empty_for_no_matches(conn):
    await _insert(conn, raw="a line about apples", msg_id=1)
    fragments = await oracle.retrieve(
        conn=conn, queries=["submarine"], limit=10,
    )
    assert fragments == []


@pytest.mark.asyncio
async def test_oracle_retrieve_skips_pathological_queries(conn):
    await _insert(conn, raw="content", msg_id=1)
    fragments = await oracle.retrieve(
        conn=conn, queries=["the a an", "content"], limit=10,
    )
    assert len(fragments) == 1


# ---- full ask flow -------------------------------------------------------

@pytest.mark.asyncio
async def test_oracle_empty_retrieval_returns_silence_message(conn):
    prov = _SeqProv(['["ghost"]'])
    providers = Providers(prov, None)
    await _insert(conn, raw="a line about something else", msg_id=1)

    answer, fragments = await oracle.ask(
        question_raw="tell me about dragons",
        settings=_settings(), providers=providers, conn=conn,
    )
    assert answer == oracle.SILENCE_MESSAGE
    assert fragments == []
    assert len(prov.calls) == 1


@pytest.mark.asyncio
async def test_oracle_synthesis_failure_reports_distinct_message(conn):
    """Regression: if retrieval found fragments but synthesis LLM failed, we
    must NOT return SILENCE_MESSAGE — the corpus isn't silent, the LLM
    broke. User needs to know to retry, not to doubt their corpus.
    """
    await _insert(conn, raw="the only fragment", msg_id=1)

    class _HalfBroken:
        name = "anthropic"
        def __init__(self):
            self._calls = 0
        async def chat(self, **kwargs):
            self._calls += 1
            if self._calls == 1:
                # Expansion succeeds
                return LlmResponse(
                    text='["fragment"]', model="m", provider="anthropic",
                    input_tokens=1, output_tokens=1,
                )
            raise RuntimeError("synthesis LLM outage")

    providers = Providers(_HalfBroken(), None)
    answer, fragments = await oracle.ask(
        question_raw="what have I saved?",
        settings=_settings(), providers=providers, conn=conn,
    )
    assert answer == oracle.SYNTHESIS_FAILED_MESSAGE
    assert answer != oracle.SILENCE_MESSAGE
    # Fragments were retrieved — that information isn't lost just because synth failed
    assert len(fragments) == 1


@pytest.mark.asyncio
async def test_oracle_empty_synthesis_response_reports_failure(conn):
    """A blank LLM response ('') shouldn't be reported as silence either —
    retrieval found fragments, the LLM just returned nothing useful."""
    await _insert(conn, raw="the only fragment", msg_id=1)
    prov = _SeqProv(['["fragment"]', ""])  # expansion ok, synthesis empty
    providers = Providers(prov, None)

    answer, fragments = await oracle.ask(
        question_raw="what have I saved?",
        settings=_settings(), providers=providers, conn=conn,
    )
    assert answer == oracle.SYNTHESIS_FAILED_MESSAGE
    assert len(fragments) == 1


@pytest.mark.asyncio
async def test_oracle_ask_empty_question_short_circuits(conn):
    prov = _SeqProv([])
    providers = Providers(prov, None)
    answer, _ = await oracle.ask(
        question_raw="   ",
        settings=_settings(), providers=providers, conn=conn,
    )
    assert answer == "ask me something."
    assert prov.calls == []


@pytest.mark.asyncio
async def test_oracle_voice_block_is_orchurator(conn):
    await _insert(conn, raw="the impediment to action advances action", msg_id=1)
    prov = _SeqProv([
        '["impediment","action"]',
        "you've been circling [1].",
    ])
    providers = Providers(prov, None)

    answer, _ = await oracle.ask(
        question_raw="what do i keep returning to?",
        settings=_settings(), providers=providers, conn=conn,
    )
    assert answer == "you've been circling [1]."

    synthesis_call = prov.calls[1]
    joined = "\n\n".join(synthesis_call["system_blocks"])
    assert "orchurator" in joined.lower()


@pytest.mark.asyncio
async def test_oracle_cites_fragment_ids_present_in_retrieval(conn):
    await _insert(conn, raw="first fragment", msg_id=1)
    await _insert(conn, raw="second fragment", msg_id=2)

    prov = _SeqProv([
        '["fragment"]',
        "you said [1], and later [2].",
    ])
    providers = Providers(prov, None)
    answer, fragments = await oracle.ask(
        question_raw="what have I captured?",
        settings=_settings(), providers=providers, conn=conn,
    )
    assert oracle.has_only_valid_citations(answer, len(fragments))
    assert set(oracle.extract_citations(answer)) <= {1, 2}


@pytest.mark.asyncio
async def test_oracle_detects_hallucinated_citations(conn, caplog):
    import logging
    await _insert(conn, raw="only fragment", msg_id=1)
    prov = _SeqProv([
        '["fragment"]',
        "you said [1] and also [7].",
    ])
    providers = Providers(prov, None)
    with caplog.at_level(logging.WARNING, logger="bot.oracle"):
        answer, fragments = await oracle.ask(
            question_raw="what have I captured?",
            settings=_settings(), providers=providers, conn=conn,
        )
    assert not oracle.has_only_valid_citations(answer, len(fragments))
    assert any("out-of-range" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_oracle_ask_supports_since_filter(conn):
    await _insert(conn, raw="an old thought about structure",
                   local_date="2026-03-01", msg_id=1)
    await _insert(conn, raw="a recent thought about structure",
                   local_date="2026-04-21", msg_id=2)

    prov = _SeqProv([
        '["structure"]',
        "[1] is recent.",
    ])
    providers = Providers(prov, None)
    answer, fragments = await oracle.ask(
        question_raw="since:2026-04-01 thoughts on structure",
        settings=_settings(), providers=providers, conn=conn,
    )
    assert len(fragments) == 1
    assert "recent thought" in fragments[0].raw_excerpt


@pytest.mark.asyncio
async def test_oracle_uses_processed_summary_when_raw_empty(conn):
    cid = await db_mod.insert_capture(
        conn, kind="image", raw=None, source="telegram",
        processed={"title": "t", "tags": [], "quotes": [],
                   "summary": "a photo of the afternoon light"},
        payload={"vision": {"ocr": "", "description": "room in amber"}},
        dob=date(1990, 1, 1), tz_name="UTC", telegram_msg_id=1,
    )
    fragments = await oracle.retrieve(
        conn=conn, queries=["afternoon"], limit=5,
    )
    assert len(fragments) == 1
    assert "afternoon light" in fragments[0].raw_excerpt


# ---- handler integration -------------------------------------------------

@pytest.mark.asyncio
async def test_ask_handler_replies_with_oracle_answer(conn):
    from bot.handlers import ask_handler

    settings = _settings()
    await _insert(conn, raw="the impediment to action advances action", msg_id=1)

    prov = _SeqProv([
        '["impediment"]',
        "[1] advances action.",
    ])
    providers = Providers(prov, None)

    update = MagicMock()
    update.effective_user = MagicMock(); update.effective_user.id = 42
    update.message = MagicMock(); update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.args = ["what", "stops", "me?"]
    context.bot_data = {"settings": settings, "db": conn, "providers": providers}

    await ask_handler(update, context)
    update.message.reply_text.assert_awaited_once_with("[1] advances action.")


@pytest.mark.asyncio
async def test_ask_handler_shows_usage_on_empty_args(conn):
    from bot.handlers import ask_handler
    settings = _settings()
    providers = Providers(_SeqProv([]), None)

    update = MagicMock()
    update.effective_user = MagicMock(); update.effective_user.id = 42
    update.message = MagicMock(); update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.args = []
    context.bot_data = {"settings": settings, "db": conn, "providers": providers}

    await ask_handler(update, context)
    args = update.message.reply_text.await_args.args[0]
    assert "usage" in args.lower()
    assert "since:" in args
    assert "limit:" in args


@pytest.mark.asyncio
async def test_ask_handler_rejects_when_no_providers(conn):
    """If the bot booted without any LLM keys, /ask should fail fast with a
    clear message instead of crashing inside the Oracle."""
    from bot.handlers import ask_handler
    settings = _settings()

    update = MagicMock()
    update.effective_user = MagicMock(); update.effective_user.id = 42
    update.message = MagicMock(); update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.args = ["what", "keeps", "coming", "back?"]
    context.bot_data = {"settings": settings, "db": conn}  # no providers

    await ask_handler(update, context)
    msg = update.message.reply_text.await_args.args[0]
    assert "no LLM" in msg or "cannot ask" in msg
