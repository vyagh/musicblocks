#!/usr/bin/env python3
"""Rewrite one issue with a dashboard of open pull requests, grouped by who acts next.

Usage:
    pr-dashboard.py --source-repo OWNER/NAME --issue-repo OWNER/NAME --issue N [--dry-run]

Reads open PRs from --source-repo and writes the rendered dashboard into the
body of issue N on --issue-repo. Uses the `gh` CLI for all GitHub calls, so
the only credential needed is GH_TOKEN with permission to edit that issue.

Grouping rules (deterministic, no external services):

  Waiting on author      changes requested, CI failing, merge conflict,
                         or a reviewer was the last person to act
  Waiting on maintainer  approved by a code owner and nothing blocks merge
  Waiting on reviewer    everything else (review requested, author acted last)

Authors silent for STALE_DAYS while blocked are listed separately as
"likely abandoned" so maintainers have a close-candidate list.
"""

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone

STALE_DAYS = 90

QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: 100, after: $cursor, states: OPEN, orderBy: {field: CREATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title url isDraft createdAt reviewDecision mergeable
        author { login }
        labels(first: 10) { nodes { name } }
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

ICON = {"APPROVED": "✅", "CHANGES_REQUESTED": "🔴", "COMMENTED": "💬", "PENDING": "⏳"}
CI = {"SUCCESS": "✅", "FAILURE": "❌", "ERROR": "❌", "PENDING": "🟡", "EXPECTED": "🟡"}
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


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def days(dt):
    return (NOW - dt).days


def evaluate(pr):
    """Return a dict describing one PR: route, blockers, reviewers, dates."""
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

    # Activity timeline: (time, actor)
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
        blockers.append("changes requested by " + ", ".join(f"@{u}" for u in changes_by))

    if blockers:
        route = "author"
    elif pr["reviewDecision"] == "APPROVED":
        route = "maintainer"
        blockers.append("ready to merge")
    elif last_actor != author and (reviews or open_threads):
        route = "author"
        blockers.append(f"reply to @{last_actor}" + (f" ({open_threads} open threads)" if open_threads else ""))
    else:
        route = "reviewer"
        if requested:
            blockers.append("review by " + ", ".join(f"@{u}" for u in requested))
        elif reviews:
            blockers.append("author replied, needs another look")
        else:
            blockers.append("no reviewer assigned")

    reviewer_cells = [f"{r['author']['login']}&nbsp;{ICON.get(r['state'], '')}" for r in reviews]
    seen = {r["author"]["login"] for r in reviews}
    reviewer_cells += [f"{u}&nbsp;⏳" for u in requested if u not in seen]

    return {
        "number": pr["number"], "title": pr["title"], "url": pr["url"], "author": author,
        "route": route, "blockers": blockers, "reviewers": reviewer_cells,
        "requested": requested, "approved_by": approved_by,
        "ci": CI.get(ci, "—"), "conflict": conflict,
        "age": days(ts(pr["createdAt"])), "author_idle": days(last_author_activity),
        "labels": [l["name"] for l in pr["labels"]["nodes"]],
        "stale": bool(blockers) and route == "author" and days(last_author_activity) >= STALE_DAYS,
    }


HEAD = "| PR | Author | Why | Reviewers | CI | Conflicts | Age |\n|---|---|---|---|:---:|:---:|:---:|"


def row(e):
    title = e["title"].replace("|", "\\|")
    return "| [#{n}]({u}) {t} | {a} | {w} | {r} | {ci} | {cf} | {age}d |".format(
        n=e["number"], u=e["url"], t=title, a=e["author"], w="; ".join(e["blockers"]),
        r="<br>".join(e["reviewers"]) or "—", ci=e["ci"],
        cf="❌" if e["conflict"] else "✅", age=e["age"])


def table(rows):
    return "\n".join([HEAD, *rows]) if rows else "_None._"


def section(title, body, open_=True, blurb=""):
    tag = "<details open>" if open_ else "<details>"
    out = [tag, f"<summary><b>{title}</b></summary>", ""]
    if blurb:
        out += [blurb, ""]
    out += [body, "", "</details>", ""]
    return "\n".join(out)


def render(prs, source_repo):
    evaluated = [evaluate(pr) for pr in prs if not pr["isDraft"]]
    drafts = sum(1 for pr in prs if pr["isDraft"])
    by_route = defaultdict(list)
    stale = []
    for e in evaluated:
        (stale if e["stale"] else by_route[e["route"]]).append(e)

    # Per-reviewer queues: PRs where this person is requested and the ball is with reviewers.
    queues = defaultdict(list)
    for e in by_route["reviewer"]:
        for u in e["requested"]:
            queues[u].append(e)

    n_m, n_r, n_a = (len(by_route[k]) for k in ("maintainer", "reviewer", "author"))
    out = [
        "> [!NOTE]",
        f"> Open non-draft pull requests in `{source_repo}`, grouped by who is expected to act next. "
        f"Refreshed automatically once a day; {drafts} draft PR(s) omitted. "
        "The grouping uses simple rules (review state, CI, conflicts, who spoke last) and can be wrong.",
        ">",
        "> Reviewers: ✅ approved · 🔴 changes requested · 💬 commented · ⏳ review requested.",
        "",
        f"**{len(evaluated)} open** · 🟢 **{n_m}** ready to merge · 👀 **{n_r}** waiting on reviewers · "
        f"✍️ **{n_a}** waiting on authors · 💤 **{len(stale)}** likely abandoned",
        "",
    ]

    out.append(section(f"🟢 Waiting on maintainers ({n_m})",
                       table([row(e) for e in sorted(by_route["maintainer"], key=lambda e: -e["age"])]),
                       blurb="Approved by a code owner and not blocked. Needs a merge decision."))
    out.append(section(f"👀 Waiting on reviewers ({n_r})",
                       table([row(e) for e in sorted(by_route["reviewer"], key=lambda e: -e["age"])]),
                       blurb="No review yet, or the author has answered and is waiting."))
    if queues:
        parts = []
        for u, items in sorted(queues.items(), key=lambda kv: -len(kv[1])):
            parts.append(section(f"@{u} · {len(items)}", table([row(e) for e in sorted(items, key=lambda e: -e['age'])]), open_=False))
        out.append(section(f"📋 Review queue by person ({len(queues)})", "\n".join(parts), open_=False,
                           blurb="The same PRs as above, one list per requested reviewer."))
    out.append(section(f"✍️ Waiting on authors ({n_a})",
                       table([row(e) for e in sorted(by_route["author"], key=lambda e: -e["age"])]), open_=False,
                       blurb="Changes requested, CI failing, merge conflict, or an unanswered review."))
    out.append(section(f"💤 Likely abandoned ({len(stale)})",
                       table([row(e) for e in sorted(stale, key=lambda e: -e["author_idle"])]), open_=False,
                       blurb=f"Blocked, and the author has not pushed or commented for {STALE_DAYS}+ days. "
                             "Candidates to close or take over."))
    out.append(f"_Last updated {NOW.strftime('%Y-%m-%d %H:%M UTC')}._")
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
