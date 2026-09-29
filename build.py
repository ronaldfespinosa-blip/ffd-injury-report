"""
Fantasy Football Doctor - injury report builder.

Runs on GitHub Actions on a schedule. Pulls:
  * Official NFL injury reports (game status + practice participation) via nflverse
  * Weekly roster status (for injured reserve) via nflverse
  * Depth charts and the season schedule via nflverse
  * FantasyPros expert consensus rankings via DynastyProcess
  * Sleeper's player feed, for injuries announced after games (a torn ACL on
    Sunday) that the official reports and roster moves don't carry yet
and writes data/injury-report.json, which the WordPress block reads.

Also reads data/return-timelines.json (typical games missed + re-injury risk,
editable by hand) and keeps data/practice-log.json (each day's practice status,
so the page can show a Wed/Thu/Fri trail).
"""
import csv, io, json, re, sys, unicodedata, urllib.request
from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo

SEASON = datetime.now(timezone.utc).year
NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download"
URLS = {
    "injuries": f"{NFLVERSE}/injuries/injuries_{SEASON}.csv",
    "rosters":  f"{NFLVERSE}/weekly_rosters/roster_weekly_{SEASON}.csv",
    "depth":    f"{NFLVERSE}/depth_charts/depth_charts_{SEASON}.csv",
    "games":    "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv",
    "ecr":      "https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_fpecr_latest.csv",
}
# Doc's own Top 200 lives on the Rankings page (the same list Doc's Top 10 on the homepage reads)
SLEEPER_URL = "https://api.sleeper.app/v1/players/nfl"
SLEEPER_STATUS = {"Out": "Out", "Doubtful": "Doubtful", "Questionable": "Questionable", "IR": "IR", "PUP": "PUP"}
SLEEPER_CACHE = "data/sleeper-injuries.json"
SLEEPER_HOUR_ET = 7   # Sleeper asks for this download at most once a day: first run after 7am ET
DOC_RANKINGS_URL = "https://fantasyfootballdoctor.com/wp-json/wp/v2/pages/36?_fields=content,modified"
RANK_PAGE = "/nfl/rankings/ros-ppr-overall.php"   # rest-of-season PPR consensus
DYNASTY_PAGE = "/nfl/rankings/dynasty-overall.php"  # keeps long-term injured players ROS drops
TOP_N = 200          # players on the weekly injury report
RESERVE_TOP_N = 250  # IR/PUP players: ROS top 250 OR dynasty top 250
RESERVE_CODES = {"R01": "IR", "R48": "IR", "R04": "PUP"}
OFFENSE = {"QB", "RB", "WR", "TE"}
# Page order: this week's decisions first, stashed IR/PUP players last
SEVERITY = {"Out": 0, "Doubtful": 1, "Questionable": 2, "Practicing": 3, "Cleared": 4, "IR": 5, "PUP": 5}
REPORT_STATUSES = {"Out", "Doubtful", "Questionable"}
OUT_PATH = "data/injury-report.json"
TIMELINES_PATH = "data/return-timelines.json"
LOG_PATH = "data/practice-log.json"
ET = ZoneInfo("America/New_York")
PRACTICE_CODES = {
    "Did Not Participate In Practice": "DNP",
    "Limited Participation in Practice": "LP",
    "Full Participation in Practice": "FP",
}


