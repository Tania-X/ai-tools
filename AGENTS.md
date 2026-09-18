# ai-tools Agent Instructions

These rules apply to any coding agent working in this repository.

This repo is pushed to `main` directly (project owner's decision — no PR, no CI review).
That makes the pre-commit self-check the **only** review a change ever gets, so the rules
below are gates, not advice.

## LLM Budget Rules

- Every budget, timeout or cap must have provenance: **derived from the requirement and
  written down, or configurable.** A bare round number is a bug that ships silently.
- When a prompt or rubric gains a requirement, re-check its output budget in the same change.
  Requirements grow; frozen budgets turn into failures months later.
- Fix the cause, not only the symptom. If a diagnosis names a root cause (e.g. "truncated by
  `max_tokens`"), the same change must fix that cause. A salvage/fallback path alone is not a fix.
- After fixing one instance of a class of bug, grep the whole repo for the same class.
- LLM failures must be self-diagnosing: keep `finish_reason` and usage, and never let a
  diagnostic archive be cut shorter than the failure it is meant to explain.
- Distinguish "tool failure" (unparseable output, no score) from "quality verdict" (a real low
  score). Never score a tool failure as 0 — it reads as a real judgement.

### Few-shots (why those rules exist)

| Incident | Lesson |
|---|---|
| `max_tokens=400` hardcoded for the judge (2026-08-12) while its prompt asked for "one reason per issue" | A number with no provenance lives for weeks, then returns as an outage |
| `REPLY_MAX_TOKENS=300` was derived from "≤3 sentences"; the judge's 400 was copied from that shape | Copying a budget across paths only works if the requirement is the same |
| The same truncation was fixed for review output (1024 → `review_max_tokens=4096`) but not for the judge | After fixing one `max_tokens`, grep every `max_tokens` |
| Judge rubric grew 390 → 1077 chars over 5 weeks; budget stayed 400 → failures went 2/15 rounds → 2/4 rounds → both attempts | Re-check the budget whenever the prompt grows |
| The truncation cause was written into a comment, but only salvage was implemented | Knowing the cause is not fixing it |
| Truncated judge output scored 0 → 3 rewrite rounds (4x cost) for 32 days | A tool failure scored as a real verdict burns money and misleads |
| Diagnostic archive capped at `raw[:500]` → both failed attempts exactly 500 chars | A cap shorter than the failure destroys the evidence |
| The whole quality gate landed via a local merge, no PR, no self-review | Self-check before commit is the only gate here |

## Repo Facts

- Python 3.12, `pytest` only (no linter/type-check gate). Run `.venv/bin/python -m pytest -q`.
- Layout: `gateway/` (LLM client), `pr-review/` (`pr_review` package), `golden-tests/`,
  `ci-diagnose/`, `ai_tools_cli/`.
- Tests live next to each package (`gateway/tests`, `pr-review/tests`, ...) and use fakes —
  no real network or LLM calls.
- Comments and docs are in Chinese; keep that.
- Break each new guard once and confirm the test goes red (mutation check) — a test that
  passes with the guard removed is not a test.

## Docs

- PR review design and incident log: `docs/pr-review-quality-gate.md`
- Review engine overview: `docs/pr-review.md`, `docs/ai-review-architecture.md`
