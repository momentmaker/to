#!/usr/bin/env python3
"""Weekly digest CLI for `to` — the Sunday-morning ritual.

This is the single most important file in this project. Everything else
captures your week. This reads the week back to you.

A typical Sunday:

    $ cd ~/my-commonplace && git pull
    $ weekly_digest
    ────────────────────────────────────────────────────────
      weekly digest · 2026-w17
    ────────────────────────────────────────────────────────

      loading captures from 2026-w17/                  17 files
        ├─ 12 text
        ├─ 3 url (+ 2 why replies)
        └─ 2 voice

      calling claude-opus-4-7 ⠋
      validating quote-only                    18 / 18 ✓

    ────────────────────────────────────────────────────────
      🕯  a week of small ignitions
    ────────────────────────────────────────────────────────

      <essay rendered here, your own words woven back>

      cost: ~$0.42 · 17.3s elapsed

      accept? [y/N/r(etry)/e(dit)] y

      wrote 2026-w17/digest.md
      updated fz-ax-backup.json
      pushed to origin/main

      done. open fz.ax to see the week.

Usage:

    cd ~/my-commonplace
    weekly_digest                               # most recent week, interactive
    weekly_digest --week 2026-w17               # specific week
    weekly_digest --list                        # show all weeks + digest status
    weekly_digest --dry-run                     # preview, write nothing
    weekly_digest --yes                         # skip the confirm prompt
    weekly_digest --push                        # pull, generate, commit, push
    weekly_digest --model claude-sonnet-4-6     # cheaper/faster for testing

Dependencies:
    pip install anthropic tomli_w grapheme rich

Environment:
    ANTHROPIC_API_KEY       required
    DOB                     only needed if fz-ax-backup.json doesn't exist yet
    WEEK_START              mon | sun (default mon; only for first-time fz-ax init)

Run from the root of your captures repo (the one that contains `YYYY-wNN/`
directories). The script writes two files on success:

  - `YYYY-wNN/digest.md`   — the anthology essay, your voice
  - `fz-ax-backup.json`    — cumulative, updated with this week's entry

Strict quote-only discipline: every sentence of the essay must be a
verbatim or minimally-edited substring of one of the week's fragments
(case-insensitive, punctuation-normalized). The LLM is instructed to
respect this AND every output is post-validated — if any sentence didn't
appear in your captures, we retry once with the offenders called out;
a second failure exits without writing. You cannot get hallucinated prose.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import anthropic
    import grapheme
    import tomli_w
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.prompt import Prompt
    from rich.rule import Rule
    from rich.spinner import Spinner
    from rich.status import Status
    from rich.table import Table
    from rich.text import Text
except ImportError as e:
    sys.stderr.write(
        f"Missing dependency ({e.name}).\n\n"
        f"Install everything the script needs:\n"
        f"  pip install anthropic tomli_w grapheme rich\n"
    )
    sys.exit(1)


# ============================================================================
# Prompts — KEEP IN SYNC with bot/prompts.py
# ============================================================================

SYSTEM_DIGEST = """You assemble the user's own week into an anthology essay. You have their saved fragments, scraped bodies, why-follow-ups, and daily reflections from a single week.

Return a single JSON object with exactly these keys:
  "essay":    a short anthology essay (3-8 short paragraphs, <=1200 words) woven entirely from the user's own words.
  "whisper":  a one-sentence distillation of the week, <=240 characters, in the user's voice.
  "mark":     exactly ONE grapheme (one character or one emoji) that captures the week's aesthetic.

The essay is not yours. It is the user reading themselves back. Select, order, and juxtapose their fragments so a thread emerges.

Respond with ONLY the JSON object — no prose, no markdown, no code fences.
""".strip()


QUOTE_ONLY_RULES = """Strict quote-only rules for the essay:

- Every sentence of the essay must be a verbatim or near-verbatim substring of the provided fragments. "Near-verbatim" means: identical content, case-insensitive, with punctuation and whitespace normalized. You may drop stopwords or trim leading/trailing words from a quote, but you MUST NOT invent, paraphrase, or combine content from different fragments into a new sentence.
- Do NOT add connective sentences. Do NOT summarize. Do NOT explain.
- Line-break and paragraph-break to control pacing; that is your only authorial move.
- If two fragments say similar things, pick the sharper one. Do not conflate them.
- If the week is thin, the essay is thin. Do not pad.
""".strip()


RETRY_SUFFIX = """
Your previous response violated the quote-only rule. The following sentences from your essay did not appear in the user's fragments:

