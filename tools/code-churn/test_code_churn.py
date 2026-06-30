#!/usr/bin/env python3
"""Deterministic tests for code_churn.py — no LLM, no network.

Builds a throwaway git repo in which author A introduces a buggy line that
author B later fixes, plus a docs-only "fix" and a bot commit that must be
excluded, then runs extract + attribute and asserts the attribution lands on A.

Run: python3 tools/code-churn/test_code_churn.py
"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import code_churn as cc  # noqa: E402


def run(repo, *args, author=None, date="2020-01-01T00:00:00"):
    env = dict(os.environ)
    if author:
        name, email = author
        env.update(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email,
                   GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email)
    env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    subprocess.run(["git", "-C", repo, *args], check=True, env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def write(repo, rel, content):
    full = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as fh:
        fh.write(content)


A = ("Alice Dev", "alice@example.com")
B = ("Bob Dev", "bob@example.com")
BOT = ("StyleCI Bot", "bot@styleci.io")


def build_repo(repo):
    run(repo, "init", "-q")
    run(repo, "config", "user.name", "x")
    run(repo, "config", "user.email", "x@x")

    # 1. Alice introduces code with a bug (rate < limit should be <=).
    write(repo, "src/calc.py",
          "def allowed(count, limit):\n"
          "    return count < limit\n")
    write(repo, "README.md", "# Demo\n")
    run(repo, "add", "-A")
    run(repo, "commit", "-qm", "Add rate calculation", author=A,
        date="2020-01-01T00:00:00")

    # 2. Bob adds an unrelated feature (not a fix).
    write(repo, "src/util.py", "def noop():\n    return None\n")
    run(repo, "add", "-A")
    run(repo, "commit", "-qm", "Add util helper", author=B,
        date="2020-02-01T00:00:00")

    # 3. Bot reformats whitespace only (must be excluded as a bot; blame -w
    #    must see through this to the real author Alice).
    write(repo, "src/calc.py",
          "def allowed(count, limit):\n"
          "        return count < limit\n")
    run(repo, "add", "-A")
    run(repo, "commit", "-qm", "Apply fixes from StyleCI", author=BOT,
        date="2020-03-01T00:00:00")

    # 4. Bob FIXES Alice's off-by-one bug (modifies the buggy line).
    write(repo, "src/calc.py",
          "def allowed(count, limit):\n"
          "        return count <= limit\n")
    run(repo, "add", "-A")
    run(repo, "commit", "-qm", "Fix off-by-one in rate check (#7)", author=B,
        date="2020-04-01T00:00:00")

    # 5. Docs-only "fix" (must be excluded by source scope).
    write(repo, "README.md", "# Demo\nFixed a typo.\n")
    run(repo, "add", "-A")
    run(repo, "commit", "-qm", "Fix typo in README", author=A,
        date="2020-05-01T00:00:00")


def test_parse_diff_suspect_lines():
    diff = (
        "diff --git a/src/calc.py b/src/calc.py\n"
        "--- a/src/calc.py\n"
        "+++ b/src/calc.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def allowed(count, limit):\n"
        "-    return count < limit\n"
        "+    return count <= limit\n"
    )
    files = cc.parse_diff(diff)
    assert len(files) == 1, files
    f = files[0]
    assert f["deleted"] == 1 and f["added"] == 1, f
    # the modified line is parent line 2
    assert f["suspect_lines"] == [2], f["suspect_lines"]
    print("ok  parse_diff identifies the modified parent line")


def test_end_to_end(tmp):
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo)
    build_repo(repo)

    commits_json = os.path.join(tmp, "commits.json")
    cc.main(["extract", "--repo", repo, "--out", commits_json,
             "--include", "src/**"])
    data = json.load(open(commits_json))
    subjects = {c["subject"]: c for c in data["commits"]}

    # bot + docs-only commits excluded
    assert "Apply fixes from StyleCI" not in subjects, "bot commit leaked in"
    assert "Fix typo in README" not in subjects, "docs-only commit leaked in"
    assert "Add rate calculation" in subjects and "Fix off-by-one in rate check (#7)" in subjects
    # issue ref parsed
    assert subjects["Fix off-by-one in rate check (#7)"]["issues"] == [7]
    print("ok  extract excludes bot + docs commits, parses issue refs")

    # fabricate the classification the LLM would produce
    cls_json = os.path.join(tmp, "classifications.json")
    classifications = []
    for c in data["commits"]:
        label = "bug_fix" if c["subject"].startswith("Fix off-by-one") else "feature"
        classifications.append({"hash": c["hash"], "label": label,
                                "confidence": 0.95, "rationale": "test"})
    json.dump({"classifications": classifications}, open(cls_json, "w"))

    report_md = os.path.join(tmp, "report.md")
    report_json = os.path.join(tmp, "report.json")
    cc.main(["attribute", "--repo", repo, "--commits", commits_json,
             "--classifications", cls_json, "--out-md", report_md,
             "--out-json", report_json])
    report = json.load(open(report_json))
    devs = {d["author"].split(" <")[0]: d for d in report["developers"]}

    assert report["totals"]["fix_commits"] == 1, report["totals"]
    # Alice's buggy line was blamed (despite the bot reformat in between).
    assert devs["Alice Dev"]["fix_churn_lines"] >= 1, devs["Alice Dev"]
    # It was fixed by Bob, i.e. attributed as "other", not self.
    assert devs["Alice Dev"]["fix_churn_other"] >= 1, devs["Alice Dev"]
    assert devs["Alice Dev"]["fix_churn_self"] == 0, devs["Alice Dev"]
    # Bob authored code but none of it was fixed.
    assert devs["Bob Dev"]["fix_churn_lines"] == 0, devs["Bob Dev"]
    assert devs["Alice Dev"]["lines_authored"] >= 2
    print("ok  attribute blames Alice's bugged line through the bot reformat")

    md = open(report_md).read()
    assert "Fix rate" in md and "Methodology & caveats" in md
    print("ok  markdown report renders")


def main():
    test_parse_diff_suspect_lines()
    with tempfile.TemporaryDirectory() as tmp:
        test_end_to_end(tmp)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
