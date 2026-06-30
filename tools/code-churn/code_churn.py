#!/usr/bin/env python3
"""code_churn.py — developer code-churn / fix-attribution analysis (SZZ).

Measures, per developer, how much of the code they authored later had to be
"fixed" by bug-fix commits. Implements a practical variant of the SZZ
algorithm (Sliwerski-Zimmermann-Zeller): for each bug-fix commit, the lines it
removes/modifies are traced back via `git blame` to the commit that originally
wrote them, and that author is charged with one unit of fix-induced churn.

The pipeline is three independent stages that exchange JSON, so the
deterministic git work is testable in isolation from the (LLM) classification:

    extract   git history          -> commits.json
    classify  commits.json         -> classifications.json   (LLM; or --api)
    attribute commits + labels     -> report.md / report.json (SZZ blame)

stdlib only — no third-party dependencies.
"""

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from collections import defaultdict

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# Files that never count as "code" for churn purposes. Overridable with
# --exclude / --include on the command line.
DEFAULT_EXCLUDE = [
    "tests/**", "test/**", "spec/**",
    "*.md", "*.txt", "*.rst",
    "docs/**", "doc/**",
    "config/**",
    "*.yml", "*.yaml", "*.json", "*.lock", "*.dist", "*.xml",
    "*.cfg", "*.ini", "*.toml",
    "LICENSE*", "CHANGELOG*", "README*",
    ".*", "**/.*",
]

# Authors that are bots, not developers. Matched (case-insensitive substring)
# against "name <email>". Overridable with --bot.
DEFAULT_BOTS = [
    "styleci", "dependabot", "github-actions", "renovate",
    "[bot]", "snyk-bot", "scrutinizer",
]

# Default identity map: email (lower) -> canonical "Name <email>".
# Ships with the merge this example repo needs; users add their own via
# --identity-map <file.json> or a .mailmap in the repo.
DEFAULT_IDENTITY_MAP = {
    "anthony@trinimex.ca": "Anthony Tibbs <anthony@tibbs.ca>",
    "anthony@tibbs.ca": "Anthony Tibbs <anthony@tibbs.ca>",
}

# Commit classification labels that count as a "fix" feeding attribution.
DEFAULT_FIX_LABELS = ["bug_fix", "revert"]

CLASSIFY_LABELS = [
    "bug_fix", "feature", "refactor", "docs", "style", "test", "chore", "revert",
]

# git format markers (unit/record separators are not valid in commit metadata).
_US = "\x1f"
_FMT = (
    f"{_US}H%H{_US}P%P{_US}AN%an{_US}AE%ae{_US}AD%ad"
    f"{_US}S%s{_US}B%b{_US}END"
)
_HEADER_RE = re.compile(
    _US + r"H(?P<H>.*?)" + _US + r"P(?P<P>.*?)" + _US + r"AN(?P<AN>.*?)"
    + _US + r"AE(?P<AE>.*?)" + _US + r"AD(?P<AD>.*?)" + _US + r"S(?P<S>.*?)"
    + _US + r"B(?P<B>.*?)" + _US + r"END",
    re.S,
)
_ISSUE_RE = re.compile(r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)?\s*#(\d+)", re.I)


# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------

def git(repo, *args, check=True):
    """Run a git command in `repo` and return stdout (text)."""
    proc = subprocess.run(
        ["git", "-C", repo, *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace",
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args[:3])}... failed ({proc.returncode}): "
            f"{proc.stderr.strip()}"
        )
    return proc.stdout


def path_matches(path, pattern):
    """Glob match that treats '**' like '*' (fnmatch '*' already crosses '/')."""
    return fnmatch.fnmatch(path, pattern.replace("**", "*"))


def is_source(path, includes, excludes):
    if any(path_matches(path, p) for p in excludes):
        return False
    if includes:
        return any(path_matches(path, p) for p in includes)
    return True


def is_bot(name, email, bots):
    who = f"{name} <{email}>".lower()
    return any(b.lower() in who for b in bots)


# ---------------------------------------------------------------------------
# Identity merging
# ---------------------------------------------------------------------------

class Identities:
    """Maps (name, email) -> a stable canonical "Name <email>" string."""

    def __init__(self, mapping):
        self.map = {k.lower(): v for k, v in mapping.items()}

    def canon(self, name, email):
        email = (email or "").strip().lower()
        if email in self.map:
            return self.map[email]
        name = (name or "").strip()
        return f"{name} <{email}>" if email else (name or "unknown")


# ---------------------------------------------------------------------------
# Diff parsing: find the parent-side "suspect" lines a fix touches
# ---------------------------------------------------------------------------

_DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_diff(diff_text, added_context=1):
    """Parse a unified diff into per-file change records.

    Returns list of dicts: {a_path, b_path, added, deleted, suspect_lines}.
    `suspect_lines` are line numbers in the PARENT file that the commit removed
    or modified (classic SZZ). For a hunk that is pure insertion (no deletions),
    the `added_context` parent line(s) immediately preceding the insertion are
    used as a fallback anchor, so guard-clause-style fixes are still attributed.
    """
    files = []
    cur = None
    a_path = b_path = None
    parent_ln = 0
    hunk_dels = 0
    hunk_adds = 0
    hunk_anchor = None  # parent line just before first addition in this hunk

    def close_file():
        nonlocal cur
        if cur is not None:
            files.append(cur)
        cur = None

    for line in diff_text.splitlines():
        m = _DIFF_HEADER.match(line)
        if m:
            close_file()
            a_path, b_path = m.group(1), m.group(2)
            cur = {"a_path": a_path, "b_path": b_path,
                   "added": 0, "deleted": 0, "suspect_lines": []}
            continue
        if cur is None:
            continue
        if line.startswith("---") or line.startswith("+++"):
            # track /dev/null so we can tell adds/deletes of whole files
            if line.startswith("--- ") and line[4:].strip() == "/dev/null":
                cur["a_path"] = None
            continue
        hm = _HUNK.match(line)
        if hm:
            parent_ln = int(hm.group(1))
            hunk_dels = hunk_adds = 0
            hunk_anchor = parent_ln - 1 if parent_ln > 0 else None
            continue
        if not line:
            continue
        tag = line[0]
        if tag == " ":
            hunk_anchor = parent_ln
            parent_ln += 1
        elif tag == "-":
            cur["deleted"] += 1
            cur["suspect_lines"].append(parent_ln)
            hunk_dels += 1
            parent_ln += 1
        elif tag == "+":
            cur["added"] += 1
            if hunk_adds == 0 and hunk_dels == 0 and added_context and hunk_anchor:
                # pure-insertion so far: anchor on the preceding parent line(s)
                for off in range(added_context):
                    ln = hunk_anchor - off
                    if ln >= 1:
                        cur["suspect_lines"].append(ln)
            hunk_adds += 1
        # '\' (no newline marker) and anything else: ignore

    close_file()
    # de-dup suspect lines per file
    for f in files:
        f["suspect_lines"] = sorted(set(f["suspect_lines"]))
    return files


# ---------------------------------------------------------------------------
# Stage 1: extract
# ---------------------------------------------------------------------------

def cmd_extract(args):
    repo = args.repo
    includes = args.include or []
    excludes = args.exclude if args.exclude else DEFAULT_EXCLUDE
    bots = (args.bot or []) + DEFAULT_BOTS
    ids = load_identities(args.identity_map)

    rev_range = args.rev_range or "HEAD"
    hashes = git(repo, "log", "--no-merges", "--use-mailmap",
                 "--format=%H", rev_range).split()
    commits = []
    skipped_bot = skipped_nosrc = 0

    for sha in hashes:
        out = git(repo, "show", "--no-color", "-U3", "-M", "-C",
                  "--date=iso-strict", f"--format={_FMT}", sha)
        m = _HEADER_RE.search(out)
        if not m:
            continue
        name, email = m["AN"], m["AE"]
        if is_bot(name, email, bots):
            skipped_bot += 1
            continue
        diff_text = out[m.end():]
        all_files = parse_diff(diff_text, added_context=args.added_context)
        src_files = [
            f for f in all_files
            if is_source(f["b_path"] or f["a_path"] or "", includes, excludes)
        ]
        if not src_files:
            skipped_nosrc += 1
            continue

        parents = m["P"].split()
        subject, body = m["S"], m["B"].strip()
        issues = sorted({int(n) for n in _ISSUE_RE.findall(subject + "\n" + body)})

        commits.append({
            "hash": sha,
            "parent": parents[0] if parents else None,
            "author_name": name,
            "author_email": email,
            "author": ids.canon(name, email),
            "date": m["AD"],
            "subject": subject,
            "body": body,
            "issues": issues,
            "files": [
                {"a_path": f["a_path"], "b_path": f["b_path"],
                 "added": f["added"], "deleted": f["deleted"],
                 "suspect_lines": f["suspect_lines"]}
                for f in src_files
            ],
            # truncated human-readable diff for the LLM classifier
            "diff_text": _source_diff_text(diff_text, src_files,
                                           args.max_diff_chars),
        })

    payload = {
        "repo": os.path.abspath(repo),
        "rev_range": rev_range,
        "config": {"include": includes, "exclude": excludes,
                   "added_context": args.added_context},
        "commits": commits,
    }
    _write_json(args.out, payload)
    print(f"extract: {len(commits)} candidate commits "
          f"({skipped_bot} bot, {skipped_nosrc} non-source skipped) -> {args.out}",
          file=sys.stderr)


