#!/usr/bin/env python3
"""Rewrite one issue with a dashboard of open pull requests, grouped by who acts next.

Usage:
    pr-dashboard.py --source-repo OWNER/NAME --issue-repo OWNER/NAME --issue N [--dry-run]

Reads open PRs from --source-repo and writes the rendered dashboard into the
body of issue N on --issue-repo. Uses the `gh` CLI for all GitHub calls, so
the only credential needed is GH_TOKEN with permission to edit that issue.

Sections:

  Ready to merge      approved by a code owner, CI green, no conflicts
  In review           waiting on a reviewer; also shown per CODEOWNERS area
  With authors        changes requested, CI failing, merge conflict, or an
                      unanswered review
  Stale               blocked, and the author has been silent STALE_DAYS+

Areas come from .github/CODEOWNERS in the source repo: each changed file is
matched against the CODEOWNERS rules (last match wins) and mapped to an area
name by path prefix in AREAS.
"""

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone

STALE_DAYS = 90

# Path prefix -> area name. First match wins. Keep in step with MAINTAINERS.md.
AREAS = [
    ("planet/", "Planet"), ("js/planetInterface.js", "Planet"), ("js/SaveInterface.js", "Planet"),
    ("js/__tests__/planetInterface", "Planet"), ("js/__tests__/SaveInterface", "Planet"),
    (".github/workflows/", "Tests & CI"), (".husky/", "Tests & CI"), ("cypress", "Tests & CI"),
    ("test/", "Tests & CI"), ("jest.config", "Tests & CI"), ("eslint.config", "Tests & CI"),
    ("commitlint", "Tests & CI"), ("lighthouserc", "Tests & CI"), (".prettier", "Tests & CI"),
    ("__tests__/", "Tests & CI"),
    ("js/blocks/", "Blocks & Runtime"), ("js/js-export/", "Blocks & Runtime"), ("js/activity.js", "Blocks & Runtime"),
    ("js/widgets/", "Music & UI"), ("css/", "Music & UI"), ("header-icons/", "Music & UI"),
    ("js/turtleactions/", "Music & UI"), ("lilypond/", "Music & UI"), ("examples/", "Music & UI"),
    ("lessonPlan/", "Music & UI"), ("js/abc.js", "Music & UI"), ("js/lilypond.js", "Music & UI"),
    ("js/notation.js", "Music & UI"), ("js/turtle-singer.js", "Music & UI"), ("js/utils/musicutils.js", "Music & UI"),
    (".github/CODEOWNERS", "Governance"), ("GOVERNANCE.md", "Governance"), ("MAINTAINERS.md", "Governance"),
    ("locales/", "Docs & i18n"), ("po/", "Docs & i18n"), ("guide/", "Docs & i18n"), ("README", "Docs & i18n"),
    ("documentation/", "Docs & i18n"), ("js/", "General JS"),
]

QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: 50, after: $cursor, states: OPEN, orderBy: {field: CREATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title url isDraft createdAt reviewDecision mergeable
        author { login }
        files(first: 100) { nodes { path } }
        reviewRequests(first: 10) { nodes { requestedReviewer { ... on User { login } ... on Team { name } } } }
        latestReviews(first: 20) { nodes { author { login } state submittedAt } }
        reviewThreads(first: 50) { nodes { isResolved } }
        comments(last: 5) { nodes { author { login } createdAt } }
        commits(last: 1) { nodes { commit { committedDate statusCheckRollup { state } } } }
      }
    }
  }
}
"""

NOW = datetime.now(timezone.utc)


def gh(*args, stdin=None):
    r = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"gh {' '.join(args[:2])} failed: {r.stderr.strip()}")
    return r.stdout


def fetch_prs(repo):
    owner, name = repo.split("/")
    prs, cursor = [], None
    while True:
        variables = {"owner": owner, "name": name, "cursor": cursor}
        out = gh("api", "graphql", "--input", "-",
                 stdin=json.dumps({"query": QUERY, "variables": variables}))
        data = json.loads(out)["data"]["repository"]["pullRequests"]
        prs.extend(data["nodes"])
        if not data["pageInfo"]["hasNextPage"]:
            return prs
        cursor = data["pageInfo"]["endCursor"]


def fetch_codeowners(repo):
    """Return [(regex, [owners])] in file order. Empty list if the file is missing."""
    try:
        text = gh("api", f"repos/{repo}/contents/.github/CODEOWNERS", "-H", "Accept: application/vnd.github.raw")
    except SystemExit:
        return []
    rules = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        pattern, *owners = line.split()
        rules.append((codeowners_regex(pattern), owners))
    return rules


def codeowners_regex(pattern):
    anchored = pattern.startswith("/")
    p = pattern.lstrip("/")
    dir_only = p.endswith("/")
    p = p.rstrip("/")
    out = ""
    i = 0
    while i < len(p):
        c = p[i]
        if p.startswith("**", i):
            out += ".*"
            i += 2
            if i < len(p) and p[i] == "/":
                i += 1
            continue
        out += "[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c)
        i += 1
    prefix = "^" if anchored else "^(?:.*/)?"
    suffix = "(?:/.*)?$" if dir_only or "." not in p.rsplit("/", 1)[-1] else "$"
    return re.compile(prefix + out + suffix)


def owners_for(path, rules):
    owners = []
    for rx, o in rules:
        if rx.search(path):
            owners = o
    return owners


def area_for(path):
    for prefix, name in AREAS:
        if path.startswith(prefix):
            return name
    return "Other"


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def days(dt):
    return (NOW - dt).days


def evaluate(pr, rules):
    author = (pr["author"] or {}).get("login", "ghost")
    commit = pr["commits"]["nodes"][0]["commit"] if pr["commits"]["nodes"] else None
    ci = (commit.get("statusCheckRollup") or {}).get("state") if commit else None
    conflict = pr["mergeable"] == "CONFLICTING"
    reviews = [r for r in pr["latestReviews"]["nodes"] if r["author"] and r["author"]["login"] != author]
    open_threads = sum(1 for t in pr["reviewThreads"]["nodes"] if not t["isResolved"])
    requested = []
    for rq in pr["reviewRequests"]["nodes"]:
        who = rq["requestedReviewer"] or {}
        login = who.get("login") or who.get("name")
        if login:
            requested.append(login)

    area_owners = defaultdict(set)
    for f in pr["files"]["nodes"]:
        area_owners[area_for(f["path"])].update(o.lstrip("@") for o in owners_for(f["path"], rules))
    areas = sorted(area_owners) or ["Other"]

    events = []
    if commit:
        events.append((ts(commit["committedDate"]), author))
    for r in reviews:
        events.append((ts(r["submittedAt"]), r["author"]["login"]))
    for c in pr["comments"]["nodes"]:
        if c["author"]:
            events.append((ts(c["createdAt"]), c["author"]["login"]))
    events.sort()
    last_actor = events[-1][1] if events else author
    author_events = [e for e in events if e[1] == author]
    last_author_activity = author_events[-1][0] if author_events else ts(pr["createdAt"])

    changes_by = [r["author"]["login"] for r in reviews if r["state"] == "CHANGES_REQUESTED"]
    approved_by = [r["author"]["login"] for r in reviews if r["state"] == "APPROVED"]

    blockers = []
    if conflict:
        blockers.append("merge conflict")
    if ci in ("FAILURE", "ERROR"):
        blockers.append("CI failing")
    if changes_by:
        blockers.append("changes requested by " + users(changes_by))

    if blockers:
        route, waiting = "author", ", ".join(blockers)
    elif pr["reviewDecision"] == "APPROVED":
        route, waiting = "ready", "approved by " + users(approved_by)
    elif last_actor != author and (reviews or open_threads):
        route = "author"
        waiting = f"reply to @{last_actor}" + (f", {open_threads} open threads" if open_threads > 1 else "")
    else:
        route = "review"
        if requested:
            waiting = users(requested)
        elif reviews:
            waiting = "another look from " + users(sorted({r["author"]["login"] for r in reviews}))
        else:
            waiting = "no reviewer requested"

    return {
        "number": pr["number"], "title": pr["title"], "url": pr["url"], "author": author,
        "route": route, "waiting": waiting, "areas": areas, "area_owners": area_owners, "requested": requested,
        "age": days(ts(pr["createdAt"])), "idle": days(last_author_activity),
        "stale": route == "author" and bool(blockers) and days(last_author_activity) >= STALE_DAYS,
    }


def users(logins):
    return ", ".join(f"@{u}" for u in logins)


def table(entries, last_col="Age", last_key="age"):
    if not entries:
        return "_Nothing here._"
    head = f"| PR | Area | Author | Waiting for | {last_col} |\n|---|---|---|---|---:|"
    rows = []
    for e in sorted(entries, key=lambda e: -e[last_key]):
        title = e["title"].replace("|", "\\|")
        rows.append(f"| [#{e['number']}]({e['url']}) {title} | {', '.join(e['areas'])} | @{e['author']} "
                    f"| {e['waiting']} | {e[last_key]}d |")
    return "\n".join([head, *rows])


def details(summary, body, open_=False):
    tag = "<details open>" if open_ else "<details>"
    return f"{tag}\n<summary>{summary}</summary>\n\n{body}\n\n</details>\n"


def render(prs, source_repo, rules):
    entries = [evaluate(pr, rules) for pr in prs if not pr["isDraft"]]
    drafts = sum(1 for pr in prs if pr["isDraft"])
    stale = [e for e in entries if e["stale"]]
    live = [e for e in entries if not e["stale"]]
    ready = [e for e in live if e["route"] == "ready"]
    review = [e for e in live if e["route"] == "review"]
    authors = [e for e in live if e["route"] == "author"]

    by_area = defaultdict(list)
    for e in review:
        for a in e["areas"]:
            by_area[a].append(e)

    out = [
        f"Open pull requests in {source_repo}, grouped by who acts next. "
        f"Updated daily. {drafts} drafts not shown.",
        "",
        f"**{len(entries)} open** — {len(ready)} ready to merge, {len(review)} in review, "
        f"{len(authors)} with authors, {len(stale)} stale",
        "",
        "## Ready to merge",
        "",
        "Approved by a code owner, CI green, no conflicts.",
        "",
        table(ready),
        "",
        "## In review",
        "",
        "Waiting on a reviewer. Same list twice: all together, then by area.",
        "",
        details(f"All · {len(review)}", table(review), open_=True),
    ]
    for area, items in sorted(by_area.items(), key=lambda kv: -len(kv[1])):
        owners = sorted({o for e in items for o in e["area_owners"][area]})
        label = f"{area} · {len(items)}" + (f" &nbsp;<sub>{users(owners)}</sub>" if owners else "")
        out.append(details(label, table(items)))
    out += [
        "",
        "## With authors",
        "",
        "Changes requested, CI failing, merge conflict, or an unanswered review.",
        "",
        details(f"Show {len(authors)}", table(authors)),
        "",
        "## Stale",
        "",
        f"Blocked, and the author has not pushed or commented for {STALE_DAYS} days or more. "
        "Candidates to close or take over.",
        "",
        details(f"Show {len(stale)}", table(stale, last_col="Idle", last_key="idle")),
        "",
        f"<sub>Last updated {NOW.strftime('%Y-%m-%d %H:%M UTC')}. "
        "Grouping is rule-based (review state, CI, conflicts, last activity) and may be off for edge cases.</sub>",
    ]
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source-repo", required=True)
    p.add_argument("--issue-repo", required=True)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()

    body = render(fetch_prs(a.source_repo), a.source_repo, fetch_codeowners(a.source_repo))
    if a.dry_run:
        print(body)
        return
    gh("issue", "edit", str(a.issue), "--repo", a.issue_repo, "--body-file", "-", stdin=body)
    print(f"Updated {a.issue_repo}#{a.issue} ({len(body)} chars)")


if __name__ == "__main__":
    main()
