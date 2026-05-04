---
name: pr-review
description: Review a pull request with confidence-filtered feedback. Use when the user says "review this PR", "code review", "review the diff", "look at PR #N", or invokes /pr-review. Reports only high-confidence issues; medium-confidence findings become questions; nits are dropped unless the user opts in.
---

# pr-review

Review a pull request the way a careful senior engineer would: read enough context to be sure, then surface only what's worth surfacing.

The defining principle is **confidence filtering**. A comment that turns out to be wrong costs the author more time than a comment you skipped costs you. When in doubt, ask instead of asserting.

## Step 1: Identify the PR

Use the first available source:

1. PR number/URL in the user's message → `gh pr view <number>` or `gh pr view <url>`
2. Current branch has an open PR → `gh pr view`
3. Otherwise ask the user for the PR identifier

Capture: title, description, base branch, head branch, author, list of changed files.

## Step 2: Read the Whole Diff

```bash
gh pr diff <number>
```

Don't stop at the diff. For each non-trivial changed file, read the full file (`git show {head}:path`) so you understand the change in context — the diff alone hides invariants and surrounding callers.

For bigger PRs (>500 lines), prioritize:
- New or significantly rewritten functions
- Public API changes (exported symbols, route handlers, schema migrations)
- Configuration or security-relevant code
- Anything in a critical path (auth, payments, data integrity)

## Step 3: Build a Findings List with Confidence

For each potential issue, classify into one of three buckets:

### HIGH confidence (will report as a review comment)
You can point at the exact bug. Examples:
- Off-by-one or wrong-operator (`<` vs `<=`)
- Null deref / unhandled None
- Resource leak (unclosed file/connection)
- Security: SQL injection, command injection, missing authz check
- Broken contract (function returns the wrong type/shape)
- Race condition (mutable shared state without lock/atomic)
- Test missing for a code path you can identify
- Backwards-incompatible API change with no migration noted

### MEDIUM confidence (will report as a question)
You suspect something but can't be sure without more context. Examples:
- "What happens when X is empty here?"
- "Is the caller expected to retry on this error?"
- "This loop runs N times — is N bounded by user input?"

### LOW confidence (drop unless the user asks for nits)
Style preferences, naming bikesheds, "I'd write this differently." Skip unless the user explicitly says "include nits".

## Step 4: Verdict

Pick one:

- **Approve** — no HIGH-confidence issues; MEDIUM questions are optional and not blocking.
- **Comment** — at least one MEDIUM question worth answering before merge, but no blocking bugs.
- **Request changes** — at least one HIGH-confidence issue that needs to be fixed.

## Step 5: Format the Review

Output a single block the user can paste into the PR review form (or you can submit via `gh pr review`).

```
## Review of #<number>: <title>

**Verdict:** <approve | comment | request changes>

**Summary:** <one paragraph: what the PR does + your overall read>

### Issues
- `path/to/file.go:42` — <description of the HIGH-confidence issue and the suggested fix>
- `path/to/other.ts:118` — <description>

### Questions
- `path/to/file.go:60` — <MEDIUM-confidence question>

### Notes
- <any architectural / forward-looking observation worth mentioning, max 3 bullets>
```

Omit any section with zero items. If verdict is **approve** with no issues or questions, just write the summary plus a one-line congratulation.

## Step 6: Submit (Optional)

If the user says "submit it" / "post the review":

```bash
gh pr review <number> --<approve|comment|request-changes> --body-file <tmpfile>
```

Otherwise leave the formatted review in the conversation for them to paste manually.

## Hard rules

- Never invent line numbers; cite the actual lines you read.
- Never approve a PR you haven't actually read end-to-end.
- If you can't tell whether the change is correct, that's a question (MEDIUM), not a comment (HIGH).
- Never say "looks good to me" without naming what's good — empty approvals are worse than no review.
