"""Refresh data/*.json from England Hockey's public team and league pages.

England Hockey's pages fill in fixtures and tables with JavaScript, so this
opens each page in a headless browser (Playwright), waits for the data to
appear and reads it from the page. It visits ~50 pages with a pause between
each, twice a week. If a page fails, that team's or league's previous data
is kept, so a bad run never wipes the site.
"""
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
BASE = "https://www.englandhockey.co.uk"
PAUSE = 2.5  # seconds between page loads, to be gentle on England Hockey
ADULT_GROUPS = {"Men", "Women"}  # line-ups and scorers are collected for adult teams only
MATCH_JS = (Path(__file__).resolve().parent / "match_parser.js").read_text()

TEAM_JS = r"""() => {
  let comp = null, compHref = null; const out = [];
  for (const el of document.querySelectorAll('.js-team-fixtures .c-ribbon, .js-team-fixtures .c-fixture')) {
    if (el.classList.contains('c-ribbon')) { const a = el.querySelector('a'); comp = el.innerText.trim(); compHref = a ? a.getAttribute('href') : null; continue; }
    const lab = (s) => { const a = el.querySelector(s + ' .c-badge__label'); return a ? a.innerText.trim() : null; };
    const home = lab('.c-fixture__badge-before'), away = lab('.c-fixture__badge-after');
    const scores = [...el.querySelectorAll('.c-score__item')].map((s) => s.innerText.trim());
    const sb = el.querySelector('.c-fixture__score-board');
    const date = (el.querySelector('.u-text-tiny') || {}).innerText;
    const lines = el.innerText.split('\n').map((s) => s.trim()).filter(Boolean);
    const known = new Set([date, home, away, ...scores, sb && sb.innerText.trim()].filter(Boolean));
    const extra = lines.filter((l) => !known.has(l) && !/View Match Details/i.test(l));
    const cont = el.closest('.c-match-detail-card__container') || el.parentElement;
    const link = cont.querySelector('a[href^="/fixtures/"]');
    const fx = link ? link.getAttribute('href').split('/').pop() : null;
    out.push({ comp, compHref, date, home, away, scores, extra, fx });
  }
  return out;
}"""

TABLE_JS = r"""() => {
  const t = document.querySelector('main table');
  if (!t) return [];
  return [...t.querySelectorAll('tbody tr')].map((tr) => [...tr.children].map((td) => td.innerText.trim().replace(/\s+/g, ' ')));
}"""

MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6, "Jul": 7, "Aug": 8,
          "Sep": 9, "Sept": 9, "Oct": 10, "Nov": 11, "Dec": 12}


def iso(date_text):
    d, m, y = date_text.split()
    return dt.date(int(y), MONTHS[m], int(d)).isoformat()


def weekend(iso_date):
    d = dt.date.fromisoformat(iso_date)
    wd = d.weekday()  # Mon 0 .. Sun 6
    sat = d - dt.timedelta(days=1) if wd == 6 else d + dt.timedelta(days=5 - wd)
    return sat.isoformat()


def fixture_rows(team, raw):
    """Turn one team's scraped fixtures into dashboard rows."""
    rows, seen = [], {}
    for f in raw:
        if not f.get("date") or not f.get("home") or not f.get("away"):
            continue
        date = iso(f["date"])
        key = f"{team['team']}|{date}"
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            key += f"|{seen[key]}"
        time_, venue, notes = None, None, []
        for x in f.get("extra") or []:
            for part in [p.strip() for p in x.split(" / ")]:
                if re.fullmatch(r"\d{1,2}:\d{2}", part):
                    time_ = None if part in ("00:00", "0:00") else part
                elif part in ("Walkover", "Postponed", "Cancelled", "Abandoned") or part.startswith("Shoot-out"):
                    notes.append(part)
                elif part:
                    venue = part
        sc = f.get("scores") or []
        hs = as_ = None
        status = "fixture"
        if len(sc) == 2 and all(re.fullmatch(r"\d+", s) for s in sc):
            hs, as_ = int(sc[0]), int(sc[1])
            status = "result"
        elif len(sc) == 2 and sc[0] == "P":
            status = "postponed"
        if f["home"].startswith("Bye") or f["away"].startswith("Bye"):
            status = "bye"
        home_side = f["home"] == team["clubName"]
        opp = f["away"] if home_side else f["home"]
        us = them = result = None
        if status == "result":
            us, them = (hs, as_) if home_side else (as_, hs)
            result = "W" if us > them else "L" if us < them else "D"
        rows.append({
            "key": key, "team": team["team"], "teamLabel": team["label"], "group": team["group"], "order": team["order"],
            "competition": f.get("comp"), "league": f.get("compHref") if f.get("compHref") == team.get("league") else None,
            "date": date, "weekend": weekend(date), "time": time_,
            "home": f["home"], "away": f["away"], "homeScore": hs, "awayScore": as_,
            "ha": "H" if home_side else "A", "opponent": opp, "beestonScore": us, "opponentScore": them,
            "score": f"{us}–{them}" if us is not None else None,
            "result": result, "status": status, "venue": venue, "note": " / ".join(notes) or None,
            "fixtureId": f.get("fx"),
        })
    return rows


def table_rows(league, league_name, raw):
    out = []
    for cells in raw:
        if len(cells) < 10 or not cells[0].isdigit():
            continue
        n = [int(c.replace("−", "-")) for c in cells[2:10]]
        out.append({"league": league, "leagueName": league_name, "pos": int(cells[0]), "team": cells[1],
                    "played": n[0], "won": n[1], "drawn": n[2], "lost": n[3],
                    "goalsFor": n[4], "goalsAgainst": n[5], "goalDiff": n[6], "points": n[7],
                    "isBeeston": cells[1].startswith("Beeston")})
    return out


