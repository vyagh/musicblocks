#!/usr/bin/env python3
"""Rewrite one issue with a table of open pull requests, grouped by who acts next.

Usage:
    pr-dashboard.py --source-repo OWNER/NAME --issue-repo OWNER/NAME --issue N [--dry-run]

Reads open PRs from --source-repo and writes the rendered table into the body
of issue N on --issue-repo. Uses the `gh` CLI for all GitHub calls, so the
only credential needed is GH_TOKEN with permission to edit that issue.

Grouping rules (simple and deterministic, no LLM):

  Waiting on author      changes requested, CI failing, merge conflict,
                         or a reviewer was the last person to act
  Waiting on maintainer  approved and nothing blocks merge
  Waiting on reviewer    everything else (review requested, author acted last)
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone

QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: 100, after: $cursor, states: OPEN, orderBy: {field: CREATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title url isDraft createdAt reviewDecision mergeable
        author { login }
        reviewRequests(first: 10) { nodes { requestedReviewer { ... on User { login } ... on Team { name } } } }
        latestReviews(first: 20) { nodes { author { login } state submittedAt } }
        reviewThreads(first: 50) { nodes { isResolved } }
        comments(last: 1) { nodes { author { login } createdAt } }
        commits(last: 1) { nodes { commit { committedDate statusCheckRollup { state } } } }
      }
    }
  }
}
"""

ICON = {"APPROVED": "✅", "CHANGES_REQUESTED": "🔴", "COMMENTED": "💬", "PENDING": "⏳"}
CI = {"SUCCESS": "✅", "FAILURE": "❌", "ERROR": "❌", "PENDING": "🟡", "EXPECTED": "🟡", None: "—"}


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


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def classify(pr):
    author = (pr["author"] or {}).get("login", "ghost")
    ci = (pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"] or {}).get("state") if pr["commits"]["nodes"] else None
    conflict = pr["mergeable"] == "CONFLICTING"
    reviews = [r for r in pr["latestReviews"]["nodes"] if r["author"] and r["author"]["login"] != author]
    open_threads = sum(1 for t in pr["reviewThreads"]["nodes"] if not t["isResolved"])
    approved = pr["reviewDecision"] == "APPROVED"

    # Who acted last: author or someone else?
    events = []
    if pr["commits"]["nodes"]:
        events.append((ts(pr["commits"]["nodes"][0]["commit"]["committedDate"]), author))
    for r in reviews:
        events.append((ts(r["submittedAt"]), r["author"]["login"]))
    for c in pr["comments"]["nodes"]:
        if c["author"]:
            events.append((ts(c["createdAt"]), c["author"]["login"]))
    last_actor = max(events, key=lambda e: e[0])[1] if events else author

    if pr["reviewDecision"] == "CHANGES_REQUESTED" or ci in ("FAILURE", "ERROR") or conflict:
        route = "author"
    elif approved:
        route = "maintainer"
    elif last_actor != author and (reviews or open_threads):
        route = "author"
    else:
        route = "reviewer"

    reviewer_cells = []
    for r in reviews:
        reviewer_cells.append(f"{r['author']['login']}&nbsp;{ICON.get(r['state'], '')}")
    seen = {r["author"]["login"] for r in reviews}
    for rq in pr["reviewRequests"]["nodes"]:
        who = rq["requestedReviewer"] or {}
        login = who.get("login") or who.get("name")
        if login and login not in seen:
            reviewer_cells.append(f"{login}&nbsp;⏳")
    if open_threads:
        reviewer_cells.append(f"💬 {open_threads} open")

    age = (datetime.now(timezone.utc) - ts(pr["createdAt"])).days
    row = "| #{n} {t} | {a} | {r} | {ci} | {cf} | {age}d |".format(
        n=pr["number"], t=pr["title"].replace("|", "\\|"), a=author,
        r="<br>".join(reviewer_cells) or "—", ci=CI.get(ci, "—"),
        cf="❌" if conflict else "✅", age=age)
    return route, row


def render(prs, source_repo):
    buckets = {"maintainer": [], "reviewer": [], "author": []}
    drafts = 0
    for pr in prs:
        if pr["isDraft"]:
            drafts += 1
            continue
        route, row = classify(pr)
        buckets[route].append(row)

    head = "| PR | Author | Reviewers | CI | Conflicts | Age |\n|---|---|---|:---:|:---:|:---:|"
    out = [
        "> [!NOTE]",
        f"> Open non-draft pull requests in `{source_repo}`, grouped by who is expected to act next. "
        f"Refreshed automatically; {drafts} draft PR(s) omitted.",
        ">",
        "> Reviewers: ✅ approved · 🔴 changes requested · 💬 commented / open threads · ⏳ review requested.",
        "",
    ]
    for key, title, blurb in (
        ("maintainer", "Waiting on maintainers", "Approved and not blocked. Needs a merge decision."),
        ("reviewer", "Waiting on reviewers", "No review yet, or the author has answered and is waiting."),
        ("author", "Waiting on authors", "Changes requested, CI failing, merge conflict, or unanswered review."),
    ):
        rows = buckets[key]
        out += [f"## {title} ({len(rows)})", "", blurb, ""]
        out += [head, *rows] if rows else ["_None._"]
        out.append("")
    out.append(f"_Last updated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}._")
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source-repo", required=True)
    p.add_argument("--issue-repo", required=True)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()

    body = render(fetch_prs(a.source_repo), a.source_repo)
    if a.dry_run:
        print(body)
        return
    gh("issue", "edit", str(a.issue), "--repo", a.issue_repo, "--body-file", "-", stdin=body)
    print(f"Updated {a.issue_repo}#{a.issue} ({len(body)} chars)")


if __name__ == "__main__":
    main()
