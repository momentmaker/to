# pr-review

Reviews a pull request with confidence-filtered feedback. Reports only high-confidence issues as review comments, treats medium-confidence findings as questions, and drops style nits unless the user explicitly asks for them. Outputs a paste-ready review block and can submit via `gh` on request.

Install:
```bash
jutsu install pr-review
```

Trigger phrases: "review this PR", "code review", "review the diff", "look at PR #N", `/pr-review`.