def load_page(page, url, ready_js, attempts=3):
    for i in range(attempts):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_function(ready_js, timeout=30000)
            return True
        except Exception as e:  # spinner never cleared, timeout, network blip
            print(f"  retry {i + 1} for {url}: {e.__class__.__name__}", flush=True)
            time.sleep(30 * (i + 1))
    return False


def collect_matches(page, teams, fixtures):
    """Fetch line-ups and scorers for completed adult matches we don't have yet."""
    path = DATA / "matches.json"
    old = json.loads(path.read_text()) if path.exists() else []
    have = {m["id"]: m for m in old}
    by_id = {t["team"]: t for t in teams}
    wanted = {}
    for r in fixtures:
        t = by_id.get(r["team"])
        if not t or t["group"] not in ADULT_GROUPS or r["status"] != "result" or not r.get("fixtureId"):
            continue
        wanted.setdefault(r["fixtureId"], []).append(r)
    today = dt.date.today()
    new = 0
    for fid, rows in sorted(wanted.items(), key=lambda kv: kv[1][0]["date"]):
        prev = have.get(fid)
        recent = (today - dt.date.fromisoformat(rows[0]["date"])).days <= 14
        # Keep what we have; re-check recent matches whose line-ups were missing or incomplete
        if prev and (prev["teams"] or not recent):
            continue
        try:
            page.goto(f"{BASE}/fixtures/{fid}", wait_until="domcontentloaded", timeout=45000)
            parsed = page.evaluate(f"({MATCH_JS})(document)")
        except Exception as e:
            print(f"  match {fid} failed: {e.__class__.__name__}", flush=True)
            continue
        r0 = rows[0]
        # Which Beeston team each Beeston side is (matters for Beeston v Beeston and men/women "Beeston 2")
        beeston = {}
        for side in parsed["teams"]:
            for r in rows:
                t = by_id[r["team"]]
                if side["name"] == t["clubName"]:
                    beeston[side["side"]] = t["team"]
        have[fid] = {
            "id": fid, "date": r0["date"], "weekend": r0["weekend"], "competition": r0["competition"],
            "home": r0["home"], "away": r0["away"], "venue": r0["venue"],
            "homeScore": parsed["homeScore"] if parsed["homeScore"] is not None else r0["homeScore"],
            "awayScore": parsed["awayScore"] if parsed["awayScore"] is not None else r0["awayScore"],
            "note": r0["note"], "beeston": beeston, "teams": parsed["teams"],
        }
        new += 1
        time.sleep(1.5)
    return sorted(have.values(), key=lambda m: (m["date"], m["id"])), new


def main():
    teams = json.loads((DATA / "teams.json").read_text())
    old_fx = json.loads((DATA / "fixtures.json").read_text())
    old_tb = json.loads((DATA / "league-tables.json").read_text())
    new_fx, new_tb, failed = [], [], []

    team_ready = "() => { const el = document.querySelector('.js-team-fixtures'); return el && !el.querySelector('.c-loader'); }"
    table_ready = "() => !!document.querySelector('main table tbody tr')"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(locale="en-GB", timezone_id="Europe/London",
                                  user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
        page = ctx.new_page()

        for t in teams:
            url = f"{BASE}/teams/{t['team']}"
            print("team", t["team"], flush=True)
            if load_page(page, url, team_ready):
                new_fx += fixture_rows(t, page.evaluate(TEAM_JS))
            else:
                failed.append(t["label"])
                new_fx += [r for r in old_fx if r["team"] == t["team"]]
            time.sleep(PAUSE)

        leagues = {}
        for t in teams:
            if t.get("league"):
                leagues.setdefault(t["league"], t["leagueName"])
        for href, name in leagues.items():
            print("table", href, flush=True)
            rows = table_rows(href, name, page.evaluate(TABLE_JS)) if load_page(page, BASE + href + "/table", table_ready) else []
            if rows:
                new_tb += rows
            else:
                failed.append(name)
                new_tb += [r for r in old_tb if r["league"] == href]
            time.sleep(PAUSE)

        matches, match_count = collect_matches(page, teams, new_fx)
        browser.close()

    # Safety: never replace good data with an obviously broken scrape.
    if len(new_fx) < 0.8 * len(old_fx) or len(new_tb) < 0.8 * len(old_tb):
        print(f"Scrape looks incomplete ({len(new_fx)} fixtures, {len(new_tb)} table rows); keeping old data.")
        sys.exit(1)

    new_fx.sort(key=lambda r: (r["date"], r["team"]))
    (DATA / "fixtures.json").write_text(json.dumps(new_fx, ensure_ascii=False))
    (DATA / "league-tables.json").write_text(json.dumps(new_tb, ensure_ascii=False))
    meta = {"updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": BASE + "/clubs/beeston-hc", "season": "2026/27", "failed": failed}
    (DATA / "meta.json").write_text(json.dumps(meta))
    (DATA / "matches.json").write_text(json.dumps(matches, ensure_ascii=False))
    print(f"Matches with line-ups: {len(matches)} ({match_count} new this run)")
    results = sum(1 for r in new_fx if r["status"] == "result")
    print(f"Done: {len(new_fx)} fixtures ({results} results), {len(new_tb)} table rows. Failed: {failed or 'none'}")


if __name__ == "__main__":
    main()
