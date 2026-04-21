"""PDF ingestion: extract text, classify size, feed the existing process pipeline.

Sizing philosophy: pages are too lossy (a 20-page dense paper can be heavier
than a 200-page novel excerpt), so the primary gate is the post-extraction
token estimate. A cheap page cap is the backstop that saves us from touching
a 500-page PDF in the first place.

Thresholds:
- MAX_PAGES: reject outright above this — avoids wasting time on huge docs
- LARGE_TOKENS: reject if post-extract tokens exceed this — protects ingest cost

Accepted PDFs are inserted with full text in `raw` (searchable via FTS5) and
metadata (page count, token estimate, tier) in `payload`. Existing process.py
truncates content at 30k chars for the LLM call, so no extra clamping here.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Literal

from pypdf import PdfReader

log = logging.getLogger(__name__)


# Tune after seeing real usage. Token estimate is ~4 chars/token — close
# enough for English prose; non-Latin scripts will skew.
MAX_PAGES = 50
TINY_TOKENS = 5_000
LARGE_TOKENS = 20_000
_CHARS_PER_TOKEN = 4


Tier = Literal["tiny", "medium", "large"]


@dataclass
class PdfExtract:
    text: str
    page_count: int
    char_count: int
    token_estimate: int
    tier: Tier
    rejected_reason: str | None  # None if accepted; human-readable if not


def estimate_tokens(text: str) -> int:
    return len(text) // _CHARS_PER_TOKEN


def classify(page_count: int, token_estimate: int) -> tuple[Tier, str | None]:
    """Return (tier, rejected_reason). `rejected_reason` is None when accepted."""
    if page_count > MAX_PAGES:
        return "large", (
            f"too many pages ({page_count} > {MAX_PAGES}). "
            f"send specific passages as text or a photo of the highlight."
        )
    if token_estimate > LARGE_TOKENS:
        return "large", (
            f"too much text (~{token_estimate} tokens > {LARGE_TOKENS}). "
            f"send specific passages as text or a photo of the highlight."
        )
    if token_estimate > TINY_TOKENS:
        return "medium", None
    return "tiny", None


def extract_pdf_bytes(pdf_bytes: bytes) -> PdfExtract:
    """Extract text from a PDF. Never raises on malformed input — returns an
    empty extract with rejected_reason set. pypdf is fast and local; the only
    real failure modes are encrypted PDFs and corrupt bytes.
    """
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as e:
        # pypdf can raise PdfReadError, EmptyFileError, or various
        # ValueError/TypeError shapes depending on the malformation. The
        # handler treats any failure here as a user-fixable rejection.
        return PdfExtract(
            text="", page_count=0, char_count=0, token_estimate=0,
            tier="large",
            rejected_reason=f"couldn't read the PDF: {type(e).__name__}: {e}",
        )

    if reader.is_encrypted:
        return PdfExtract(
            text="", page_count=0, char_count=0, token_estimate=0,
            tier="large", rejected_reason="the PDF is password-protected.",
        )

    page_count = len(reader.pages)
    if page_count > MAX_PAGES:
        return PdfExtract(
            text="", page_count=page_count, char_count=0, token_estimate=0,
            tier="large",
            rejected_reason=(
                f"too many pages ({page_count} > {MAX_PAGES}). "
                f"send specific passages as text or a photo of the highlight."
            ),
        )

    parts: list[str] = []
    for i, page in enumerate(reader.pages):
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            log.warning("pdf: failed to extract page %d; skipping", i + 1)
    text = "\n\n".join(p.strip() for p in parts if p.strip())
    char_count = len(text)
    token_estimate = estimate_tokens(text)
    tier, reason = classify(page_count, token_estimate)
    return PdfExtract(
        text=text, page_count=page_count, char_count=char_count,
        token_estimate=token_estimate, tier=tier, rejected_reason=reason,
    )
