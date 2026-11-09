#!/usr/bin/env python3
"""Build the data cards for github.com/Mertbaturg.

Everything on the cards is measured, not typed in by hand:

  --github   GitHub GraphQL API -> data/github.json
             (contributions per year, repos across personal + org accounts,
              stars, language bytes). Needs a token that can read private
              repos: GH_TOKEN / GITHUB_TOKEN, else `gh auth token`.
  --ai       Local AI usage logs -> data/ai.json
             (~/.claude/stats-cache.json, ~/.claude.json dates,
              ~/.codex/sessions/**/*.jsonl). Only works on my machine.
  --render   data/*.json -> assets/*.svg (default when no flag is given)

Only aggregates are written: no repo names, paths or account ids.
Stdlib only, Python 3.9+.
"""
from __future__ import annotations

import argparse
import base64
import collections
import datetime as dt
import glob
import html
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
ASSETS = ROOT / "assets"

# Repos that are not mine to count (coursework sandboxes, templates).
SKIP_OWNERS = {"code50"}

# ---------------------------------------------------------------- GitHub ----

GQL = "https://api.github.com/graphql"


def token() -> str:
    for k in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(k):
            return os.environ[k]
    return subprocess.check_output(["gh", "auth", "token"], text=True).strip()


def gql(query: str, **variables):
    req = urllib.request.Request(
        GQL,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"bearer {token()}", "User-Agent": "profile-cards"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.load(r)
    if out.get("errors"):
        raise SystemExit(f"GraphQL error: {out['errors']}")
    return out["data"]


REPO_FIELDS = """
  pageInfo { hasNextPage endCursor }
  nodes {
    nameWithOwner isPrivate stargazerCount forkCount
    languages(first: 30, orderBy: {field: SIZE, direction: DESC}) {
      edges { size node { name } }
    }
  }
"""


def paged(query: str, path: list[str], **v):
    cursor, nodes = None, []
    while True:
        d = gql(query, cursor=cursor, **v)
        for p in path:
            d = d[p]
        nodes += d["nodes"]
        if not d["pageInfo"]["hasNextPage"]:
            return nodes
        cursor = d["pageInfo"]["endCursor"]


def collect_github() -> dict:
    me = gql("""{ viewer { login createdAt followers { totalCount }
        organizations(first: 50) { nodes { login } }
        repositoriesContributedTo(first: 1, contributionTypes: [COMMIT, PULL_REQUEST, REPOSITORY]) { totalCount }
        starredRepositories { totalCount } } }""")["viewer"]
    login = me["login"]
    orgs = [o["login"] for o in me["organizations"]["nodes"]]

    # Contributions, one calendar year at a time (API max span is 1 year).
    now = dt.datetime.now(dt.timezone.utc)
    by_year, totals = {}, collections.Counter()
    for year in range(int(me["createdAt"][:4]), now.year + 1):
        end = min(dt.datetime(year, 12, 31, 23, 59, 59, tzinfo=dt.timezone.utc), now)
        c = gql(
            """query($login: String!, $from: DateTime!, $to: DateTime!) {
              user(login: $login) { contributionsCollection(from: $from, to: $to) {
                totalCommitContributions totalPullRequestContributions
                totalIssueContributions totalPullRequestReviewContributions
                restrictedContributionsCount
                contributionCalendar { totalContributions } } } }""",
            login=login, **{"from": f"{year}-01-01T00:00:00Z", "to": end.strftime("%Y-%m-%dT%H:%M:%SZ")},
        )["user"]["contributionsCollection"]
        by_year[str(year)] = c["contributionCalendar"]["totalContributions"]
        totals.update(
            commits=c["totalCommitContributions"],
            prs=c["totalPullRequestContributions"],
            issues=c["totalIssueContributions"],
            reviews=c["totalPullRequestReviewContributions"],
            contributions=c["contributionCalendar"]["totalContributions"],
        )

    repos = paged(
        f"""query($login: String!, $cursor: String) {{ user(login: $login) {{
            repositories(first: 100, after: $cursor, ownerAffiliations: OWNER, isFork: false) {{ {REPO_FIELDS} }} }} }}""",
        ["user", "repositories"], login=login,
    )
    for org in orgs:
        repos += paged(
            f"""query($org: String!, $cursor: String) {{ organization(login: $org) {{
                repositories(first: 100, after: $cursor, isFork: false) {{ {REPO_FIELDS} }} }} }}""",
            ["organization", "repositories"], org=org,
        )
    seen, uniq = set(), []
    for r in repos:
        if r["nameWithOwner"] in seen or r["nameWithOwner"].split("/")[0] in SKIP_OWNERS:
            continue
        seen.add(r["nameWithOwner"])
        uniq.append(r)

    lang = collections.Counter()
    for r in uniq:
        for e in r["languages"]["edges"]:
            lang[e["node"]["name"]] += e["size"]
    total_bytes = sum(lang.values()) or 1

    return {
        "login": login,
        "generated_at": now.strftime("%Y-%m-%d"),
        "member_since": me["createdAt"][:10],
        "contributions_by_year": by_year,
        **dict(totals),
        "repos": len(uniq),
        "repos_private": sum(r["isPrivate"] for r in uniq),
        "orgs": len(orgs),
        "stars": sum(r["stargazerCount"] for r in uniq),
        "forks": sum(r["forkCount"] for r in uniq),
        "followers": me["followers"]["totalCount"],
        "contributed_to": me["repositoriesContributedTo"]["totalCount"],
        "languages": [
            {"name": n, "bytes": b, "pct": round(b / total_bytes * 100, 2)}
            for n, b in lang.most_common()
        ],
        "language_count": len(lang),
    }


# -------------------------------------------------------------------- AI ----

FAMILIES = ("opus", "sonnet", "fable", "haiku")


def family(model: str) -> str:
    return next((f for f in FAMILIES if f in model), "other")


def pretty_model(m: str) -> str:
    # claude-opus-4-5-20251101 -> Opus 4.5
    parts = [p for p in m.replace("claude-", "").split("-") if not (p.isdigit() and len(p) == 8)]
    return parts[0].capitalize() + " " + ".".join(parts[1:])


def collect_ai() -> dict:
    home = Path.home()
    s = json.loads((home / ".claude/stats-cache.json").read_text())
    acct = json.loads((home / ".claude.json").read_text())

    tok = collections.Counter()
    fam, models = collections.Counter(), {}
    fam_out = collections.Counter()
    for m, u in s["modelUsage"].items():
        t = u["inputTokens"] + u["outputTokens"] + u["cacheReadInputTokens"] + u["cacheCreationInputTokens"]
        tok.update(input=u["inputTokens"], output=u["outputTokens"],
                   cache_read=u["cacheReadInputTokens"], cache_write=u["cacheCreationInputTokens"])
        fam[family(m)] += t
        fam_out[family(m)] += u["outputTokens"]
        models[pretty_model(m)] = models.get(pretty_model(m), 0) + t

    monthly, days = collections.Counter(), collections.Counter()
    daily = {}
    for d in s["dailyModelTokens"]:
        v = sum(d["tokensByModel"].values())
        monthly[d["date"][:7]] += v
        days[d["date"][:7]] += 1
        daily[d["date"]] = v
    peak_day = max(daily.items(), key=lambda kv: kv[1]) if daily else (None, 0)
    peak_month = max(monthly.items(), key=lambda kv: kv[1]) if monthly else (None, 0)

    # Codex (GPT) sessions: last cumulative token_count per rollout file.
    codex_total, codex_models, codex_first = 0, collections.Counter(), None
    for f in sorted(glob.glob(str(home / ".codex/sessions/**/*.jsonl"), recursive=True)):
        last, model = None, None
        with open(f, errors="ignore") as fh:
            for line in fh:
                if '"token_count"' not in line and '"turn_context"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                p = o.get("payload", {})
                if o.get("type") == "turn_context" and p.get("model"):
                    model = p["model"].split("/")[-1]
                if p.get("type") == "token_count" and (p.get("info") or {}).get("total_token_usage"):
                    last = p["info"]["total_token_usage"]
        if last:
            codex_first = codex_first or Path(f).name[8:18]
            codex_total += last.get("total_tokens", 0)
            codex_models[model or "unknown"] += last.get("total_tokens", 0)

    ls = s.get("longestSession", {})
    oauth = acct.get("oauthAccount", {})
    return {
        "generated_at": dt.date.today().isoformat(),
        "claude": {
            "first_logged": s["firstSessionDate"][:10],
            "last_logged": s.get("lastComputedDate"),
            "claude_account_since": (oauth.get("accountCreatedAt") or "")[:10],
            "claude_code_since": (acct.get("claudeCodeFirstTokenDate") or "")[:10],
            "plan": "Max" if oauth.get("organizationType") == "claude_max" else oauth.get("organizationType"),
            "tokens": dict(tok),
            "total": sum(tok.values()),
            "by_family": dict(fam.most_common()),
            "output_by_family": dict(fam_out.most_common()),
            "by_model": dict(sorted(models.items(), key=lambda kv: -kv[1])),
            "monthly": [{"month": m, "tokens": monthly[m], "days": days[m]} for m in sorted(monthly)],
            "unsplit_before": sum(tok.values()) - sum(monthly.values()),
            "peak_month": {"month": peak_month[0], "tokens": peak_month[1], "days": days.get(peak_month[0], 0)},
            "peak_day": {"date": peak_day[0], "tokens": peak_day[1]},
            "sessions": s.get("totalSessions"),
            "messages": s.get("totalMessages"),
            "longest_session_days": round(ls.get("duration", 0) / 86_400_000, 1),
            "longest_session_messages": ls.get("messageCount"),
        },
        "codex": {
            "since": codex_first,
            "total": codex_total,
            "by_model": dict(codex_models.most_common()),
        },
    }


# ---------------------------------------------------------------- render ----

FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
INK = "#f0f6fc"        # primary text
INK2 = "#a8b3c4"       # secondary text
INK3 = "#7c8799"       # muted text / axis
GRID = "#1e293b"
SKY, VIOLET = "#7dd3fc", "#a78bfa"   # brand accents (decorative only)
# Validated categorical palette (dark mode, surface #0d1117): all checks pass.
SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9"]
BAR = SERIES[0]
TILE_H = 110


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def human(n: float, digits: int = 1) -> str:
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            v = n / div
            out = f"{v:.0f}" if v >= 100 else f"{v:.{digits}f}"
            if "." in out:
                out = out.rstrip("0").rstrip(".")
            return out + unit
    return f"{n:,.0f}"


def frame(w: int, h: int, label: str, body: str) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="{esc(label)}">
  <title>{esc(label)}</title>
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#070b14"/><stop offset="1" stop-color="#0d1424"/>
    </linearGradient>
    <linearGradient id="hero" x1="0" x2="1">
      <stop offset="0" stop-color="{INK}"/><stop offset=".55" stop-color="{SKY}"/><stop offset="1" stop-color="{VIOLET}"/>
    </linearGradient>
    <pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
      <rect width="6" height="6" fill="{BAR}" opacity=".35"/><line x1="0" y1="0" x2="0" y2="6" stroke="{BAR}" stroke-width="3"/>
    </pattern>
    <style>
      .t {{ font-family: {FONT}; }}
      .m {{ font-family: {MONO}; }}
    </style>
  </defs>
  <rect width="{w}" height="{h}" rx="20" fill="url(#bg)"/>
  <rect x=".5" y=".5" width="{w-1}" height="{h-1}" rx="19.5" fill="none" stroke="{GRID}"/>
{body}
</svg>
"""


def header(x: int, y: int, eyebrow: str, title: str, right: str = "", w: int = 1200) -> str:
    out = (f'  <text x="{x}" y="{y}" class="m" font-size="15" letter-spacing="3" fill="{SKY}">{esc(eyebrow)}</text>\n'
           f'  <text x="{x}" y="{y+40}" class="t" font-size="32" font-weight="700" fill="{INK}">{esc(title)}</text>\n')
    if right:
        out += f'  <text x="{w-x}" y="{y}" class="m" font-size="14" fill="{INK3}" text-anchor="end">{esc(right)}</text>\n'
    return out


def tile(x: int, y: int, w: int, value: str, label: str, sub: str = "") -> str:
    s = (f'  <g transform="translate({x},{y})">\n'
         f'    <rect width="{w}" height="{TILE_H}" rx="12" fill="#0f172a" stroke="{GRID}"/>\n'
         f'    <text x="20" y="44" class="t" font-size="32" font-weight="700" fill="{INK}">{esc(value)}</text>\n'
         f'    <text x="20" y="72" class="t" font-size="16" fill="{INK2}">{esc(label)}</text>\n')
    if sub:
        s += f'    <text x="20" y="94" class="m" font-size="13" fill="{INK3}">{esc(sub)}</text>\n'
    return s + "  </g>\n"


def top_rounded_bar(x: float, base: float, w: float, h: float, fill: str, r: float = 4) -> str:
    """Vertical bar anchored to the baseline, rounded only at the data end."""
    if h <= 0:
        return ""
    r = min(r, h, w / 2)
    y = base - h
    return (f'<path d="M{x:.1f},{base:.1f} V{y+r:.1f} Q{x:.1f},{y:.1f} {x+r:.1f},{y:.1f} '
            f'H{x+w-r:.1f} Q{x+w:.1f},{y:.1f} {x+w:.1f},{y+r:.1f} V{base:.1f} Z" fill="{fill}"/>')


def right_rounded_bar(x: float, y: float, w: float, h: float, fill: str, r: float = 4) -> str:
    """Horizontal bar anchored left, rounded only at the data end."""
    w = max(w, 2)
    r = min(r, w, h / 2)
    return (f'<path d="M{x:.1f},{y:.1f} H{x+w-r:.1f} Q{x+w:.1f},{y:.1f} {x+w:.1f},{y+r:.1f} '
            f'V{y+h-r:.1f} Q{x+w:.1f},{y+h:.1f} {x+w-r:.1f},{y+h:.1f} H{x:.1f} Z" fill="{fill}"/>')


def month_label(ym: str) -> str:
    return dt.date(int(ym[:4]), int(ym[5:]), 1).strftime("%b")


def render_github(g: dict) -> str:
    years = g["contributions_by_year"]
    active = {y: v for y, v in years.items() if v >= 10}
    body = header(48, 56, "GITHUB · LIVE FROM THE GRAPHQL API",
                  "By the numbers", f"updated {g['generated_at']}")

    tiles = [
        (f"{g['contributions']:,}", "contributions", f"since {min(active) if active else g['member_since'][:4]}"),
        (f"{g['commits']:,}", "commits", f"{g['prs']} PRs · {g['issues']} issues"),
        (f"{g['repos']}", "repositories", f"{g['repos_private']} private"),
        (f"{g['orgs']}", "organizations", f"commits in {g['contributed_to']} repos"),
        (f"★ {g['stars']}", "stars", f"{g['forks']} forks"),
    ]
    tw, gap = 208, 16
    for i, (v, l, s) in enumerate(tiles):
        body += tile(48 + i * (tw + gap), 124, tw, v, l, s)

    # Contributions per year: plain text row (range spans 3 orders of magnitude,
    # bars would hide the early years).
    yx = 48
    body += f'  <text x="{yx}" y="282" class="m" font-size="14" letter-spacing="2" fill="{INK3}">PER YEAR</text>\n'
    xs = yx + 110
    for y, v in sorted(active.items()):
        body += (f'  <text x="{xs}" y="282" class="m" font-size="15" fill="{INK2}">{y}</text>'
                 f'<text x="{xs+46}" y="282" class="t" font-size="16" font-weight="700" fill="{INK}">{v:,}</text>\n')
        xs += 46 + 12 * len(f"{v:,}") + 40

    # Languages: 100% stacked bar, top 6 + Other, fixed categorical order.
    langs = g["languages"]
    top, rest = langs[:6], sum(l["bytes"] for l in langs[6:])
    total = sum(l["bytes"] for l in langs) or 1
    segs = [(l["name"], l["bytes"]) for l in top] + ([("Other", rest)] if rest else [])
    body += f'  <text x="48" y="332" class="m" font-size="14" letter-spacing="2" fill="{INK3}">LANGUAGES · {g["language_count"]} DETECTED · BY BYTES ACROSS ALL REPOS</text>\n'
    bx, by, bw, bh = 48, 348, 1104, 18
    x = bx
    body += f'  <clipPath id="lc"><rect x="{bx}" y="{by}" width="{bw}" height="{bh}" rx="6"/></clipPath>\n  <g clip-path="url(#lc)">\n'
    for i, (n, b) in enumerate(segs):
        w = bw * b / total
        body += f'    <rect x="{x:.1f}" y="{by}" width="{max(w-2, 1):.1f}" height="{bh}" fill="{SERIES[i]}"/>\n'
        x += w
    body += "  </g>\n"
    lx, ly = 48, 400
    for i, (n, b) in enumerate(segs):
        label = f"{n} {b/total*100:.1f}%"
        if lx + 20 + len(label) * 8.6 > 1152:
            lx, ly = 48, ly + 30
        body += (f'  <rect x="{lx}" y="{ly-12}" width="12" height="12" rx="3" fill="{SERIES[i]}"/>'
                 f'<text x="{lx+20}" y="{ly-1}" class="t" font-size="16" fill="{INK2}">'
                 f'<tspan fill="{INK}" font-weight="600">{esc(n)}</tspan> {b/total*100:.1f}%</text>\n')
        lx += 20 + int(len(label) * 8.6) + 30
    return frame(1200, 440 if ly < 420 else 470, f"GitHub stats for {g['login']}: {g['contributions']:,} contributions, "
                 f"{g['commits']:,} commits, {g['repos']} repositories, {g['stars']} stars", body)


def render_ai(a: dict) -> str:
    c, cx = a["claude"], a["codex"]
    body = header(48, 56, "AI USAGE · MEASURED FROM MY OWN LOGS",
                  "AI-native since ChatGPT's first day", f"updated {a['generated_at']}")

    # Left column: headline number + tiles.
    body += (f'  <text x="48" y="196" class="t" font-size="84" font-weight="800" fill="url(#hero)">{human(c["total"])}</text>\n'
             f'  <text x="48" y="232" class="t" font-size="19" fill="{INK2}">tokens through Claude Code · '
             f'{dt.date.fromisoformat(c["first_logged"]).strftime("%b %Y")} → {dt.date.fromisoformat(c["last_logged"]).strftime("%b %Y")}</text>\n')
    pm = c["peak_month"]
    pm_label = dt.date.fromisoformat(pm["month"] + "-01").strftime("%b %Y")
    tw = 262
    body += tile(48, 264, tw, human(pm["tokens"]), f"in {pm_label} alone", f"{pm['days']} days")
    body += tile(48 + tw + 16, 264, tw, human(c["tokens"]["output"]), "output tokens written")
    body += tile(48, 388, tw, f"{c['sessions']:,}", "agent sessions", f"{human(c['messages'])} msgs")
    body += tile(48 + tw + 16, 388, tw, f"{c['longest_session_days']} days", "longest single session",
                 f"{human(c['longest_session_messages'])} msgs")

    # Right column: tokens per month.
    px, pw, base, ph = 680, 472, 446, 250
    body += f'  <text x="{px}" y="150" class="m" font-size="14" letter-spacing="2" fill="{INK3}">TOKENS PER MONTH</text>\n'
    months = c["monthly"]
    vmax = max(m["tokens"] for m in months) if months else 1
    step = 40e9 if vmax > 80e9 else 10e9
    top_v = (int(vmax // step) + 1) * step
    ticks = [i * step for i in range(int(top_v // step) + 1)]
    for t in ticks:
        y = base - ph * t / top_v
        body += (f'  <line x1="{px+44}" x2="{px+pw}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID}"/>'
                 f'<text x="{px+36}" y="{y+5:.1f}" class="m" font-size="13" fill="{INK3}" text-anchor="end">{human(t) if t else 0}</text>\n')
    n = len(months)
    slot = (pw - 60) / max(n, 1)
    bw = min(44, slot * 0.6)
    today = dt.date.today().strftime("%Y-%m")
    for i, m in enumerate(months):
        x = px + 56 + i * slot + (slot - bw) / 2
        h = ph * m["tokens"] / top_v
        partial = m["month"] == today
        body += "  " + top_rounded_bar(x, base, bw, h, "url(#hatch)" if partial else BAR) + "\n"
        body += (f'  <text x="{x+bw/2:.1f}" y="{base+22}" class="t" font-size="15" fill="{INK2}" text-anchor="middle">{month_label(m["month"])}</text>'
                 f'<text x="{x+bw/2:.1f}" y="{base+40}" class="m" font-size="12" fill="{INK3}" text-anchor="middle">{m["days"]}d</text>\n')
        if m["month"] == pm["month"] or partial:
            tag = human(m["tokens"]) + (" so far" if partial else "")
            body += f'  <text x="{x+bw/2:.1f}" y="{base-h-10:.1f}" class="t" font-size="15" font-weight="700" fill="{INK}" text-anchor="middle">{tag}</text>\n'

    # Model mix: single-hue horizontal bars, direct labels.
    fy = 560
    body += f'  <text x="48" y="{fy}" class="m" font-size="14" letter-spacing="2" fill="{INK3}">MODEL MIX · FRONTIER MAIN AGENT + SUBAGENTS</text>\n'
    fams = [(k, v) for k, v in c["by_family"].items() if k != "other"]
    total = sum(v for _, v in fams) or 1
    colw = 270
    for i, (k, v) in enumerate(fams):
        x, y = 48 + i * colw, fy + 22
        pct = v / total * 100
        body += (f'  <text x="{x}" y="{y+14}" class="t" font-size="16" font-weight="600" fill="{INK}">{k.capitalize()}</text>'
                 f'<text x="{x+colw-24}" y="{y+14}" class="m" font-size="14" fill="{INK2}" text-anchor="end">{pct:.1f}% · {human(v)}</text>\n'
                 f'  <rect x="{x}" y="{y+24}" width="{colw-24}" height="10" rx="5" fill="{GRID}"/>'
                 f'  {right_rounded_bar(x, y+24, (colw-24)*v/total, 10, BAR, 5)}\n')
        vers = sorted((m.split(" ", 1)[1] for m in c["by_model"] if m.lower().startswith(k)),
                      key=lambda v: [int(p) for p in v.split(".")])
        body += f'  <text x="{x}" y="{y+56}" class="m" font-size="13" fill="{INK3}">{esc(" · ".join(vers))}</text>\n'
    models = list(c["by_model"])
    if cx["total"]:
        cm = [m for m in cx["by_model"] if m != "unknown"]
        body += (f'  <text x="48" y="{fy+108}" class="t" font-size="15" fill="{INK2}">'
                 f'{len(models)} Claude models, Opus as the main agent with Sonnet/Haiku subagents · '
                 f'+ {human(cx["total"])} via Codex since {dt.date.fromisoformat(cx["since"]).strftime("%b %Y")} '
                 f'({len(cm)} GPT models)</text>\n')
    share = c["tokens"]["cache_read"] / c["total"] * 100
    body += (f'  <text x="48" y="{fy+142}" class="t" font-size="13" fill="{INK3}">'
             f'Totals count input, output and cache tokens (cache reads are {share:.1f}%). Bars show days with logs; a hatched bar is the current month.</text>\n'
             f'  <text x="48" y="{fy+162}" class="t" font-size="13" fill="{INK3}">'
             f'Read from Claude Code and Codex logs on my main machine. ChatGPT use since 2022 was never metered, so it is not counted here.</text>\n')
    return frame(1200, 750, f"AI usage: {human(c['total'])} Claude Code tokens, peak {human(pm['tokens'])} in {pm_label}", body)


TIMELINE = [
    ("2021", "Rise", "SEO & web agency"),
    ("Nov 2022", "ChatGPT", "from day one"),
    ("2023", "Techsy.io", "co-founder & CTO"),
    ("Jul 2024", "Claude", "claude.ai"),
    ("Jul 2025", "Claude Code", "Max, agentic CLI"),
    ("Dec 2025", "Agent fleets", "Opus + subagents"),
    ("May 2026", "Codex", "GPT-5.5 → 6.1"),
    ("Oct 2026", "121B tokens", "in one month"),
]


def render_timeline(a: dict | None) -> str:
    items = list(TIMELINE)
    if a:  # keep the measured milestones in sync with the logs
        cc = a["claude"]
        items[3] = (dt.date.fromisoformat(cc["claude_account_since"]).strftime("%b %Y"), "Claude", "claude.ai")
        items[4] = (dt.date.fromisoformat(cc["claude_code_since"]).strftime("%b %Y"), "Claude Code", f"{cc['plan']}, agentic CLI")
        pm = cc["peak_month"]
        items[7] = (dt.date.fromisoformat(pm["month"] + "-01").strftime("%b %Y"), f"{human(pm['tokens'])} tokens", "in one month")
        if a["codex"]["since"]:
            items[6] = (dt.date.fromisoformat(a["codex"]["since"]).strftime("%b %Y"), "Codex", "GPT-5.5 → 6.1")
    body = header(48, 56, "AI TIMELINE", "Early, and never stopped")
    x0, x1, y = 96, 1104, 182
    body += f'  <line x1="{x0}" x2="{x1}" y1="{y}" y2="{y}" stroke="{GRID}" stroke-width="2"/>\n'
    step = (x1 - x0) / (len(items) - 1)
    for i, (when, what, sub) in enumerate(items):
        x = x0 + i * step
        last = i == len(items) - 1
        body += (f'  <circle cx="{x:.1f}" cy="{y}" r="{8 if last else 6}" fill="{SKY if last else "#0d1424"}" stroke="{SKY}" stroke-width="2"/>\n'
                 f'  <text x="{x:.1f}" y="{y-22}" class="m" font-size="14" fill="{SKY}" text-anchor="middle">{esc(when)}</text>\n'
                 f'  <text x="{x:.1f}" y="{y+36}" class="t" font-size="17" font-weight="700" fill="{INK}" text-anchor="middle">{esc(what)}</text>\n'
                 f'  <text x="{x:.1f}" y="{y+58}" class="t" font-size="14" fill="{INK2}" text-anchor="middle">{esc(sub)}</text>\n')
    return frame(1200, 270, "AI timeline: " + "; ".join(f"{w} {t}" for w, t, _ in items), body)


def render_tribute() -> str | None:
    photo = ASSETS / "john-mccarthy.jpg"
    if not photo.exists():
        return None
    b64 = base64.b64encode(photo.read_bytes()).decode()
    body = f"""  <clipPath id="ph"><rect x="40" y="40" width="240" height="280" rx="14"/></clipPath>
  <filter id="gray"><feColorMatrix type="saturate" values="0"/></filter>
  <image href="data:image/jpeg;base64,{b64}" x="40" y="40" width="240" height="280" preserveAspectRatio="xMidYMid slice" clip-path="url(#ph)" filter="url(#gray)"/>
  <rect x="40.5" y="40.5" width="239" height="279" rx="13.5" fill="none" stroke="{GRID}"/>
  <text x="330" y="78" class="m" font-size="15" letter-spacing="3" fill="{SKY}">STANDING ON THE SHOULDERS OF</text>
  <text x="330" y="122" class="t" font-size="36" font-weight="700" fill="{INK}">John McCarthy <tspan fill="{INK3}" font-weight="400" font-size="24">1927–2011</tspan></text>
  <text x="330" y="160" class="t" font-size="18" fill="{INK2}">Named the field “artificial intelligence” (Dartmouth, 1955) · created Lisp (1958)</text>
  <text x="330" y="186" class="t" font-size="18" fill="{INK2}">pioneered time-sharing and garbage collection · Turing Award 1971</text>
  <text x="330" y="246" class="t" font-size="30" font-style="italic" fill="{INK}">“He who refuses to do arithmetic</text>
  <text x="330" y="284" class="t" font-size="30" font-style="italic" fill="{INK}">is doomed to talk nonsense.”</text>
  <text x="1160" y="320" class="t" font-size="15" fill="{INK3}" text-anchor="end">That's why the numbers below come from logs and APIs, not memory.</text>
"""
    return frame(1200, 360, "Tribute to John McCarthy, who coined the term artificial intelligence", body)


# ------------------------------------------------- README table views ----

def splice(text: str, name: str, content: str) -> str:
    start, end = f"<!-- {name}:START -->", f"<!-- {name}:END -->"
    if start not in text or end not in text:
        return text
    head, rest = text.split(start, 1)
    return head + start + "\n" + content.strip() + "\n" + end + rest.split(end, 1)[1]


def table_ai(a: dict) -> str:
    c, cx = a["claude"], a["codex"]
    rows = "\n".join(
        f"| {dt.date.fromisoformat(m['month'] + '-01').strftime('%b %Y')} | {human(m['tokens'])} | {m['days']} |"
        for m in c["monthly"]
    )
    fams = "\n".join(
        f"| {k.capitalize()} | {human(v)} | {v / c['total'] * 100:.1f}% |"
        for k, v in c["by_family"].items() if k != "other"
    )
    return f"""<details>
<summary>Same data as a table</summary>

| Month | Claude Code tokens | Days logged |
|---|--:|--:|
{rows}

Plus {human(c['unsplit_before'])} tokens from {dt.date.fromisoformat(c['first_logged']).strftime('%b %Y')} until daily logs began, not split by month.
Busiest day: {human(c['peak_day']['tokens'])} tokens on {c['peak_day']['date']}.

| Model family | Tokens | Share |
|---|--:|--:|
{fams}

| Token type | Count |
|---|--:|
| Cache reads | {human(c['tokens']['cache_read'])} |
| Cache writes | {human(c['tokens']['cache_write'])} |
| Output | {human(c['tokens']['output'])} |
| Fresh input | {human(c['tokens']['input'])} |
| Codex (GPT) total | {human(cx['total'])} |

</details>"""


def table_github(g: dict) -> str:
    years = " · ".join(f"{y}: {v:,}" for y, v in g["contributions_by_year"].items() if v >= 10)
    langs = " · ".join(f"{l['name']} {l['pct']:.1f}%" for l in g["languages"][:12])
    return f"""<details>
<summary>Same data as text</summary>

{g['contributions']:,} contributions ({years}) · {g['commits']:,} commits · {g['prs']} PRs · {g['issues']} issues ·
{g['repos']} repositories ({g['repos_private']} private) across {g['orgs']} organizations and my account · ★ {g['stars']} stars.
Top languages by bytes: {langs}.

</details>"""


def update_readme(g: dict | None, a: dict | None) -> None:
    readme = ROOT / "README.md"
    if not readme.exists():
        return
    t = readme.read_text()
    if a:
        t = splice(t, "AI-TABLE", table_ai(a))
    if g:
        t = splice(t, "GITHUB-TABLE", table_github(g))
    readme.write_text(t)


# ------------------------------------------------------------------ main ----

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--github", action="store_true")
    ap.add_argument("--ai", action="store_true")
    ap.add_argument("--render", action="store_true")
    args = ap.parse_args()
    if not (args.github or args.ai or args.render):
        args.render = True
    DATA.mkdir(exist_ok=True)
    ASSETS.mkdir(exist_ok=True)

    if args.github:
        (DATA / "github.json").write_text(json.dumps(collect_github(), indent=2) + "\n")
        args.render = True
    if args.ai:
        (DATA / "ai.json").write_text(json.dumps(collect_ai(), indent=2) + "\n")
        args.render = True
    if args.render:
        g = DATA / "github.json"
        a = DATA / "ai.json"
        ai = json.loads(a.read_text()) if a.exists() else None
        gh = json.loads(g.read_text()) if g.exists() else None
        if gh:
            (ASSETS / "github-stats.svg").write_text(render_github(gh))
        update_readme(gh, ai)
        if ai:
            (ASSETS / "ai-usage.svg").write_text(render_ai(ai))
        (ASSETS / "ai-timeline.svg").write_text(render_timeline(ai))
        t = render_tribute()
        if t:
            (ASSETS / "mccarthy.svg").write_text(t)
        print("rendered:", ", ".join(p.name for p in sorted(ASSETS.glob("*.svg"))))


if __name__ == "__main__":
    sys.exit(main())
