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
  On hold             carries a HOLD_LABELS label (e.g. string freeze)
  With authors        changes requested, CI failing, merge conflict, or a
                      reviewer's comment newer than the author's last push
                      or comment
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
from collections import Counter, defaultdict
from datetime import datetime, timezone

STALE_DAYS = 90
TITLE_MAX = 72
AREA_ORDER = ["Music & UI", "Blocks & Runtime", "Planet", "Docs & i18n", "Governance", "Tests & CI", "General JS", "Other"]
GENERIC_AREAS = {"Tests & CI", "General JS", "Other"}  # used as a PR's area only when nothing more specific applies
HOLD_LABELS = ["String Freeze"]      # approved but deliberately not merged yet; shown in their own section
MAX_ROWS = 120  # per table; keeps the issue body under GitHub's 65 KB limit

# Path prefix -> area name. First match wins. Keep in step with MAINTAINERS.md.
AREAS = [
    ("planet/", "Planet"), ("js/planetInterface.js", "Planet"), ("js/SaveInterface.js", "Planet"),
    ("js/__tests__/planetInterface", "Planet"), ("js/__tests__/SaveInterface", "Planet"),
    (".github/workflows/", "Tests & CI"), (".husky/", "Tests & CI"), ("cypress", "Tests & CI"),
    ("test/", "Tests & CI"), ("jest.config", "Tests & CI"), ("eslint.config", "Tests & CI"),
    ("commitlint", "Tests & CI"), ("lighthouserc", "Tests & CI"), (".prettier", "Tests & CI"),
    
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
        author { login __typename }
        labels(first: 20) { nodes { name } }
        files(first: 100) { nodes { path } }
        reviewRequests(first: 10) { nodes { requestedReviewer { ... on User { login } ... on Team { name } } } }
        latestReviews(first: 20) { nodes { author { login __typename } state submittedAt } }
        comments(last: 10) { nodes { author { login __typename } createdAt } }
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
        rules.append((codeowners_regex(pattern), owners, pattern))
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


def area_owners_from_rules(raw_rules):
    """Area -> sorted owners, from the CODEOWNERS patterns themselves."""
    out = defaultdict(set)
    for pattern, owners in raw_rules:
        out[area_for(pattern.lstrip("/"))].update(o.lstrip("@") for o in owners)
    return out


def owners_for(path, rules):
    owners = []
    for rx, o, _ in rules:
        if rx.search(path):
            owners = o
    return owners


def area_for(path):
    if path.startswith(("js/__tests__/planetInterface", "js/__tests__/SaveInterface")):
        return "Planet"
    if "__tests__/" in path or path.startswith("cypress"):
        return "Tests & CI"
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
    requested = []
    for rq in pr["reviewRequests"]["nodes"]:
        who = rq["requestedReviewer"] or {}
        login = who.get("login") or who.get("name")
        if login:
            requested.append(login)

    area_owners = defaultdict(set)
    for f in pr["files"]["nodes"]:
        area_owners[area_for(f["path"])].update(o.lstrip("@") for o in owners_for(f["path"], rules))
    areas = sorted(area_owners, key=lambda a: AREA_ORDER.index(a) if a in AREA_ORDER else 99) or ["Other"]
    specific = [a for a in areas if a not in GENERIC_AREAS]
    primary = (specific or areas)[0]

    labels = [l["name"] for l in pr["labels"]["nodes"]]
    hold = [l for l in labels if l in HOLD_LABELS]

    # Newest human activity from the author vs. from anyone else (bots excluded by account type).
    def human(node):
        a = node.get("author") or {}
        return a.get("login") and a.get("__typename") != "Bot"
    author_times = [ts(commit["committedDate"])] if commit else []
    other_times = []
    for r in pr["latestReviews"]["nodes"]:
        if human(r):
            (author_times if r["author"]["login"] == author else other_times).append((ts(r["submittedAt"]), r["author"]["login"]))
    for c in pr["comments"]["nodes"]:
        if human(c):
            (author_times if c["author"]["login"] == author else other_times).append((ts(c["createdAt"]), c["author"]["login"]))
    author_times = [t if isinstance(t, datetime) else t[0] for t in author_times]
    last_author_activity = max(author_times, default=ts(pr["createdAt"]))
    last_other = max(other_times, default=None)

    changes_by = [r["author"]["login"] for r in reviews if r["state"] == "CHANGES_REQUESTED"]
    approved_by = [r["author"]["login"] for r in reviews if r["state"] == "APPROVED"]

    reviewers = []
    blockers = []
    if conflict:
        blockers.append("<kbd>merge conflict</kbd>")
    if ci in ("FAILURE", "ERROR"):
        blockers.append("<kbd>CI failing</kbd>")
    if changes_by:
        blockers.append("<kbd>changes requested</kbd> " + users(changes_by))
    if not blockers and last_other and last_other[0] > last_author_activity:
        blockers.append("<kbd>reply to</kbd> " + last_other[1])

    kinds = []
    if conflict: kinds.append("merge conflict")
    if ci in ("FAILURE", "ERROR"): kinds.append("CI failing")
    if changes_by: kinds.append("changes requested")
    if not kinds and last_other and last_other[0] > last_author_activity: kinds.append("unanswered question")

    if hold:
        route, waiting = "hold", " ".join(f"<kbd>{l}</kbd>" for l in hold) + (" · " + " ".join(blockers) if blockers else "")
    elif blockers:
        route, waiting = "author", " ".join(blockers)
    elif pr["reviewDecision"] == "APPROVED":
        route, waiting = "ready", "<kbd>approved</kbd> " + users(approved_by)
    else:
        route = "review"
        if requested:
            waiting, reviewers = users(requested), requested
        elif reviews:
            reviewers = sorted({r["author"]["login"] for r in reviews})
            waiting = users(reviewers) + " <kbd>re-review</kbd>"
        else:
            waiting, reviewers = "<kbd>unassigned</kbd>", []

    return {
        "number": pr["number"], "title": " ".join(pr["title"].split()), "url": pr["url"], "author": author,
        "route": route, "waiting": waiting, "areas": areas, "primary": primary,
        "bot": (pr["author"] or {}).get("__typename") == "Bot", "reviewers": reviewers, "area_owners": area_owners, "requested": requested,
        "age": days(ts(pr["createdAt"])), "idle": days(last_author_activity), "kinds": kinds,
        "unassigned": route == "review" and not requested and not reviews,
        "stale": route == "author" and days(last_author_activity) >= STALE_DAYS,
    }


def users(logins):
    return ", ".join(logins)


def mentions(logins):
    return ", ".join(f"@{u}" for u in logins)


EMPTY = "_Nothing here._\n"


def section(entries, open_=False, **kw):
    """A collapsible table, or a one-line empty state."""
    if not entries:
        return EMPTY
    n = len(entries)
    return details(f"{n} pull request" + ("" if n == 1 else "s"), table(entries, **kw), open_=open_)


def table(entries, last_col="Age", last_key="age", mid_col="Waiting for", author=False):
    if not entries:
        return EMPTY
    head = (f"| Pull request | Author | {mid_col} | {last_col} |\n|---|---|---|---:|" if author
            else f"| Pull request | {mid_col} | {last_col} |\n|---|---|---:|")
    rows = []
    ordered = sorted(entries, key=lambda e: -e[last_key])
    for e in ordered[:MAX_ROWS]:
        title = e["title"].replace("|", "\\|")
        if len(title) > TITLE_MAX:
            title = title[:TITLE_MAX - 1].rstrip() + "…"
        n = e[last_key]
        age = f"**{n}d**" if n >= 60 else f"{n}d"
        who = f" {e['author']} |" if author else ""
        rows.append(f"| [#{e['number']}]({e['url']}) {title} |{who} {e['waiting']} | {age} |")
    if len(ordered) > MAX_ROWS:
        rows.append(f"\n_and {len(ordered) - MAX_ROWS} more._")
    return "\n".join([head, *rows])


def details(summary, body, open_=False):
    tag = "<details open>" if open_ else "<details>"
    return f"{tag}\n<summary>{summary}</summary>\n\n{body}\n\n</details>\n"


def render(prs, source_repo, rules):
    owners_by_area = area_owners_from_rules([(pat, o) for _, o, pat in rules])
    entries = [evaluate(pr, rules) for pr in prs if not pr["isDraft"]]
    drafts = sum(1 for pr in prs if pr["isDraft"])
    stale = [e for e in entries if e["stale"]]
    live = [e for e in entries if not e["stale"]]
    ready = [e for e in live if e["route"] == "ready"]
    review = [e for e in live if e["route"] == "review"]
    authors = [e for e in live if e["route"] == "author"]
    hold = [e for e in live if e["route"] == "hold"]

    bots = [e for e in live if e["bot"]]
    ready = [e for e in ready if not e["bot"]]
    review = [e for e in review if not e["bot"]]
    authors = [e for e in authors if not e["bot"]]
    hold = [e for e in hold if not e["bot"]]
    by_area = defaultdict(list)
    for e in review:
        by_area[e["primary"]].append(e)

    def breakdown(entries):
        c = Counter(k for e in entries for k in e["kinds"])
        return " · ".join(f"**{n}** {k}" for k, n in c.most_common()) if c else ""

    unassigned = sum(1 for e in review if e["unassigned"])

    def badge(label, n, color):
        text = label.replace(" ", "_").replace("-", "--")
        return f'<img alt="{label}: {n}" src="https://img.shields.io/badge/{text}-{n}-{color}?style=flat-square">'

    out = [
        '<p align="center">',
        f'  <strong>{source_repo}</strong> · open pull requests by who acts next<br>',
        f"  <sub>updated daily · {NOW.strftime('%Y-%m-%d')}</sub>",
        "</p>",
        '<p align="center">',
        "  " + " ".join([
            badge("ready to merge", len(ready), "2ea043"),
            badge("in review", len(review), "0969da"),
            badge("with authors", len(authors), "bf8700"),
            badge("on hold", len(hold), "8b949e"),
            badge("stale", len(stale), "cf222e"),
        ]),
        "</p>",
        "",
        "## Ready to merge",
        "*Approved by a code owner, CI green, no conflicts. Needs a merge decision.*",
        "",
        section(ready, open_=True),
        "",
        "## In review",
        "*Waiting on a reviewer, by area. Each PR is listed once, under the most specific area it touches. "
        "Waiting is days since the author last pushed or commented.*",
        "",
    ]
    if unassigned:
        out += [f"**{unassigned}** with no reviewer requested.", ""]
    ordered_areas = [a for a in AREA_ORDER if a in by_area] + sorted(a for a in by_area if a not in AREA_ORDER)
    if not ordered_areas:
        out += ["_Nothing here._", ""]
    else:
        legend = " · ".join(f"{a}: {mentions(sorted(owners_by_area[a]))}" for a in ordered_areas if owners_by_area.get(a))
        out += [f"<sub>Code owners — {legend}</sub>", ""]
    for area in ordered_areas:
        items = by_area[area]
        out += [f"### {area}", "",
                section(items, open_=True, last_col="Waiting", last_key="idle", mid_col="Reviewers")]
    out += [
        "",
        "## With authors",
        "*Changes requested, CI failing, merge conflict, or an unanswered question from a reviewer.*",
        "",
        breakdown(authors),
        "",
        section(authors, mid_col="Blocked by"),
        "",
        "## On hold",
        f"*Labelled {', '.join(f'`{l}`' for l in HOLD_LABELS)}: reviewed, but merging waits on the project.*",
        "",
        section(hold),
        "",
        "## Stale",
        f"*Blocked, and the author has not pushed or commented for {STALE_DAYS}+ days. Candidates to close or take over.*",
        "",
        breakdown(stale),
        "",
        section(stale, last_col="Idle", last_key="idle", mid_col="Blocked by", author=True),
        "",
        "## Automated",
        "*Opened by bots (dependency bumps, release chores).*",
        "",
        section(bots),
        "",
        "> [!NOTE]",
        f"> **How this is grouped.** Ready to merge: approved by a code owner, CI green, no conflicts. "
        "With authors: changes requested, CI failing, merge conflict, or a reviewer's comment newer than the author's last push. "
        f"Stale: with authors and silent {STALE_DAYS}+ days. On hold: labelled {', '.join(f'`{l}`' for l in HOLD_LABELS)}. "
        "Waiting / Idle: days since the author last pushed or commented. Ages past 60 days are bold. "
        f"Drafts ({drafts}) and reviewers without CODEOWNERS entries are not shown.",
    ]
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source-repo", required=True)
    p.add_argument("--issue-repo", required=True)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()

    rules = fetch_codeowners(a.source_repo)
    body = render(fetch_prs(a.source_repo), a.source_repo, rules)
    if a.dry_run:
        print(body)
        return
    gh("issue", "edit", str(a.issue), "--repo", a.issue_repo, "--body-file", "-", stdin=body)
    print(f"Updated {a.issue_repo}#{a.issue} ({len(body)} chars)")


if __name__ == "__main__":
    main()
