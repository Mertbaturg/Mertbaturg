#!/usr/bin/env python3
"""Build the animated SVG cards for github.com/Mertbaturg.

  --github   GitHub GraphQL API -> data/github.json (contributions per year,
             repos across personal + org accounts, stars, language bytes).
             Token: GH_TOKEN / GITHUB_TOKEN, else `gh auth token`.
  --render   data/*.json -> assets/*.svg (default when no flag is given)

data/ai.json is edited by hand (lifetime token totals, timeline, AI lab).
Stdlib only, Python 3.9+.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
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

SKIP_OWNERS = {"code50"}  # coursework sandboxes

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
        repositoriesContributedTo(first: 1, contributionTypes: [COMMIT, PULL_REQUEST, REPOSITORY]) { totalCount } } }""")["viewer"]
    login = me["login"]
    orgs = [o["login"] for o in me["organizations"]["nodes"]]

    now = dt.datetime.now(dt.timezone.utc)
    by_year, totals = {}, collections.Counter()
    for year in range(int(me["createdAt"][:4]), now.year + 1):
        end = min(dt.datetime(year, 12, 31, 23, 59, 59, tzinfo=dt.timezone.utc), now)
        c = gql(
            """query($login: String!, $from: DateTime!, $to: DateTime!) {
              user(login: $login) { contributionsCollection(from: $from, to: $to) {
                totalCommitContributions totalPullRequestContributions
                totalIssueContributions totalPullRequestReviewContributions
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


# ---------------------------------------------------------------- render ----

FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
INK = "#f0f6fc"        # primary text
INK2 = "#a8b3c4"       # secondary text
INK3 = "#7c8799"       # muted text / axis
GRID = "#1e293b"
TILE = "#0f172a"
SKY, VIOLET = "#7dd3fc", "#a78bfa"   # brand accents (decorative only)
# Validated categorical palette (dark mode, surface #0d1117): all checks pass.
SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9"]
BAR = SERIES[0]
EASE = 'calcMode="spline" keyTimes="0;1" keySplines=".22 1 .36 1"'


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


def fade(begin: float, dur: float = .6) -> str:
    """Fade + rise in. Element must start with opacity="0"."""
    return (f'<animate attributeName="opacity" from="0" to="1" begin="{begin:.2f}s" dur="{dur}s" fill="freeze"/>'
            f'<animateTransform attributeName="transform" type="translate" from="0 8" to="0 0" '
            f'begin="{begin:.2f}s" dur="{dur}s" fill="freeze" additive="sum" {EASE}/>')


def frame(w: int, h: int, label: str, body: str) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="{esc(label)}">
  <title>{esc(label)}</title>
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#070b14"/><stop offset="1" stop-color="#0d1424"/>
    </linearGradient>
    <linearGradient id="hero" gradientUnits="userSpaceOnUse" x1="0" x2="600">
      <stop offset="0" stop-color="{INK}"/><stop offset=".45" stop-color="{SKY}"/>
      <stop offset=".75" stop-color="{VIOLET}"/><stop offset="1" stop-color="{INK}"/>
      <animateTransform attributeName="gradientTransform" type="translate" values="-300 0; 300 0; -300 0" dur="10s" repeatCount="indefinite"/>
    </linearGradient>
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


def eyebrow(x: int, y: int, text: str, right: str = "", w: int = 1200) -> str:
    out = f'  <text x="{x}" y="{y}" class="m" font-size="15" letter-spacing="3" fill="{SKY}">{esc(text)}</text>\n'
    if right:
        out += f'  <text x="{w-x}" y="{y}" class="m" font-size="14" fill="{INK3}" text-anchor="end">{esc(right)}</text>\n'
    return out


def tile(x: int, y: int, w: int, value: str, label: str, sub: str = "", begin: float = 0) -> str:
    s = (f'  <g transform="translate({x},{y})" opacity="0">{fade(begin)}\n'
         f'    <rect width="{w}" height="110" rx="12" fill="{TILE}" stroke="{GRID}"/>\n'
         f'    <text x="20" y="44" class="t" font-size="32" font-weight="700" fill="{INK}">{esc(value)}</text>\n'
         f'    <text x="20" y="72" class="t" font-size="16" fill="{INK2}">{esc(label)}</text>\n')
    if sub:
        s += f'    <text x="20" y="94" class="m" font-size="13" fill="{INK3}">{esc(sub)}</text>\n'
    return s + "  </g>\n"


# --- GitHub card ------------------------------------------------------------

def render_github(g: dict) -> str:
    active = {y: v for y, v in g["contributions_by_year"].items() if v >= 10}
    body = eyebrow(48, 56, "GITHUB · LIVE FROM THE GRAPHQL API", f"updated {g['generated_at']}")
    body += f'  <text x="48" y="96" class="t" font-size="32" font-weight="700" fill="{INK}">By the numbers</text>\n'
    tiles = [
        (f"{g['contributions']:,}", "contributions", f"since {min(active) if active else g['member_since'][:4]}"),
        (f"{g['commits']:,}", "commits", f"{g['prs']} PRs · {g['issues']} issues"),
        (f"{g['repos']}", "repositories", f"{g['repos_private']} private"),
        (f"{g['orgs']}", "organizations", f"commits in {g['contributed_to']} repos"),
        (f"★ {g['stars']}", "stars", f"{g['forks']} forks"),
    ]
    tw, gap = 208, 16
    for i, (v, l, s) in enumerate(tiles):
        body += tile(48 + i * (tw + gap), 124, tw, v, l, s, begin=.1 + i * .08)

    body += f'  <text x="48" y="282" class="m" font-size="14" letter-spacing="2" fill="{INK3}">PER YEAR</text>\n'
    xs = 158
    for y, v in sorted(active.items()):
        body += (f'  <text x="{xs}" y="282" class="m" font-size="15" fill="{INK2}">{y}</text>'
                 f'<text x="{xs+46}" y="282" class="t" font-size="16" font-weight="700" fill="{INK}">{v:,}</text>\n')
        xs += 46 + 12 * len(f"{v:,}") + 40

    langs = g["languages"]
    top, rest = langs[:6], sum(l["bytes"] for l in langs[6:])
    total = sum(l["bytes"] for l in langs) or 1
    segs = [(l["name"], l["bytes"]) for l in top] + ([("Other", rest)] if rest else [])
    body += f'  <text x="48" y="332" class="m" font-size="14" letter-spacing="2" fill="{INK3}">LANGUAGES · {g["language_count"]} DETECTED</text>\n'
    bx, by, bw, bh = 48, 348, 1104, 18
    body += (f'  <clipPath id="lc"><rect x="{bx}" y="{by}" width="0" height="{bh}" rx="6">'
             f'<animate attributeName="width" from="0" to="{bw}" begin=".5s" dur="1.4s" fill="freeze" {EASE}/></rect></clipPath>\n'
             f'  <g clip-path="url(#lc)">\n')
    x = bx
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
        body += (f'  <g opacity="0">{fade(1.2 + i * .06)}<rect x="{lx}" y="{ly-12}" width="12" height="12" rx="3" fill="{SERIES[i]}"/>'
                 f'<text x="{lx+20}" y="{ly-1}" class="t" font-size="16" fill="{INK2}">'
                 f'<tspan fill="{INK}" font-weight="600">{esc(n)}</tspan> {b/total*100:.1f}%</text></g>\n')
        lx += 20 + int(len(label) * 8.6) + 30
    return frame(1200, 440 if ly < 420 else 470, f"GitHub stats for {g['login']}: {g['contributions']:,} contributions, "
                 f"{g['commits']:,} commits, {g['repos']} repositories, {g['stars']} stars", body)


# --- AI tokens card ---------------------------------------------------------

def render_ai(a: dict) -> str:
    prov = sorted(a["providers"], key=lambda p: -p["tokens"])
    total = sum(p["tokens"] for p in prov)
    body = eyebrow(48, 56, "AI TOKENS · LIFETIME")

    # Left: headline + three stats.
    body += (f'  <g opacity="0">{fade(.1, .8)}'
             f'<text x="44" y="190" class="t" font-size="120" font-weight="800" fill="url(#hero)">{human(total)}</text>'
             f'<text x="48" y="232" class="t" font-size="20" fill="{INK2}">tokens since {esc(a["since"])}</text></g>\n')
    stats = [(human(a["claude_monthly_avg"]), "Claude / month"), (str(a["labs"]), "AI labs"),
             (str(a["claude_models"]), "Claude models")]
    for i, (v, l) in enumerate(stats):
        x = 48 + i * 140
        body += (f'  <g opacity="0">{fade(.5 + i * .1)}'
                 f'<rect x="{x}" y="280" width="128" height="92" rx="12" fill="{TILE}" stroke="{GRID}"/>'
                 f'<text x="{x+16}" y="322" class="t" font-size="28" font-weight="700" fill="{INK}">{esc(v)}</text>'
                 f'<text x="{x+16}" y="352" class="t" font-size="14" fill="{INK2}">{esc(l)}</text></g>\n')

    # Right: one bar per model family, single hue, direct labels.
    px, pw, y0, row = 520, 632, 92, 66
    vmax = prov[0]["tokens"]
    for i, p in enumerate(prov):
        y = y0 + i * row
        w = max(pw * p["tokens"] / vmax, 4)
        t = .3 + i * .12
        val = ("~" if p.get("approx") else "") + human(p["tokens"], 2)
        note = f'<tspan fill="{INK3}" font-weight="400" font-size="14" dx="10">{esc(p["note"])}</tspan>' if p.get("note") else ""
        body += (f'  <g opacity="0">{fade(t)}'
                 f'<text x="{px}" y="{y+18}" class="t" font-size="18" font-weight="700" fill="{INK}">{esc(p["name"])}{note}</text>'
                 f'<text x="{px+pw}" y="{y+18}" class="t" font-size="18" font-weight="700" fill="{INK}" text-anchor="end">{val}</text></g>\n'
                 f'  <rect x="{px}" y="{y+30}" width="{pw}" height="12" rx="6" fill="{GRID}"/>\n'
                 f'  <rect x="{px}" y="{y+30}" width="0" height="12" rx="6" fill="{BAR}">'
                 f'<animate attributeName="width" from="0" to="{w:.1f}" begin="{t+.1:.2f}s" dur="1.4s" fill="freeze" {EASE}/></rect>\n')
        if p.get("tag"):
            body += (f'  <g opacity="0">{fade(t + 1.2)}<text x="{px+pw}" y="{y+60}" class="m" font-size="13" fill="{SKY}" text-anchor="end">'
                     f'{esc(p["tag"])}</text></g>\n')
    label = f"Lifetime AI tokens: {human(total)} since {a['since']}. " + "; ".join(
        f"{p['name']} {human(p['tokens'])}" for p in prov)
    return frame(1200, 500, label, body)


# --- AI timeline card -------------------------------------------------------

def render_timeline(a: dict) -> str:
    items = a["timeline"]
    body = eyebrow(48, 56, "TIMELINE")
    body += f'  <text x="48" y="96" class="t" font-size="32" font-weight="700" fill="{INK}">{esc(a.get("timeline_title", ""))}</text>\n'
    x0, x1, y = 140, 1080, 196
    span = x1 - x0
    body += (f'  <line x1="{x0}" x2="{x1}" y1="{y}" y2="{y}" stroke="{GRID}" stroke-width="2"/>\n'
             f'  <line x1="{x0}" x2="{x1}" y1="{y}" y2="{y}" stroke="{SKY}" stroke-width="2" stroke-dasharray="{span}" stroke-dashoffset="{span}">'
             f'<animate attributeName="stroke-dashoffset" from="{span}" to="0" begin=".2s" dur="2.4s" fill="freeze" {EASE}/></line>\n')
    step = span / (len(items) - 1)
    for i, (when, what, *rest) in enumerate(items):
        sub = rest[0] if rest else ""
        x = x0 + i * step
        t = .2 + 2.4 * i / (len(items) - 1) * .8
        last = i == len(items) - 1
        body += (f'  <g opacity="0">{fade(t, .5)}'
                 f'<circle cx="{x:.1f}" cy="{y}" r="{8 if last else 6}" fill="{SKY if last else "#0d1424"}" stroke="{SKY}" stroke-width="2"/>'
                 f'<text x="{x:.1f}" y="{y-22}" class="m" font-size="14" fill="{SKY}" text-anchor="middle">{esc(when)}</text>'
                 f'<text x="{x:.1f}" y="{y+38}" class="t" font-size="17" font-weight="700" fill="{INK}" text-anchor="middle">{esc(what)}</text>'
                 f'<text x="{x:.1f}" y="{y+60}" class="t" font-size="14" fill="{INK2}" text-anchor="middle">{esc(sub)}</text></g>\n')
        if last:
            body += (f'  <circle cx="{x:.1f}" cy="{y}" r="8" fill="none" stroke="{SKY}" stroke-width="2" opacity="0">'
                     f'<animate attributeName="r" values="8;22" begin="{t+.5:.2f}s" dur="2s" repeatCount="indefinite"/>'
                     f'<animate attributeName="opacity" values=".7;0" begin="{t+.5:.2f}s" dur="2s" repeatCount="indefinite"/></circle>\n')
    return frame(1200, 300, "Timeline: " + "; ".join(f"{i[0]} {i[1]}" + (f" ({i[2]})" if len(i) > 2 and i[2] else "") for i in items), body)


# --- AI lab card ------------------------------------------------------------

def render_lab(a: dict) -> str:
    body = eyebrow(48, 56, "AI LAB · MODELS TESTED ACROSS GPUS")
    cols = list(a["lab"].items())
    colw, gap, top = 352, 24, 92
    t = .1
    max_y = top
    for c, (title, chips) in enumerate(cols):
        x0 = 48 + c * (colw + gap)
        body += (f'  <g opacity="0">{fade(t)}<text x="{x0}" y="{top+20}" class="t" font-size="20" font-weight="700" fill="{INK}">{esc(title)}</text>'
                 f'<text x="{x0+colw}" y="{top+20}" class="m" font-size="14" fill="{INK3}" text-anchor="end">{len(chips)}</text></g>\n')
        x, y = x0, top + 40
        for chip in chips:
            w = int(len(chip) * 8.4) + 28
            if x + w > x0 + colw:
                x, y = x0, y + 46
            t += .05
            body += (f'  <g opacity="0">{fade(t, .4)}<rect x="{x}" y="{y}" width="{w}" height="36" rx="18" fill="{TILE}" stroke="{GRID}"/>'
                     f'<text x="{x+w/2}" y="{y+24}" class="t" font-size="15" fill="{INK2}" text-anchor="middle">{esc(chip)}</text></g>\n')
            x += w + 10
        max_y = max(max_y, y + 36)
    my = max_y + 48
    metrics = "  ·  ".join(a.get("lab_metrics", []))
    body += (f'  <line x1="48" x2="1152" y1="{my-24}" y2="{my-24}" stroke="{GRID}"/>\n'
             f'  <g opacity="0">{fade(t + .2)}<text x="48" y="{my+4}" class="m" font-size="14" letter-spacing="1" fill="{INK3}">'
             f'BENCHMARKED  <tspan fill="{INK2}">{esc(metrics)}</tspan></text></g>\n')
    label = "AI lab: " + "; ".join(f"{k}: {', '.join(v)}" for k, v in cols)
    return frame(1200, my + 36, label, body)


# ------------------------------------------------------------------ main ----

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--github", action="store_true")
    ap.add_argument("--render", action="store_true")
    args = ap.parse_args()
    DATA.mkdir(exist_ok=True)
    ASSETS.mkdir(exist_ok=True)

    if args.github:
        (DATA / "github.json").write_text(json.dumps(collect_github(), indent=2) + "\n")
    g, a = DATA / "github.json", DATA / "ai.json"
    if g.exists():
        (ASSETS / "github-stats.svg").write_text(render_github(json.loads(g.read_text())))
    if a.exists():
        ai = json.loads(a.read_text())
        (ASSETS / "ai-usage.svg").write_text(render_ai(ai))
        (ASSETS / "ai-timeline.svg").write_text(render_timeline(ai))
        (ASSETS / "ai-lab.svg").write_text(render_lab(ai))
    print("rendered:", ", ".join(p.name for p in sorted(ASSETS.glob("*.svg"))))


if __name__ == "__main__":
    sys.exit(main())