{offenders}

Regenerate the JSON. Every sentence of the new essay must be a substring (after normalization) of one fragment.
""".strip()


# Approximate per-million-token pricing (USD). Used for cost estimate only.
_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-7":           (15.0, 75.0),
    "claude-sonnet-4-6":         (3.0,  15.0),
    "claude-haiku-4-5-20251001": (0.8,  4.0),
}


console = Console(stderr=False)
err_console = Console(stderr=True)


# ============================================================================
# Validators — KEEP IN SYNC with bot/digest/validate.py
# ============================================================================

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WS_RE = re.compile(r"\s+")


def is_single_grapheme(s: str) -> bool:
    return grapheme.length((s or "").strip()) == 1


def whisper_ok(s: str) -> bool:
    return 0 < grapheme.length((s or "").strip()) <= 240


def normalize_for_quote_check(text: str) -> str:
    s = (text or "").lower()
    s = _PUNCT_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def split_sentences(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    flat = _WS_RE.sub(" ", text).strip()
    return [p.strip() for p in _SENTENCE_SPLIT.split(flat) if p.strip()]


def validate_quote_only(essay: str, corpus_texts: list[str]) -> tuple[bool, list[str], int]:
    """Returns (ok, offending_sentences, total_sentences_checked)."""
    sentences = split_sentences(essay)
    if not sentences:
        return False, ["[empty essay]"], 0
    real = [(s, normalize_for_quote_check(s)) for s in sentences]
    real = [(s, ns) for s, ns in real if ns]
    if not real:
        return False, ["[empty essay]"], 0
    combined = " ".join(t for t in corpus_texts if isinstance(t, str))
    norm_corpus = normalize_for_quote_check(combined)
    offenders = [s for s, ns in real if ns not in norm_corpus]
    return (not offenders), offenders, len(real)


def extract_single_grapheme(mark: str) -> str:
    s = (mark or "").strip()
    if not s:
        return ""
    parts = list(grapheme.graphemes(s))
    return parts[0] if parts else ""


# ============================================================================
# Capture loading
# ============================================================================

_WEEK_DIR_RE = re.compile(r"^\d{4}-w\d{2}$")


@dataclass
class Capture:
    file: str
    fm: dict[str, Any]
    body: str

    @property
    def kind(self) -> str:
        return str(self.fm.get("kind", "text"))


def find_week_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and _WEEK_DIR_RE.match(p.name))


def count_captures_in_dir(path: Path) -> int:
    return sum(1 for p in path.glob("*.md") if p.name != "digest.md")


def most_recent_week(root: Path) -> str | None:
    dirs = find_week_dirs(root)
    return dirs[-1].name if dirs else None


def _parse_frontmatter(text: str) -> tuple[dict | None, str]:
    if not text.startswith("+++"):
        return None, text
    try:
        end = text.index("\n+++\n", 3)
    except ValueError:
        return None, text
    try:
        fm = tomllib.loads(text[4:end + 1])
    except Exception:
        return None, text
    return fm, text[end + 5:]


def load_week(root: Path, week: str) -> list[Capture]:
    week_dir = root / week
    if not week_dir.is_dir():
        raise FileNotFoundError(f"No directory: {week_dir}")
    captures: list[Capture] = []
    for md in sorted(week_dir.glob("*.md")):
        if md.name == "digest.md":
            continue
        text = md.read_text(encoding="utf-8")
        fm, body = _parse_frontmatter(text)
        if fm is None:
            continue
        captures.append(Capture(file=md.name, fm=fm, body=body))
    return captures


def capture_breakdown(captures: list[Capture]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for c in captures:
        counts[c.kind] = counts.get(c.kind, 0) + 1
    return counts


# ============================================================================
# Corpus assembly
# ============================================================================

def build_corpus(captures: list[Capture]) -> tuple[str, list[str]]:
    """Format fragments for the LLM, and return the quotable substring list
    for the post-validator.
    """
    lines = ["Week's fragments:"]
    quotable: list[str] = []

    for i, cap in enumerate(captures, 1):
        kind = cap.kind
        local_date = cap.fm.get("local_date", "")
        title = cap.fm.get("title")

        header = f"[{i}] ({kind}) {local_date}"
        if title:
            header += f" — {title}"
        lines.append(header)

        body = cap.body.strip()
        if body:
            lines.append(f"  {body[:1500]}")
            quotable.append(body)

    return "\n".join(lines), quotable


# ============================================================================
# Claude call
# ============================================================================

@dataclass
class LlmResult:
    text: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int

    def cost_usd(self, model: str) -> float:
        prices = _PRICING.get(model)
        if not prices:
            return 0.0
        inp, out = prices
        regular = max(self.input_tokens, 0)
        c = regular * inp / 1_000_000
        c += self.cache_read_tokens * inp * 0.1 / 1_000_000
        c += self.cache_write_tokens * inp * 1.25 / 1_000_000
        c += self.output_tokens * out / 1_000_000
        return round(c, 4)


def call_claude(
    client: anthropic.Anthropic,
    *,
    model: str,
    corpus: str,
    retry_history: list[dict] | None = None,
) -> LlmResult:
    system = [
        {"type": "text", "text": SYSTEM_DIGEST, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": QUOTE_ONLY_RULES, "cache_control": {"type": "ephemeral"}},
    ]
    messages = retry_history or [{"role": "user", "content": corpus}]
    resp = client.messages.create(
        model=model,
        max_tokens=4096,
        system=system,
        messages=messages,
        timeout=180,
    )
    text = "".join(
        b.text for b in resp.content
        if getattr(b, "type", None) == "text"
    )
    usage = resp.usage
    return LlmResult(
        text=text,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
    )


def coerce_json(raw: str) -> dict | None:
    if not raw:
        return None
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def validate_output(obj: dict, quotable: list[str]) -> tuple[bool, dict, list[str], int]:
    essay = obj.get("essay") if isinstance(obj.get("essay"), str) else ""
    whisper = obj.get("whisper") if isinstance(obj.get("whisper"), str) else ""
    mark = extract_single_grapheme(obj.get("mark") or "")

    offenders: list[str] = []
    if not is_single_grapheme(mark):
        offenders.append(f"[mark] '{mark}' is not a single grapheme")
    if not whisper_ok(whisper):
        offenders.append(f"[whisper] length {len(whisper)} chars — must be 1..240")

    essay_ok, essay_offenders, sentence_count = validate_quote_only(essay, quotable)
    offenders.extend(essay_offenders)

    clean = {"essay": essay.strip(), "whisper": whisper.strip(), "mark": mark}
    return (not offenders), clean, offenders, sentence_count


# ============================================================================
# Output rendering + writing
# ============================================================================

def render_digest_md(iso_week: str, clean: dict) -> str:
    return (
        f"# {iso_week}\n\n"
        f"**{clean.get('mark', '')}**  _{clean.get('whisper', '')}_\n\n"
        f"{clean.get('essay', '').strip()}\n"
    )


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def update_fz_backup(
    path: Path,
    *,
    fz_week_idx: int,
    mark: str,
    whisper: str,
    marked_at: str,
    dob: str | None = None,
    week_start: str = "mon",
) -> bool:
    """Surgical update. Creates a minimal fz-ax-backup.json if it doesn't
    exist. Returns True if written, False if skipped (e.g. can't bootstrap).
    """
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        inner = state["state"]
    else:
        if not dob:
            return False
        inner = {
            "version": 1,
            "dob": dob,
            "weeks": {},
            "vow": None,
            "letters": [],
            "anchors": [],
            "prefs": {
                "theme": "auto",
                "pushOptIn": False,
                "reducedMotion": "auto",
                "weekStart": week_start if week_start in ("mon", "sun") else "mon",
            },
            "meta": {"createdAt": _now_iso()},
        }
        state = {"fzAxBackup": True, "exportedAt": "", "state": inner}

    inner.setdefault("weeks", {})[str(fz_week_idx)] = {
        "mark": mark,
        "whisper": whisper,
        "markedAt": marked_at,
    }
    anchors = set(int(a) for a in inner.get("anchors", []))
    anchors.add(int(fz_week_idx))
    inner["anchors"] = sorted(anchors)
    state["exportedAt"] = _now_iso()

    path.write_text(
        json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return True


# ============================================================================
# Git helpers
# ============================================================================

def _git(args: list[str], cwd: Path) -> tuple[int, str]:
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd,
            capture_output=True, text=True, timeout=60,
        )
        return out.returncode, (out.stdout + out.stderr).strip()
    except Exception as e:
        return 1, str(e)


def git_available(cwd: Path) -> bool:
    if not shutil.which("git"):
        return False
    rc, _ = _git(["rev-parse", "--is-inside-work-tree"], cwd)
    return rc == 0


def git_pull(cwd: Path) -> tuple[bool, str]:
    rc, out = _git(["pull", "--ff-only"], cwd)
    return rc == 0, out


def git_commit_and_push(cwd: Path, *, paths: list[str], message: str) -> tuple[bool, str]:
    for p in paths:
        _git(["add", p], cwd)
    rc, out = _git(["commit", "-m", message], cwd)
    if rc != 0 and "nothing to commit" not in out.lower():
        return False, out
    rc, out = _git(["push"], cwd)
    return rc == 0, out


# ============================================================================
# UI helpers
# ============================================================================

def _rule(title: str) -> Rule:
    return Rule(Text(title, style="bold cyan"), style="dim")


def print_header(week: str) -> None:
    console.print()
    console.print(_rule(f"weekly digest · {week}"))
    console.print()


def print_capture_summary(week: str, captures: list[Capture], why_count: int) -> None:
    breakdown = capture_breakdown(captures)
    total = len(captures)
    console.print(
        f"  [dim]loading captures from[/dim] {week}/[dim]...[/dim]  [bold]{total} files[/bold]"
    )
    items = []
    for kind in ("text", "url", "voice", "image", "reflection"):
        n = breakdown.get(kind, 0)
        if n > 0:
            items.append(f"{n} {kind}")
    if why_count:
        items[-1] = items[-1] + f" (+ {why_count} why {'reply' if why_count == 1 else 'replies'})"
    for i, item in enumerate(items):
        branch = "└─" if i == len(items) - 1 else "├─"
        console.print(f"    [dim]{branch}[/dim] {item}")
    console.print()


def print_ok(msg: str) -> None:
    console.print(f"  [green]✓[/green] {msg}")


def print_warn(msg: str) -> None:
    console.print(f"  [yellow]![/yellow] {msg}")


def print_err(msg: str) -> None:
    err_console.print(f"  [red]✗[/red] {msg}")


def print_digest(clean: dict, result: LlmResult, model: str, elapsed: float) -> None:
    console.print()
    console.print(_rule(f"{clean['mark']}  {clean['whisper']}"))
    console.print()
    console.print(Markdown(clean["essay"]))
    console.print()
    cost = result.cost_usd(model)
    console.print(
        f"  [dim]cost: ~${cost:.3f} · {elapsed:.1f}s elapsed · "
        f"{result.input_tokens}→{result.output_tokens} tokens"
        f"{' · cache ' + str(result.cache_read_tokens) + ' read' if result.cache_read_tokens else ''}"
        f"[/dim]"
    )
    console.print()


def show_offenders(offenders: list[str]) -> None:
    console.print()
    console.print(f"  [red]quote-only validation found {len(offenders)} offender(s):[/red]")
    for o in offenders[:10]:
        console.print(f"    [red]·[/red] {o[:200]}")
    console.print()


# ============================================================================
# Commands
# ============================================================================

def cmd_list(root: Path) -> int:
    dirs = find_week_dirs(root)
    if not dirs:
        err_console.print(f"[yellow]No week directories found under[/yellow] {root}")
        err_console.print(
            "[dim]Run from the root of your captures repo (the one with "
            "YYYY-wNN/ directories).[/dim]"
        )
        return 1

    table = Table(
        title=f"Weeks in {root}",
        title_style="bold cyan",
        show_lines=False,
        expand=False,
    )
    table.add_column("Week", style="bold")
    table.add_column("Captures", justify="right")
    table.add_column("Digest", justify="center")
    for d in dirs:
        count = count_captures_in_dir(d)
        has_digest = (d / "digest.md").exists()
        mark = "[green]✓[/green]" if has_digest else "[dim]—[/dim]"
        table.add_row(d.name, str(count), mark)
    console.print(table)
    return 0


def _count_whys_in_bodies(captures: list[Capture]) -> int:
    count = 0
    for c in captures:
        # Rough heuristic: a parent's body contains "## why?" sections if
        # children exist. Each why is prefixed with `> _TIMESTAMP_`.
        count += c.body.count("> _")
    return count


def cmd_generate(root: Path, args: argparse.Namespace) -> int:
    week = args.week or most_recent_week(root)
    if not week:
        print_err(
            "no week directory found. run from your captures repo, "
            "or pass --week YYYY-wNN."
        )
        return 1
    if not _WEEK_DIR_RE.match(week):
        print_err(f"--week must match YYYY-wNN (got {week!r}).")
        return 1

    print_header(week)

    # Optional pull
    if args.push:
        if not git_available(root):
            print_warn("--push specified but this isn't a git repo; skipping pull")
        else:
            with console.status("[dim]pulling latest...[/dim]", spinner="dots"):
                ok, out = git_pull(root)
            if ok:
                print_ok("pulled from origin")
            else:
                print_warn(f"git pull failed — continuing anyway: {out}")

    # Load captures
    try:
        captures = load_week(root, week)
    except FileNotFoundError as e:
        print_err(str(e))
        return 1
    if not captures:
        print_err(f"no captures in {week}.")
        return 1

    why_count = _count_whys_in_bodies(captures)
    print_capture_summary(week, captures, why_count)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print_err("set ANTHROPIC_API_KEY in your environment and try again.")
        return 1

    client = anthropic.Anthropic(api_key=api_key)
    corpus, quotable = build_corpus(captures)

    # First LLM call
    t0 = time.monotonic()
    with console.status(
        f"[dim]calling[/dim] {args.model}",
        spinner="dots",
    ):
        try:
            result = call_claude(client, model=args.model, corpus=corpus)
        except Exception as e:
            print_err(f"claude call failed: {e}")
            return 3
    print_ok(f"generated in {time.monotonic() - t0:.1f}s")

    parsed = coerce_json(result.text) or {}
    ok, clean, offenders, sentences = validate_output(parsed, quotable)

    if ok:
        print_ok(f"quote-only validation passed  [dim]{sentences}/{sentences} sentences ✓[/dim]")
    else:
        print_warn(f"validation failed — {len(offenders)} offender(s). retrying once...")
        retry_history = [
            {"role": "user",      "content": corpus},
            {"role": "assistant", "content": result.text or ""},
            {"role": "user",      "content": RETRY_SUFFIX.format(
                offenders="\n".join(f"- {o[:200]}" for o in offenders[:10])
            )},
        ]
        with console.status(
            f"[dim]retry with[/dim] {args.model}", spinner="dots"
        ):
            try:
                result2 = call_claude(
                    client, model=args.model, corpus=corpus,
                    retry_history=retry_history,
                )
            except Exception as e:
                print_err(f"retry failed: {e}")
                return 3
        # Merge result tokens for cost display
        result.input_tokens += result2.input_tokens
        result.output_tokens += result2.output_tokens
        result.cache_read_tokens += result2.cache_read_tokens
        result.cache_write_tokens += result2.cache_write_tokens
        result.text = result2.text

        parsed = coerce_json(result.text) or {}
        ok, clean, offenders, sentences = validate_output(parsed, quotable)
        if ok:
            print_ok(f"retry passed  [dim]{sentences}/{sentences} sentences ✓[/dim]")
        else:
            print_err("retry also failed. offending sentences shown below.")
            show_offenders(offenders)
            err_console.print(
                "[dim]Nothing written. Either your captures are very thin this "
                "week, or try a stronger model with --model claude-opus-4-7.[/dim]"
            )
            return 2

    elapsed = time.monotonic() - t0
    print_digest(clean, result, args.model, elapsed)

    # Decide whether to write
    if args.dry_run:
        console.print("  [yellow]--dry-run:[/yellow] nothing written.")
        return 0

    if not args.yes:
        choice = Prompt.ask(
            "  [bold]accept?[/bold]",
            choices=["y", "n", "r"],
            default="y",
            show_choices=True,
            show_default=True,
        )
        if choice == "n":
            console.print("  [yellow]declined.[/yellow]")
            return 0
        if choice == "r":
            # Simplest retry: user re-runs the command. Tell them.
            console.print(
                "  [yellow]to retry, run the same command again "
                "(or try [bold]--model claude-opus-4-7[/bold]).[/yellow]"
            )
            return 0

    # Write files
    iso_week = str(captures[0].fm.get("iso_week") or week.upper().replace("-W", "-W"))
    fz_week_idx = captures[0].fm.get("week_idx")

    digest_path = root / week / "digest.md"
    digest_path.write_text(render_digest_md(iso_week, clean), encoding="utf-8")
    print_ok(f"wrote {digest_path.relative_to(root)}")

    if fz_week_idx is None:
        print_warn(
            "captures missing week_idx in frontmatter; "
            "skipping fz-ax-backup.json update"
        )
    else:
        fz_path = root / "fz-ax-backup.json"
        dob_fallback = args.dob or os.environ.get("DOB")
        wrote = update_fz_backup(
            fz_path,
            fz_week_idx=int(fz_week_idx),
            mark=clean["mark"],
            whisper=clean["whisper"],
            marked_at=_now_iso(),
            dob=dob_fallback,
            week_start=os.environ.get("WEEK_START", "mon"),
        )
        if wrote:
            print_ok(f"updated {fz_path.relative_to(root)}")
        else:
            print_warn(
                "fz-ax-backup.json doesn't exist yet and no --dob/$DOB provided; "
                "skipping (pass --dob YYYY-MM-DD to bootstrap)"
            )

    # Optional git push
    if args.push:
        if not git_available(root):
            print_warn("--push specified but this isn't a git repo; skipping")
        else:
            paths_to_stage = [str(digest_path.relative_to(root))]
            fz_path = root / "fz-ax-backup.json"
            if fz_path.exists():
                paths_to_stage.append(str(fz_path.relative_to(root)))
            with console.status("[dim]committing and pushing...[/dim]", spinner="dots"):
                ok, out = git_commit_and_push(
                    root,
                    paths=paths_to_stage,
                    message=f"digest: {iso_week}  {clean['mark']}",
                )
            if ok:
                print_ok("pushed to origin")
            else:
                print_warn(f"push failed: {out}")

    console.print()
    console.print("  [bold green]done.[/bold green] [dim]open[/dim] fz.ax [dim]to see the week.[/dim]")
    console.print()
    return 0


# ============================================================================
# Main
# ============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="weekly_digest",
        description=(
            "Generate the weekly anthology digest for your `to` captures repo. "
            "Run from your captures repo root. Reads YYYY-wNN/*.md, writes "
            "YYYY-wNN/digest.md + fz-ax-backup.json."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  weekly_digest                               # most recent week, interactive\n"
            "  weekly_digest --week 2026-w17               # specific week\n"
            "  weekly_digest --list                        # available weeks\n"
            "  weekly_digest --dry-run                     # preview without writing\n"
            "  weekly_digest --yes --push                  # full automation\n"
            "  weekly_digest --model claude-sonnet-4-6     # cheaper for testing\n"
        ),
    )
    parser.add_argument("--week", help="YYYY-wNN; defaults to the most recent week")
    parser.add_argument("--list", action="store_true", help="list weeks with digest status")
    parser.add_argument("--dry-run", action="store_true", help="preview, write nothing")
    parser.add_argument("--yes", "-y", action="store_true", help="skip the confirm prompt")
    parser.add_argument("--push", action="store_true",
                         help="git pull before, git add+commit+push after (full automation)")
    parser.add_argument("--model", default="claude-opus-4-7",
                         help="Anthropic model (default: claude-opus-4-7)")
    parser.add_argument("--dob", help="YYYY-MM-DD — only needed if fz-ax-backup.json doesn't exist yet")
    parser.add_argument("--root", type=Path, default=Path.cwd(),
                         help="captures repo root (default: current directory)")
    args = parser.parse_args()

    if args.list:
        return cmd_list(args.root)
    return cmd_generate(args.root, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        err_console.print("\n  [yellow]interrupted.[/yellow]")
        sys.exit(130)