def _source_diff_text(diff_text, src_files, limit):
    """Keep only the diff blocks for source files, truncated to `limit` chars."""
    keep_paths = {f["b_path"] for f in src_files} | {f["a_path"] for f in src_files}
    blocks, cur, cur_path = [], [], None
    for line in diff_text.splitlines():
        m = _DIFF_HEADER.match(line)
        if m:
            if cur and cur_path in keep_paths:
                blocks.append("\n".join(cur))
            cur, cur_path = [line], m.group(2)
        else:
            cur.append(line)
    if cur and cur_path in keep_paths:
        blocks.append("\n".join(cur))
    text = "\n".join(blocks)
    if limit and len(text) > limit:
        text = text[:limit] + "\n... [diff truncated] ..."
    return text


# ---------------------------------------------------------------------------
# Stage 3: attribute (SZZ blame)
# ---------------------------------------------------------------------------

_BLAME_HDR = re.compile(r"^([0-9a-f]{40}) \d+ (\d+)(?: \d+)?$")


def blame_map(repo, rev, path, ids, cache):
    """Return {line_no_in_rev: (sha, canonical_author)} for `path` at `rev`."""
    key = (rev, path)
    if key in cache:
        return cache[key]
    out = git(repo, "blame", "-w", "-M", "-C", "--line-porcelain",
              rev, "--", path, check=False)
    result = {}
    sha = None
    final_ln = None
    name = email = None
    for line in out.splitlines():
        hm = _BLAME_HDR.match(line)
        if hm:
            sha, final_ln = hm.group(1), int(hm.group(2))
            name = email = None
            continue
        if line.startswith("author "):
            name = line[len("author "):]
        elif line.startswith("author-mail "):
            email = line[len("author-mail "):].strip().strip("<>")
        elif line.startswith("\t"):
            if sha is not None and final_ln is not None:
                result[final_ln] = (sha, ids.canon(name, email))
            sha = final_ln = None
    cache[key] = result
    return result


def cmd_attribute(args):
    repo = args.repo
    ids = load_identities(args.identity_map)
    fix_labels = set(args.fix_labels or DEFAULT_FIX_LABELS)

    data = _read_json(args.commits)
    commits = data["commits"]
    labels = {}
    if args.classifications:
        cls = _read_json(args.classifications)
        labels = {c["hash"]: c for c in cls.get("classifications", cls)} \
            if isinstance(cls, (list, dict)) else {}
        if isinstance(cls, dict) and "classifications" in cls:
            labels = {c["hash"]: c for c in cls["classifications"]}
        elif isinstance(cls, list):
            labels = {c["hash"]: c for c in cls}

    # Denominator: source lines each author has ever added.
    authored = defaultdict(int)
    for c in commits:
        add = sum(f["added"] for f in c["files"])
        authored[c["author"]] += add

    churn = defaultdict(int)            # author -> suspect lines blamed to them
    churn_self = defaultdict(int)       # subset where introducer == fixer
    fixes_hit = defaultdict(set)        # author -> {fix commit hashes}
    hotspots = defaultdict(int)         # file -> suspect lines
    fix_commits = []
    blame_cache = {}

    for c in commits:
        label_rec = labels.get(c["hash"])
        label = label_rec["label"] if label_rec else None
        if label not in fix_labels:
            continue
        parent = c["parent"]
        fix_author = c["author"]
        per_fix_authors = set()
        attributed = 0
        if parent:
            for f in c["files"]:
                if not f["suspect_lines"]:
                    continue
                path = f["a_path"] or f["b_path"]
                bmap = blame_map(repo, parent, path, ids, blame_cache)
                for ln in f["suspect_lines"]:
                    hit = bmap.get(ln)
                    if not hit:
                        continue
                    intro_author = hit[1]
                    churn[intro_author] += 1
                    if intro_author == fix_author:
                        churn_self[intro_author] += 1
                    fixes_hit[intro_author].add(c["hash"])
                    hotspots[f["b_path"] or path] += 1
                    per_fix_authors.add(intro_author)
                    attributed += 1
        fix_commits.append({
            "hash": c["hash"], "subject": c["subject"], "author": fix_author,
            "date": c["date"], "label": label,
            "confidence": (label_rec or {}).get("confidence"),
            "rationale": (label_rec or {}).get("rationale"),
            "attributed_lines": attributed,
            "introduced_by": sorted(per_fix_authors),
        })

    # Assemble per-developer rows.
    devs = []
    for author in sorted(set(list(authored) + list(churn))):
        a = authored[author]
        ch = churn[author]
        devs.append({
            "author": author,
            "lines_authored": a,
            "fix_churn_lines": ch,
            "fix_churn_self": churn_self[author],
            "fix_churn_other": ch - churn_self[author],
            "fixes_touching_their_code": len(fixes_hit[author]),
            "fix_rate": (ch / a) if a else 0.0,
        })
    devs.sort(key=lambda d: (d["fix_rate"], d["fix_churn_lines"]), reverse=True)

    report = {
        "repo": data.get("repo"),
        "totals": {
            "commits_analyzed": len(commits),
            "fix_commits": len(fix_commits),
            "classified": bool(labels),
        },
        "developers": devs,
        "fix_commits": sorted(fix_commits, key=lambda x: x["date"]),
        "hotspots": sorted(
            ({"file": f, "fix_churn_lines": n} for f, n in hotspots.items()),
            key=lambda x: x["fix_churn_lines"], reverse=True),
    }
    _write_json(args.out_json, report)
    _write_markdown(args.out_md, report, classified=bool(labels))
    print(f"attribute: {len(fix_commits)} fix commits, "
          f"{len(devs)} developers -> {args.out_md}", file=sys.stderr)


