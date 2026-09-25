"""
Fantasy Football Doctor - injury report builder.

Runs on GitHub Actions on a schedule. Pulls:
  * Official NFL injury reports (game status + practice participation) via nflverse
  * Weekly roster status (for injured reserve) via nflverse
  * Depth charts and the season schedule via nflverse
  * FantasyPros expert consensus rankings via DynastyProcess
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
        if p["status"] in ("IR", "PUP"):
            return f"Eligible Week {p['ir_week'] + 4}"
        if p["status"] == "Cleared":
            return "Expected to play"
        if p["status"] == "Practicing":
            return f"Status due {p['status_day']}" if p.get("status_day") else "Status due Friday"
        if p["status"] == "Out":
            return f"Out Week {week}"
        if p["status"] == "Doubtful":
            return f"Unlikely Week {week}"
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
        item.update(timeline_for(p))
        out.append(item)
    out.sort(key=lambda x: (SEVERITY[x["status"]], x["rank"] or 1000 + (x["dyn_rank"] or 999)))

    with open(LOG_PATH, "w") as f:
        json.dump(log, f, indent=1)

    payload = {
        "season": SEASON, "week": week, "rank_source": rank_source,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": "Official NFL injury reports, rosters, depth charts and schedule via nflverse; FantasyPros consensus rankings via DynastyProcess; return timelines from data/return-timelines.json",
        "players": out,
    }
    # Only rewrite the file when the player list itself changed
    old = load_json(OUT_PATH, {})
    if old.get("players") == out and old.get("week") == week and old.get("rank_source") == rank_source:
        print("No change in player list.")
        return
    with open(OUT_PATH, "w") as f:
        json.dump(payload, f, indent=1)
    print(f"Week {week}: {len(out)} players written to {OUT_PATH}")


if __name__ == "__main__":
    main()
