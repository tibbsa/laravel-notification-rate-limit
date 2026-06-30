---
name: code-churn
description: >-
  Analyze developer code churn — how much of each developer's code later had to
  be fixed by bug-fix commits (a defect-proneness signal). Use when the user
  asks to measure code churn, fix attribution, which developers' code gets
  fixed most, bug-introduction by author, or to run an SZZ-style analysis on a
  git repository.
---

# Code-Churn (Fix-Attribution) Analysis

Drives `tools/code-churn/code_churn.py` to produce a per-developer report of
**fix-induced churn**: for each genuine bug-fix commit, the lines it changes
are blamed back to whoever originally wrote them (SZZ algorithm), and that
author is charged with the churn. Your job in this skill is the **classification
step** — deciding which commits are real bug fixes.

The script path is relative to this repo. For another repo, pass its path with
`--repo` (the script and skill are portable; copy this skill to
`~/.claude/skills/` to use it globally).

## Steps

### 1. Extract
Run, choosing source globs for the target repo (`src/**` for a PHP/JS package,
`src/**`/`app/**`/`lib/**` otherwise — confirm with the user if unsure):

```bash
python3 tools/code-churn/code_churn.py extract \
    --repo <REPO> --include '<SRC_GLOB>' --out /tmp/cc-commits.json
```

This already excludes merge commits, bot authors (StyleCI/dependabot/…), and
non-source files. If the user mentions developers who commit under multiple
emails, write a `{email: "Name <email>"}` map and pass `--identity-map`.

### 2. Classify (your job)
Read `/tmp/cc-commits.json`. For **every** commit, assign exactly one label:

`bug_fix | feature | refactor | docs | style | test | chore | revert`

Rules:
- `bug_fix` = corrects incorrect or broken behavior in **existing** code
  (wrong logic, crash, regression, off-by-one, missing guard, bad event order,
  compatibility/deprecation break).
- A new feature, a requirements/spec change, a refactor, a docs/style/test/
  chore change is **NOT** a `bug_fix` even when the message contains "fix".
  Examples that are not bug fixes: "Fix release date in README" (docs),
  "Apply fixes from StyleCI" / "StyleCi Fixes" (style), "Add … support"
  (feature).
- Read the `diff_text` for ambiguous commits (especially "WIP" / vague
  subjects) before deciding — don't classify on the subject alone.

Write `/tmp/cc-classifications.json`:

```json
{"classifications": [
  {"hash": "<full sha>", "label": "bug_fix", "confidence": 0.9,
   "rationale": "<=12 words why"}
]}
```

Include an entry for every commit; `confidence` is 0–1; keep `rationale` short
(it appears in the report for auditability). Only `bug_fix` and `revert` feed
attribution, but label everything so the report's counts are complete.

(Headless/CI alternative: `code_churn.py classify --api` calls the Claude API
with `ANTHROPIC_API_KEY` set. The interactive path above is preferred — you are
already a capable classifier and it needs no key.)

### 3. Attribute & report
```bash
python3 tools/code-churn/code_churn.py attribute \
    --repo <REPO> --commits /tmp/cc-commits.json \
    --classifications /tmp/cc-classifications.json \
    --out-md /tmp/cc-report.md --out-json /tmp/cc-report.json
```

### 4. Present
Summarize `report.md` for the user: the ranked fix-rate table, file hotspots,
and the auditable bug-fix list. **Always** restate the key caveats — small
denominators make fix rates noisy, and this is a code-health signal, not a
developer performance ranking.

## Verify
`python3 tools/code-churn/test_code_churn.py` runs a deterministic,
network-free self-test of the extract + blame-attribution logic.