def _write_markdown(path, report, classified):
    L = []
    L.append("# Developer Code-Churn Report\n")
    if report.get("repo"):
        L.append(f"_Repository:_ `{report['repo']}`\n")
    t = report["totals"]
    if not classified:
        L.append("> **Warning:** no classifications supplied — fix metrics are "
                 "empty. Run the classify stage first.\n")
    L.append(f"- Commits analyzed (non-merge, source-touching, non-bot): "
             f"**{t['commits_analyzed']}**")
    L.append(f"- Bug-fix commits: **{t['fix_commits']}**\n")

    L.append("## Developers — ranked by fix rate\n")
    L.append("Fix rate = fix-induced churn lines ÷ source lines authored. "
             "A higher rate means more of that author's code was later "
             "modified by bug fixes. **This is a code-health signal, not a "
             "performance ranking** (see caveats).\n")
    L.append("| Developer | Lines authored | Fix-churn lines | Fix rate | "
             "Fixes hitting their code | Self-fixed | Fixed by others |")
    L.append("|---|--:|--:|--:|--:|--:|--:|")
    for d in report["developers"]:
        L.append(
            f"| {d['author']} | {d['lines_authored']} | {d['fix_churn_lines']} "
            f"| {d['fix_rate'] * 100:.1f}% | {d['fixes_touching_their_code']} "
            f"| {d['fix_churn_self']} | {d['fix_churn_other']} |")
    L.append("")

    if report["hotspots"]:
        L.append("## File hotspots\n")
        L.append("| File | Fix-churn lines |")
        L.append("|---|--:|")
        for h in report["hotspots"][:15]:
            L.append(f"| `{h['file']}` | {h['fix_churn_lines']} |")
        L.append("")

    L.append("## Bug-fix commits (auditable)\n")
    L.append("| Date | Commit | Author | Lines attributed | Introduced by | Rationale |")
    L.append("|---|---|---|--:|---|---|")
    for fc in report["fix_commits"]:
        intro = ", ".join(a.split(" <")[0] for a in fc["introduced_by"]) or "—"
        rationale = (fc.get("rationale") or "").replace("|", "\\|")
        L.append(
            f"| {fc['date'][:10]} | `{fc['hash'][:8]}` {fc['subject'][:60]} "
            f"| {fc['author'].split(' <')[0]} | {fc['attributed_lines']} "
            f"| {intro} | {rationale} |")
    L.append("")

    L.append("## Methodology & caveats\n")
    L.append(
        "- **SZZ heuristic.** For each bug-fix commit, the lines it removes or "
        "modifies are blamed against the parent commit to find who wrote them. "
        "The line a fix touches is not always the true root cause.\n"
        "- **Blame robustness.** Blame uses `-w -M -C` so whitespace/style "
        "reformatting and moved code don't misattribute authorship.\n"
        "- **Denominator** is source lines each author added over history; code "
        "that was authored and later fully deleted is under-counted.\n"
        "- **Classification quality bounds everything.** Low-confidence labels "
        "are listed above for review rather than trusted silently.\n"
        "- **Not a performance ranking.** A high fix rate can mean an author "
        "owns the hardest, most-changed parts of the system.\n")

    with open(path, "w") as fh:
        fh.write("\n".join(L))


