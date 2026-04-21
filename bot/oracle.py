"""The Oracle — `/ask` lets the user consult their past self.

Flow:
1. Parse the question + optional modifiers (since:YYYY-MM-DD, limit:N).
2. Expand the question into 3-5 FTS5 queries via a cheap LLM call.
3. Retrieve captures matching ANY expanded query (BM25 ranking, union + dedupe).
4. If retrieval is empty → reply "the corpus is silent on this."
5. Otherwise, call the Oracle LLM with the numbered fragments and return a
   ≤3-sentence, orchurator-voiced answer that cites fragments by [N].
6. Validate that every [N] in the answer corresponds to a retrieved fragment;
   log a warning on hallucinated citations (don't rewrite the response).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiosqlite

from bot.config import Settings
from bot.llm.base import Message
from bot.llm.router import Providers, call_llm
from bot.persona import VOICE_ORCHURATOR
from bot.prompts import SYSTEM_ORACLE, SYSTEM_ORACLE_EXPAND

log = logging.getLogger(__name__)


_DEFAULT_LIMIT = 8
_MAX_LIMIT = 25

# Words dropped from FTS5 queries before passing to MATCH. These dominate a
# bag-of-tokens search and push useful signal off the top hits.
_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "i", "you", "we", "they", "he", "she", "it",
    "what", "how", "when", "where", "why", "who", "which",
    "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "done",
    "of", "to", "in", "on", "at", "by", "for", "with", "from",
    "and", "or", "not", "near",  # FTS5 operator keywords
    "my", "me", "your", "our", "their",
})

# Strip all non-word, non-space characters before tokenizing. That covers
# FTS5 syntax chars (`"`, `*`, `+`, `-`, `(`, `)`, `:`) as well as general
# punctuation (`?`, `.`, `!`, `,`) that the FTS5 tokenizer would drop anyway.
_FTS_SPECIAL_RE = re.compile(r"[^\w\s]", re.UNICODE)


# ---- question parsing -----------------------------------------------------


@dataclass
class AskRequest:
    question: str
    since: str | None   # YYYY-MM-DD
    limit: int          # [1, _MAX_LIMIT]


def parse_ask_args(raw: str) -> AskRequest:
    """Pull `since:YYYY-MM-DD` and `limit:N` out of the question text.
    Unknown or malformed modifiers are treated as part of the question.
    """
    since: str | None = None
    limit: int = _DEFAULT_LIMIT
    kept: list[str] = []
    for word in (raw or "").strip().split():
        if ":" in word:
            key, _, val = word.partition(":")
            if key == "since":
                try:
                    datetime.strptime(val, "%Y-%m-%d")
                    since = val
                    continue
                except ValueError:
                    pass
            elif key == "limit":
                try:
                    n = int(val)
                    if 1 <= n <= _MAX_LIMIT:
                        limit = n
                        continue
                except ValueError:
                    pass
        kept.append(word)
    return AskRequest(question=" ".join(kept).strip(), since=since, limit=limit)


# ---- query expansion ------------------------------------------------------


def _coerce_query_list(raw: str) -> list[str]:
    """Extract a JSON array of strings from a possibly-messy LLM response."""
    if not raw:
        return []
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", cleaned, flags=re.DOTALL)
        if not m:
            return []
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    if not isinstance(obj, list):
        return []
    return [s.strip() for s in obj if isinstance(s, str) and s.strip()]


async def expand_query(
    question: str,
    *,
    settings: Settings,
    providers: Providers,
    conn: aiosqlite.Connection,
) -> list[str]:
    """Generate 3-5 FTS5 query strings. Falls back to the raw question if
    the LLM call or parse fails — `/ask` should still retrieve something.
    """
    try:
        response = await call_llm(
            purpose="oracle",
            system_blocks=[SYSTEM_ORACLE_EXPAND],
            messages=[Message(role="user", content=question)],
            max_tokens=200,
            settings=settings, providers=providers, conn=conn,
        )
    except Exception:
        log.exception("oracle: expansion LLM call failed")
        return [question] if question else []

    queries = _coerce_query_list(response.text)
    if not queries:
        return [question] if question else []
    # Cap at 5 to bound retrieval cost.
    return queries[:5]


# ---- retrieval ------------------------------------------------------------


def _fts_query(raw: str) -> str:
    """Clean an arbitrary string into an FTS5 MATCH argument.

    Strips FTS5 special syntax and common stopwords. Joins remaining tokens
    with space — FTS5's default AND semantics means all tokens must appear.
    Returns empty string when no content tokens survive.
    """
    cleaned = _FTS_SPECIAL_RE.sub(" ", raw or "")
    tokens = [
        t for t in cleaned.lower().split()
        if t and t not in _STOPWORDS
    ]
    return " ".join(tokens)


@dataclass
class OracleFragment:
    capture_id: int
    kind: str
    url: str | None
    local_date: str
    raw_excerpt: str           # trimmed to ~400 chars
    bm25_rank: float           # lower is better


async def retrieve(
    *,
    conn: aiosqlite.Connection,
    queries: list[str],
    since: str | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> list[OracleFragment]:
    """Run each FTS5 query, union by capture_id, rank by best BM25, cap at limit."""
    best_by_id: dict[int, OracleFragment] = {}
    for q in queries:
        fts_arg = _fts_query(q)
        if not fts_arg:
            continue
        params: list[Any] = [fts_arg]
        date_clause = ""
        if since:
            date_clause = " AND c.local_date >= ?"
            params.append(since)
        # Per-query pool size: slightly larger than final limit so the union
        # has room to rank. BM25 is negative-scaled; ORDER BY rank ASC gives best.
        params.append(limit * 2)
        sql = f"""
            SELECT c.id, c.kind, c.url, c.raw, c.processed,
                   c.local_date, captures_fts.rank AS bm25_rank
            FROM captures_fts
            JOIN captures c ON c.id = captures_fts.rowid
            WHERE captures_fts MATCH ?{date_clause}
            ORDER BY captures_fts.rank
            LIMIT ?
        """
        try:
            async with conn.execute(sql, params) as cur:
                rows = await cur.fetchall()
        except aiosqlite.OperationalError as e:
            # FTS5 can throw on pathological queries (e.g. a token list that
            # tokenizes to nothing after its own rules). Skip, don't crash.
            log.warning("oracle: FTS5 query %r failed: %s", fts_arg, e)
            continue
        for row in rows:
            cap_id = int(row["id"])
            prev = best_by_id.get(cap_id)
            rank = float(row["bm25_rank"])
            if prev is None or rank < prev.bm25_rank:
                best_by_id[cap_id] = OracleFragment(
                    capture_id=cap_id,
                    kind=row["kind"],
                    url=row["url"],
                    local_date=row["local_date"],
                    raw_excerpt=_best_excerpt(row),
                    bm25_rank=rank,
                )
    ranked = sorted(best_by_id.values(), key=lambda f: f.bm25_rank)
    return ranked[:limit]


def _best_excerpt(row: Any) -> str:
    """Pick the most informative text slice for an Oracle fragment."""
    raw = (row["raw"] or "").strip()
    if raw:
        return raw[:400]
    # Fall back to processed.summary if raw is empty (image captures etc.)
    processed_raw = row["processed"]
    if processed_raw:
        try:
            p = json.loads(processed_raw)
        except json.JSONDecodeError:
            p = None
        if isinstance(p, dict):
            summary = p.get("summary")
            if isinstance(summary, str) and summary.strip():
                return summary.strip()[:400]
    return "(no text)"


# ---- synthesis ------------------------------------------------------------


def _format_fragments(fragments: list[OracleFragment]) -> str:
    """Render the numbered fragment bundle the Oracle LLM sees."""
    lines = []
    for i, f in enumerate(fragments, 1):
        lines.append(f'[{i}] ({f.local_date}, {f.kind}) "{f.raw_excerpt}"')
    return "\n".join(lines)


_CITATION_RE = re.compile(r"\[(\d+)\]")


def extract_citations(text: str) -> list[int]:
    return [int(m) for m in _CITATION_RE.findall(text or "")]


def has_only_valid_citations(text: str, num_fragments: int) -> bool:
    return all(1 <= c <= num_fragments for c in extract_citations(text))


SILENCE_MESSAGE = "the corpus is silent on this."
SYNTHESIS_FAILED_MESSAGE = "something slipped while asking. try again."


async def ask(
    *,
    question_raw: str,
    settings: Settings,
    providers: Providers,
    conn: aiosqlite.Connection,
) -> tuple[str, list[OracleFragment]]:
    """Top-level entry point. Returns (response_text, fragments_cited)."""
    req = parse_ask_args(question_raw)
    if not req.question:
        return ("ask me something.", [])

    queries = await expand_query(
        req.question, settings=settings, providers=providers, conn=conn,
    )
    fragments = await retrieve(
        conn=conn, queries=queries, since=req.since, limit=req.limit,
    )
    if not fragments:
        return (SILENCE_MESSAGE, [])

    bundle = (
        f"Question: {req.question}\n\n"
        f"Fragments retrieved from the user's commonplace:\n"
        f"{_format_fragments(fragments)}"
    )
    try:
        response = await call_llm(
            purpose="oracle",
            system_blocks=[VOICE_ORCHURATOR, SYSTEM_ORACLE],
            messages=[Message(role="user", content=bundle)],
            max_tokens=400,
            settings=settings, providers=providers, conn=conn,
        )
    except Exception:
        # Fragments exist — tell the user the LLM failed rather than silently
        # claiming "corpus is silent", which would be a lie.
        log.exception("oracle: synthesis LLM call failed")
        return (SYNTHESIS_FAILED_MESSAGE, fragments)

    answer = (response.text or "").strip()
    if not answer:
        return (SYNTHESIS_FAILED_MESSAGE, fragments)

    if not has_only_valid_citations(answer, len(fragments)):
        # Log but don't rewrite — let the user see the actual output rather
        # than a silently-edited version. Stage 7 may tighten.
        log.warning(
            "oracle: response cites out-of-range fragment ids: %s (retrieved %d)",
            extract_citations(answer), len(fragments),
        )
    return (answer, fragments)