def fetch_csv(url):
    req = urllib.request.Request(url, headers={"User-Agent": "ffd-injury-report"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return list(csv.DictReader(io.StringIO(r.read().decode("utf-8"))))


def fetch_doc_top200():
    """Doc's Top 200 from the Rankings page: {(name, pos): rank}. Returns {} if it can't be read."""
    try:
        req = urllib.request.Request(DOC_RANKINGS_URL, headers={"User-Agent": "ffd-injury-report"})
        with urllib.request.urlopen(req, timeout=60) as r:
            html = json.loads(r.read().decode("utf-8"))["content"]["rendered"]
        m = re.search(r"var P\s*=\s*(\[\[[\s\S]*?\]\])\s*;", html)
        rows = json.loads(m.group(1)) if m else []
        return {(norm(row[1]), row[2]): int(row[0]) for row in rows if row[2] in OFFENSE}
    except Exception as err:
        print(f"Could not read Doc's Top 200 ({err}); falling back to FantasyPros.")
        return {}


def fetch_sleeper():
    """Sleeper players: {player_id: {...}}. Returns {} if it can't be read (the report still builds)."""
    try:
        req = urllib.request.Request(SLEEPER_URL, headers={"User-Agent": "ffd-injury-report"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as err:
        print(f"Could not read Sleeper ({err}); using the saved copy.")
        return {}


def sleeper_injuries():
    """Injured offensive players from Sleeper, downloaded once a day and saved in data/sleeper-injuries.json
    so the other runs reuse the saved copy (Sleeper asks for this download at most once a day)."""
    cache = load_json(SLEEPER_CACHE, {})
    now_et = datetime.now(ET)
    fetched = cache.get("fetched_et", "")[:10]
    due = fetched != now_et.date().isoformat() and (now_et.hour >= SLEEPER_HOUR_ET or not fetched)
    if not due:
        print(f"Sleeper: using saved copy from {cache.get('fetched_et')}.")
        return cache.get("players", [])
    raw = fetch_sleeper()
    if not raw:
        return cache.get("players", [])
    players = []
    for sp in raw.values():
        if not isinstance(sp, dict) or sp.get("position") not in OFFENSE or not sp.get("team"):
            continue
        if (sp.get("injury_status") or "") not in SLEEPER_STATUS:
            continue
        players.append({
            "id": (sp.get("gsis_id") or "").strip(), "sleeper_id": sp.get("player_id"),
            "name": sp.get("full_name") or f"{sp.get('first_name', '')} {sp.get('last_name', '')}".strip(),
            "pos": sp["position"], "team": sp["team"], "status": sp["injury_status"],
            "part": (sp.get("injury_body_part") or "").strip(),
        })
    with open(SLEEPER_CACHE, "w") as f:
        json.dump({"fetched_et": now_et.strftime("%Y-%m-%dT%H:%M"), "players": players}, f, indent=1)
    print(f"Sleeper: downloaded today's copy ({len(players)} injured offensive players).")
    return players


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def norm(name):
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.'`]", "", s)
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", s)
    s = re.sub(r"[^a-z ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def tidy(label):
    if label.lower().startswith("not injury related"):
        rest = label.split("-", 1)[1].strip() if "-" in label else "Non-injury"
        return rest[:1].upper() + rest[1:]
    return label


def practice_window(games, week):
    """Team -> the ET dates its practice reports cover this week."""
    windows = {}
    for g in games:
        if g["season"] != str(SEASON) or g["week"] != str(week):
            continue
        gd = date.fromisoformat(g["gameday"])
        # Thursday games: Mon/Tue/Wed reports. Everyone else: the 3 days ending 2 days before kickoff.
        days = [gd - timedelta(d) for d in ((3, 2, 1) if g["weekday"] == "Thursday" else (4, 3, 2))]
        for team in (g["home_team"], g["away_team"]):
            windows[team] = set(days)
    return windows


def final_report_day(games, week):
    """Team -> ET date of its final injury report (the one that sets game statuses)."""
    finals = {}
    for g in games:
        if g["season"] != str(SEASON) or g["week"] != str(week):
            continue
        gd = date.fromisoformat(g["gameday"])
        day = gd - timedelta(1 if g["weekday"] == "Thursday" else 2)
        for team in (g["home_team"], g["away_team"]):
            finals[team] = day
    return finals


def main():
    data = {k: fetch_csv(u) for k, u in URLS.items()}
    timelines = load_json(TIMELINES_PATH, {"injuries": [], "fallback": {}})
    today_et = datetime.now(ET).date()

    # 1. Consensus top 200 (offense only), keyed by name + position
    ranks = [r for r in data["ecr"] if r["fp_page"] == RANK_PAGE]
    ranks.sort(key=lambda r: float(r["ecr"]))
    ros_rank = {(norm(r["player"]), r["pos"]): i for i, r in enumerate(ranks, start=1) if r["pos"] in OFFENSE}
    doc = fetch_doc_top200()
    if len(doc) >= 100:
        top, rank_source = doc, "Doc's Top 200"
    else:
        top, rank_source = {k: v for k, v in ros_rank.items() if v <= TOP_N}, "FantasyPros rest-of-season"
    if len(top) < 100:
        sys.exit(f"Rankings look wrong ({len(top)} offensive players). Not overwriting.")
    dyn = [r for r in data["ecr"] if r["fp_page"] == DYNASTY_PAGE]
    dyn.sort(key=lambda r: float(r["ecr"]))
    dyn_rank = {(norm(r["player"]), r["pos"]): i for i, r in enumerate(dyn, start=1) if r["pos"] in OFFENSE}

    def reserve_relevant(key):
        return key in top or ros_rank.get(key, 9999) <= RESERVE_TOP_N or dyn_rank.get(key, 9999) <= RESERVE_TOP_N

    # 2. Latest week of official injury reports
    inj = data["injuries"]
    last_injury = {}
    for r in sorted(inj, key=lambda r: int(r["week"])):
        label = r["report_primary_injury"] or r["practice_primary_injury"]
        if label:
            last_injury[r["gsis_id"]] = label
    week = max(int(r["week"]) for r in inj)

    # 2a. Practice log: save today's practice status on the team's practice days
    log = load_json(LOG_PATH, {})
    if log.get("season") != SEASON or log.get("week") != week:
        log = {"season": SEASON, "week": week, "players": {}}
    windows = practice_window(data["games"], week)
    for r in inj:
        if int(r["week"]) != week or r["position"] not in OFFENSE:
            continue
        if (norm(r["full_name"]), r["position"]) not in top:
            continue
        code = PRACTICE_CODES.get(r["practice_status"])
        if code and today_et in windows.get(r["team"], ()):
            log["players"].setdefault(r["gsis_id"], {})[today_et.isoformat()] = code

    # A team's final report is out once any of its players has a game status,
    # or once its final-report day has passed.
    finals = final_report_day(data["games"], week)
    final_out = {r["team"] for r in inj if int(r["week"]) == week and r["report_status"]}
    final_out |= {t for t, d in finals.items() if today_et > d}

    rows = {}
    for r in inj:
        if int(r["week"]) != week or r["position"] not in OFFENSE:
            continue
        key = (norm(r["full_name"]), r["position"])
        if key not in top:
            continue
        status = r["report_status"]
        label = r["report_primary_injury"] or r["practice_primary_injury"] or ""
        if status not in REPORT_STATUSES:
            if label.lower().startswith("not injury related"):
                continue          # veteran rest days and personal days without a designation
            status = "Cleared" if r["team"] in final_out else "Practicing"
        rows[r["gsis_id"]] = {
            "id": r["gsis_id"], "name": r["full_name"], "pos": r["position"], "team": r["team"],
            "status": status, "injury": r["report_primary_injury"] or r["practice_primary_injury"] or "Undisclosed",
            "practice": r["practice_status"], "rank": top[key],
            "status_day": finals[r["team"]].strftime("%a") if r["team"] in finals else "",
        }

    # 3. Injured reserve and PUP from weekly rosters (these players drop off the weekly report)
    ros = data["rosters"]
    roster_week = max(int(r["week"]) for r in ros)
    ir_since = {}
    for r in sorted(ros, key=lambda r: int(r["week"])):
        if r["status"] == "RES" and r["status_description_abbr"] in RESERVE_CODES:
            ir_since.setdefault(r["gsis_id"], int(r["week"]))
        elif r["status"] == "ACT":
            ir_since.pop(r["gsis_id"], None)
    for r in ros:
        if int(r["week"]) != roster_week or r["gsis_id"] not in ir_since or r["position"] not in OFFENSE:
            continue
        if r["status"] != "RES" or r["status_description_abbr"] not in RESERVE_CODES:
            continue
        key = (norm(r["full_name"]), r["position"])
        if not reserve_relevant(key):
            continue
        rows[r["gsis_id"]] = {
            "id": r["gsis_id"], "name": r["full_name"], "pos": r["position"], "team": r["team"],
            "status": RESERVE_CODES[r["status_description_abbr"]],
            "injury": last_injury.get(r["gsis_id"], "Undisclosed"),
            "practice": "", "rank": top.get(key), "dyn_rank": dyn_rank.get(key),
            "ir_week": ir_since[r["gsis_id"]],
        }

    # 3a. Injuries announced after a team's game (Sleeper), before the next official report or IR move.
    #     Official data always wins: only players the official data doesn't list, and only for teams
    #     that have already played this week (their next report isn't out yet).
    team_gameday = {}
    for g in data["games"]:
        if g["season"] == str(SEASON) and g["week"] == str(week):
            for t in (g["home_team"], g["away_team"]):
                team_gameday[t] = date.fromisoformat(g["gameday"])
    listed_names = {(norm(x["name"]), x["pos"]) for x in rows.values()}
    news_added = 0
    for sp in sleeper_injuries():
        status = SLEEPER_STATUS.get(sp["status"])
        gid, team, pos, name = sp["id"], sp["team"], sp["pos"], sp["name"]
        key = (norm(name), pos)
        if gid in rows or key in listed_names:
            continue
        if not (team in team_gameday and today_et > team_gameday[team]):
            continue
        reserve = status in ("IR", "PUP")
        if not (reserve_relevant(key) if reserve else key in top):
            continue
        part = sp["part"]
        row = {
            "id": gid, "name": name, "pos": pos, "team": team,
            "injury": part[:1].upper() + part[1:] if part else "Undisclosed",
            "practice": "", "rank": top.get(key), "dyn_rank": dyn_rank.get(key), "news": True,
        }
        if reserve:
            row.update(status=status, ir_week=week + 1)
        else:
            # Sleeper's Out/Questionable here usually means he left the game, not a ruling for next week.
            # Show it as hurt with next week's status to come, until the official report says more.
            row.update(status="Practicing", status_day="",
                       back=f"Hurt Week {week}. Week {week + 1} status TBD",
                       cond="Serious" if status in ("Out", "Doubtful") else "Fair")
        rows[gid or f"sleeper-{sp['sleeper_id']}"] = row
        news_added += 1
    print(f"Sleeper: added {news_added} post-game injuries not yet on official reports.")

    # 4. Latest depth chart snapshot -> next healthy player at the same spot
    dc = data["depth"]
    latest = max(r["dt"] for r in dc)
    chart = [r for r in dc if r["dt"] == latest and r["pos_abb"] in OFFENSE]
    unavailable = {gid for gid, x in rows.items() if x["status"] in ("IR", "PUP", "Out")}

    def backup_for(p):
        if p["status"] in ("IR", "PUP", "Cleared"):
            return ""
        mine = [c for c in chart if c["gsis_id"] == p["id"]]
        same_team = [c for c in chart if c["team"] == p["team"] and c["pos_abb"] == p["pos"]]
        if mine:
            slot, rank = mine[0]["pos_slot"], int(mine[0]["pos_rank"])
            pool = [c for c in same_team if c["pos_slot"] == slot] or same_team
            pool = [c for c in pool if int(c["pos_rank"]) > rank]
        else:
            pool = same_team
        pool.sort(key=lambda c: int(c["pos_rank"]))
        for c in pool:
            if c["gsis_id"] != p["id"] and c["gsis_id"] not in unavailable:
                return c["player_name"]
        return ""

    # 5. Return window from league rules, not guesses
    def back_text(p):
        if p.get("back"):
            return p["back"]
        if p["status"] in ("IR", "PUP"):
            return f"Eligible Week {p['ir_week'] + 4}"
        if p["status"] == "Cleared":
            return "Expected to play"
        if p["status"] == "Practicing":
            return f"Status due {p['status_day']}" if p.get("status_day") else "Status due Friday"
        wk = p.get("wk", week)
        if p["status"] == "Out":
            return f"Out Week {wk}"
        if p["status"] == "Doubtful":
            return f"Unlikely Week {wk}"
        return "Game-time call"

    # 6. Typical games missed + re-injury risk from the lookup table
    def timeline_for(p):
        label = p["injury"].lower()
        entry = next((e for e in timelines["injuries"] if any(m in label for m in e["match"])),
                     timelines.get("fallback", {}))
        lo = entry.get("min", 0)
        hi = entry.get("qb_max", entry.get("max", 2)) if p["pos"] == "QB" else entry.get("max", 2)
        if p["status"] in ("IR", "PUP"):
            typical = f"{p['status']}: 4+ games"
        elif p["status"] == "Cleared":
            typical = "\u2014"
        else:
            if p["status"] in ("Out", "Doubtful"):
                lo, hi = max(lo, 1), max(hi, 1)
            typical = f"{lo} game" if lo == hi == 1 else (f"{lo} games" if lo == hi else f"{lo}\u2013{hi} games")
        return {
            "typical": typical,
            "reinjury": entry.get("reinjury", "Unknown"),
            "note": entry.get("note", ""),
            "evidence": entry.get("evidence", "estimate"),
            "source": entry.get("source", ""),
            "source_url": entry.get("url", ""),
        }

    # 7. Condition, hospital-chart style, from game status + latest practice
    def condition_for(p):
        if p.get("cond"):
            return p["cond"]
        code = PRACTICE_CODES.get(p["practice"], "")
        if p["status"] in ("IR", "PUP", "Out"):
            return "Critical"
        if p["status"] == "Doubtful" or code == "DNP":
            return "Serious"
        if code == "FP" or p["status"] == "Cleared":
            return "Good"
        return "Fair"

    def trail_for(p):
        days = log["players"].get(p["id"], {})
        return [{"d": date.fromisoformat(d).strftime("%a"), "s": s} for d, s in sorted(days.items())][-3:]

    out = []
    for p in rows.values():
        p["injury"] = tidy(p["injury"])
        item = {
            "name": p["name"], "pos": p["pos"], "team": p["team"], "status": p["status"],
            "condition": condition_for(p),
            "injury": p["injury"], "practice": p["practice"],
            "practice_code": PRACTICE_CODES.get(p["practice"], ""),
            "trail": trail_for(p),
            "back": back_text(p), "pickup": backup_for(p), "rank": p["rank"],
            "dyn_rank": p.get("dyn_rank"),
        }
        if p.get("news"):
            item["news"] = True   # reported by the team/media; not on an official NFL report yet
        item.update(timeline_for(p))
        out.append(item)
    out.sort(key=lambda x: (SEVERITY[x["status"]], x["rank"] or 1000 + (x["dyn_rank"] or 999)))

    with open(LOG_PATH, "w") as f:
        json.dump(log, f, indent=1)

    payload = {
        "season": SEASON, "week": week, "rank_source": rank_source,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": "Official NFL injury reports, rosters, depth charts and schedule via nflverse; post-game injury news via Sleeper; FantasyPros consensus rankings via DynastyProcess; return timelines from data/return-timelines.json",
        "players": out,
    }
    # Rewrite every run so "Updated" on the page shows the latest check
       
    with open(OUT_PATH, "w") as f:
        json.dump(payload, f, indent=1)
    print(f"Week {week}: {len(out)} players written to {OUT_PATH}")


if __name__ == "__main__":
    main()