# ---------------------------------------------------------------------------
# Stage 2 (optional): classify via the Claude API
# ---------------------------------------------------------------------------

def cmd_classify(args):
    """Optional standalone classifier. The primary path is the Claude skill,
    which classifies commits.json directly. This calls the Claude API for
    CI/headless use."""
    import ssl
    import urllib.request

    data = _read_json(args.commits)
    commits = data["commits"]
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("classify --api requires ANTHROPIC_API_KEY in the environment.")

    ca = "/root/.ccr/ca-bundle.crt"
    ctx = ssl.create_default_context(cafile=ca) if os.path.exists(ca) \
        else ssl.create_default_context()

    results = []
    batch = args.batch
    for i in range(0, len(commits), batch):
        chunk = commits[i:i + batch]
        results.extend(_classify_chunk(chunk, api_key, ctx, args.model))
        print(f"classify: {min(i + batch, len(commits))}/{len(commits)}",
              file=sys.stderr)

    _write_json(args.out, {"model": args.model, "classifications": results})
    print(f"classify: wrote {len(results)} labels -> {args.out}", file=sys.stderr)


def _classify_chunk(chunk, api_key, ctx, model):
    import urllib.request

    items = [
        {"hash": c["hash"], "subject": c["subject"],
         "body": c["body"][:500], "issues": c["issues"],
         "files": [f["b_path"] for f in c["files"]],
         "diff": c["diff_text"][:4000]}
        for c in chunk
    ]
    prompt = (
        "You are classifying git commits for a code-churn analysis. For EACH "
        "commit, decide its primary intent. Use exactly one label from: "
        f"{', '.join(CLASSIFY_LABELS)}.\n"
        "`bug_fix` = corrects incorrect/broken behavior in existing code. "
        "A new feature, a requirements change, a refactor, a docs/style/test/"
        "chore change is NOT a bug_fix even if the message says 'fix'.\n"
        "Return ONLY a JSON array, one object per commit, in order: "
        '{"hash","label","confidence":0..1,"rationale":"<=12 words"}.\n\n'
        + json.dumps(items, indent=1)
    )
    body = json.dumps({
        "model": model,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, context=ctx) as resp:
        payload = json.load(resp)
    text = "".join(b.get("text", "") for b in payload.get("content", []))
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    return json.loads(text)


# ---------------------------------------------------------------------------
# shared IO
# ---------------------------------------------------------------------------

def load_identities(path):
    mapping = dict(DEFAULT_IDENTITY_MAP)
    if path:
        mapping.update(_read_json(path))
    return Identities(mapping)


def _read_json(path):
    with open(path) as fh:
        return json.load(fh)


def _write_json(path, obj):
    with open(path, "w") as fh:
        json.dump(obj, fh, indent=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("extract", help="walk git history -> commits.json")
    e.add_argument("--repo", default=".")
    e.add_argument("--out", default="commits.json")
    e.add_argument("--rev-range", default=None,
                   help="e.g. HEAD, v1.0..HEAD (default: HEAD)")
    e.add_argument("--include", action="append",
                   help="source glob to include (repeatable; default: all)")
    e.add_argument("--exclude", action="append",
                   help="glob to exclude (repeatable; default: docs/tests/config)")
    e.add_argument("--bot", action="append", help="extra bot author substring")
    e.add_argument("--identity-map", help="JSON {email: 'Name <email>'} overrides")
    e.add_argument("--added-context", type=int, default=1,
                   help="parent context lines to blame for pure-insertion fixes")
    e.add_argument("--max-diff-chars", type=int, default=6000)
    e.set_defaults(func=cmd_extract)

    c = sub.add_parser("classify", help="(optional) label commits via Claude API")
    c.add_argument("--commits", default="commits.json")
    c.add_argument("--out", default="classifications.json")
    c.add_argument("--api", action="store_true", help="use the Claude API")
    c.add_argument("--model", default="claude-haiku-4-5-20251001")
    c.add_argument("--batch", type=int, default=20)
    c.set_defaults(func=cmd_classify)

    a = sub.add_parser("attribute", help="SZZ blame attribution -> report")
    a.add_argument("--repo", default=".")
    a.add_argument("--commits", default="commits.json")
    a.add_argument("--classifications", default="classifications.json")
    a.add_argument("--out-md", default="report.md")
    a.add_argument("--out-json", default="report.json")
    a.add_argument("--identity-map")
    a.add_argument("--fix-labels", nargs="+", default=None,
                   help=f"labels treated as fixes (default: {DEFAULT_FIX_LABELS})")
    a.set_defaults(func=cmd_attribute)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
