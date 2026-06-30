# Code-Churn (Fix-Attribution) Analysis

Measures, per developer, **how much of the code they wrote later had to be
"fixed" by bug-fix commits** — a proxy for the defect-proneness of each
author's contributions.

It implements a practical variant of the **SZZ algorithm**
(Śliwerski–Zimmermann–Zeller): for each bug-fix commit, the lines it removes or
modifies are traced back through `git blame` to the commit that originally
wrote them, and that author is charged with one unit of *fix-induced churn*.

The hard part — separating genuine bug fixes from feature work, requirements
changes, refactors, and style-bot commits — is done by an LLM
(`bug_fix` vs `feature` / `refactor` / `docs` / `style` / `test` / `chore` /
`revert`). A commit that merely says "fix" (e.g. *"Fix release date in
README"*, *"Apply fixes from StyleCI"*) is **not** counted as a bug fix.

## Pipeline

Three independent stages exchange JSON, so the deterministic git work is
testable in isolation from the (LLM) classification:

```
git repo ──[1 extract]──▶ commits.json ──[2 classify]──▶ classifications.json ──[3 attribute]──▶ report.md / report.json
            (python)                       (Claude / --api)                     (python, SZZ blame)
```

The easiest way to run all three is the **`/code-churn` skill**
(`.claude/skills/code-churn/`), which runs `extract`, classifies the commits
itself, then runs `attribute`. The stages below are for running it by hand or
in CI.

## Usage

```bash
# 1. Extract candidate commits (non-merge, source-touching, non-bot).
python3 tools/code-churn/code_churn.py extract \
    --repo /path/to/repo --include 'src/**' --out commits.json

# 2. Classify. Either let the Claude skill label commits.json, or:
export ANTHROPIC_API_KEY=...
python3 tools/code-churn/code_churn.py classify \
    --commits commits.json --api --out classifications.json

# 3. Attribute and report.
python3 tools/code-churn/code_churn.py attribute \
    --repo /path/to/repo --commits commits.json \
    --classifications classifications.json \
    --out-md report.md --out-json report.json
```

`classifications.json` is `{"classifications": [{"hash","label","confidence",
"rationale"}, ...]}` — easy to hand-author or post-process.

### Key options

| Stage | Option | Purpose |
|---|---|---|
| extract | `--include` / `--exclude` | source-scope globs (repeatable). Default excludes docs/tests/config/CI. |
| extract | `--bot` | extra bot-author substrings (StyleCI/dependabot/etc. are excluded by default). |
| extract | `--identity-map FILE` | JSON `{email: "Name <email>"}` to merge multiple emails into one developer. |
| extract | `--added-context N` | parent context lines to blame for pure-insertion fixes (default 1; classic SZZ uses deleted lines only). |
| extract | `--rev-range` | e.g. `v1.0..HEAD`. |
| attribute | `--fix-labels` | which labels count as fixes (default `bug_fix revert`). |

Identity merging also honours a repo `.mailmap` (via `git --use-mailmap`); the
script ships a default map merging this repo's two Anthony Tibbs emails.

## The metric

- **Fix rate** (headline) = `fix_churn_lines ÷ lines_authored`. Normalizing by
  lines authored keeps prolific authors from being ranked worst by raw volume.
- **fix_churn_lines** — suspect lines blamed to the author.
- **Self-fixed vs. fixed-by-others** — whether the author fixed their own code.
- **lines_authored** — source lines the author added over history (denominator).
- **File hotspots** — files accumulating the most fix churn.

## Caveats — read before drawing conclusions

- **SZZ is heuristic.** The line a fix touches isn't always the true root
  cause; refactors and a dominant initial-import commit can skew blame.
- **`git blame -w -M -C`** lets blame see through whitespace/style reformatting
  and moved code, but it is not perfect.
- **Small denominators are noisy.** An author with very few surviving authored
  lines can show an extreme fix rate from a single fixed line — read the
  absolute counts alongside the rate.
- **Classification quality bounds everything.** Low-confidence labels are
  surfaced in the report for review, not trusted silently.
- **This is a code-health signal, not a developer performance ranking.** A high
  fix rate often means an author owns the hardest, most-changed parts of the
  system.

## Tests

```bash
python3 tools/code-churn/test_code_churn.py
```

Builds a throwaway git repo (author A introduces a buggy line, a StyleCI bot
reformats it, author B later fixes it, plus a docs-only "fix") and asserts the
churn is attributed to A through the bot reformat, and that bot/docs commits are
excluded. No network or LLM required.
