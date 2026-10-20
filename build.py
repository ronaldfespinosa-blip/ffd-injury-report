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
OVERRIDES_PATH = "data/overrides.json"   # hand-edited corrections, e.g. a diagnosis the label gets wrong
CHECKS_PATH = "data/checks.json"         # what the quality checks caught on the latest run
CHANGE_FIELDS = ("status", "injury", "practice_code")   # a change in any of these is a real update
CHANGES_KEEP_DAYS = 7
CHANGES_KEEP_MAX = 80
SEED_PATH = "data/changes-seed.json"     # one-time backfill of the change list; ages out after 7 days
ET = ZoneInfo("America/New_York")
# Absences that aren't injuries: no recovery note, no re-injury risk, never "Serious" from a rest day
NON_INJURY_RE = re.compile(
    r"coach'?e?s?'?\s*decision|team decision|not injury|non-injury|\bresting\b|\brest\b|personal|suspen")


def non_injury(label):
    return bool(NON_INJURY_RE.search((label or "").lower().replace("’", "'")))
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


def _as_et(s):
    """Parse a stored timestamp; older ones were written without an offset and are ET."""
    try:
        t = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=ET)


def track_changes(prev, out, season, week, rank_source, now, seed=()):
    """Stamp each player in `out` with when he last changed ("changed") and return the rolling
    7-day change list, newest first. Each entry: at, name, team, pos, rank, kind
    (added | status | injury | off), from, to, injury, news, week."""
    stamp = now.isoformat(timespec="minutes")
    prev_by = {(x["name"], x["team"]): x for x in prev.get("players", [])}
    # Only compare like with like: a rankings fallback or a new season would look like mass changes
    comparable = prev.get("rank_source") == rank_source and prev.get("season") == season
    rollover = prev.get("week") != week
    cutoff = now - timedelta(CHANGES_KEEP_DAYS)
    seen, changes = set(), []
    for c in list(prev.get("recent_changes", [])) + list(seed):
        at = _as_et(c.get("at"))
        sig = (c.get("at"), c.get("name"), c.get("kind"))
        if c.get("kind") and at and at >= cutoff and sig not in seen:   # no kind = an older format
            seen.add(sig)
            changes.append(c)

    def add(kind, item, old=None, **extra):
        # A player who drops off and comes back with the same status (a rankings wobble, say)
        # never really changed: cancel the earlier entry instead of logging a second one.
        undo = {"added": ("off", "from"), "off": ("added", "to")}.get(kind)
        if undo:
            for c in changes:
                if (c["name"], c["team"], c["kind"]) == (item["name"], item["team"], undo[0]) \
                        and c.get(undo[1]) == item.get("status"):
                    changes.remove(c)
                    return
        changes.append({
            "at": stamp, "name": item["name"], "team": item["team"], "pos": item.get("pos"),
            "rank": item.get("rank"), "kind": kind,
            "from": item.get("status") if kind == "off" else (old.get("status") if old else None),
            "to": None if kind == "off" else item.get("status"),
            "injury": item.get("injury"), "news": bool(item.get("news")), "week": week, **extra})

    for item in out:
        old = prev_by.get((item["name"], item["team"]))
        last = old.get("changed") if old else None
        if item.get("news") and old and old.get("news") and old.get("source_date"):
            # Sleeper is re-downloaded daily; the report date is the day we first saw it
            item["source_date"] = old["source_date"]
            item["source_label"] = f"Reported by {date.fromisoformat(old['source_date']):%A}"
        if not comparable:
            item["changed"] = last
        elif old is None:
            m = re.match(r"Eligible Week (\d+)", item.get("back") or "")
            if item["status"] in ("IR", "PUP") and m and int(m.group(1)) - 4 < week:
                # Long-term IR player who just moved back into the rankings window: not news
                item["changed"] = None
                continue
            item["changed"] = stamp
            add("added", item)
        elif rollover:
            # A new week resets every status to Practicing. Only moves on or off IR/PUP are news then.
            if (old.get("status") in ("IR", "PUP")) != (item["status"] in ("IR", "PUP")):
                item["changed"] = stamp
                add("status", item, old)
            else:
                item["changed"] = last
        elif all(old.get(k) == item.get(k) for k in CHANGE_FIELDS):
            item["changed"] = last
        else:
            item["changed"] = stamp
            if old.get("status") != item["status"]:
                add("status", item, old)
            elif old.get("injury") != item["injury"]:
                add("injury", item, old, was=old.get("injury"))
            # a practice-only change updates "changed" but stays off the list (it happens daily)
    if comparable and not rollover:
        current = {(x["name"], x["team"]) for x in out}
        for key, old in prev_by.items():
            if key not in current:
                add("off", old)
    changes.sort(key=lambda c: (-_as_et(c["at"]).timestamp(), c.get("rank") or 999))
    # A post-game report's date is the day it first appeared in the list, if that's earlier
    first_seen = {}
    for c in changes:
        if c["kind"] == "added" and c.get("news"):
            k, d = (c["name"], c["team"]), _as_et(c["at"]).date().isoformat()
            first_seen[k] = min(first_seen.get(k, d), d)
    for item in out:
        d = first_seen.get((item["name"], item["team"]))
        if item.get("news") and d and (not item.get("source_date") or d < item["source_date"]):
            item["source_date"] = d
            item["source_label"] = f"Reported by {date.fromisoformat(d):%A}"
    return changes[:CHANGES_KEEP_MAX]


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

    # 2a. Practice log. nflverse posts each practice report overnight, so what we read today is the
    #     report from the team's most recent practice day BEFORE today. (Stamping it with today's date
    #     shifted every trail a day late and dropped the final Friday report.) Only that latest
    #     practice day is ever written, so earlier days in the trail are never overwritten.
    log = load_json(LOG_PATH, {})
    if log.get("season") != SEASON or log.get("week") != week:
        log = {"season": SEASON, "week": week, "players": {}}
    windows = practice_window(data["games"], week)
    report_day = {}
    for team, days in windows.items():
        past = [d for d in days if d < today_et]
        if past:
            report_day[team] = max(past)
    for r in inj:
        if int(r["week"]) != week or r["position"] not in OFFENSE:
            continue
        if (norm(r["full_name"]), r["position"]) not in top:
            continue
        code = PRACTICE_CODES.get(r["practice_status"])
        day = report_day.get(r["team"])
        if code and day:
            log["players"].setdefault(r["gsis_id"], {})[day.isoformat()] = code

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
            "source_date": report_day[r["team"]].isoformat() if r["team"] in report_day else None,
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
    checks = []   # filled in by the quality checks below; saved to data/checks.json
    sleeper_players = sleeper_injuries()
    sleeper_day = (load_json(SLEEPER_CACHE, {}).get("fetched_et") or "")[:10] or None
    for sp in sleeper_players:
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
        if non_injury(part):
            # A benching or rest day after a game isn't an injury report (this put Nicholas
            # Singleton's coach's decision on the page with himself as his own pickup).
            checks.append({"name": name, "team": team, "check": "non_injury_news",
                           "detail": part, "action": "left off the report"})
            continue
        row = {
            "id": gid, "name": name, "pos": pos, "team": team,
            "injury": part[:1].upper() + part[1:] if part else "Undisclosed",
            "practice": "", "rank": top.get(key), "dyn_rank": dyn_rank.get(key), "news": True,
            "source_date": sleeper_day,
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
    # By name too: post-game (Sleeper) rows can arrive without an NFL id
    unavailable_names = {(norm(x["name"]), x["team"]) for x in rows.values() if x["status"] in ("IR", "PUP", "Out")}

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
            if c["gsis_id"] == p["id"] or norm(c["player_name"]) == norm(p["name"]):
                continue   # never suggest the injured player as his own replacement
            if c["gsis_id"] in unavailable or (norm(c["player_name"]), c["team"]) in unavailable_names:
                continue
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
        if non_injury(label):
            label = "not injury"   # route every non-injury reason to the non-injury entry
        matched = next((e for e in timelines["injuries"] if any(m in label for m in e["match"])), None)
        entry = matched or timelines.get("fallback", {})
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
        result = {
            "typical": typical,
            "reinjury": entry.get("reinjury", "Unknown"),
            "note": entry.get("note", ""),
            "evidence": entry.get("evidence", "estimate"),
            "source": entry.get("source", ""),
            "source_url": entry.get("url", ""),
        }
        shown = p["injury"].strip()
        if matched is None and shown.lower() not in ("", "undisclosed"):
            # The team DID name it; the old fallback said it hadn't
            result["note"] = (f"The team listed {shown.lower()} without more detail. "
                              "We don't have a typical timeline for that one yet.")
            checks.append({"name": p["name"], "team": p["team"], "check": "unmatched_label",
                           "detail": shown, "action": "generic note; add it to return-timelines.json"})
        elif matched is not None and p["status"] in ("IR", "PUP") and not non_injury(label) \
                and entry.get("max", 2) < 4 and "IR" not in entry.get("note", ""):
            # IR means 4+ games. A note about a routine version of this injury contradicts that
            # (the Hurts entry explained a 2.5-week calf strain for a player on IR).
            result["note"] = (f"He's on IR, so he's out at least four games. That's longer than a "
                              f"typical {shown.lower()} injury, so expect more than the routine "
                              f"version. Routine case: " + entry.get("note", ""))
            checks.append({"name": p["name"], "team": p["team"], "check": "ir_longer_than_typical",
                           "detail": f"{shown} usually 0\u2013{entry.get('max', 2)} games",
                           "action": "note reworded to say the IR stint is longer than typical"})
        return result

    # 7. Condition, hospital-chart style, from game status + latest practice
    def condition_for(p):
        if p.get("cond"):
            return p["cond"]
        code = PRACTICE_CODES.get(p["practice"], "")
        if p["status"] in ("IR", "PUP", "Out"):
            return "Critical"
        if non_injury(p["injury"]):
            # A rest-day DNP isn't a health signal: Doubtful still reads Serious, otherwise Fair
            return "Serious" if p["status"] == "Doubtful" else "Fair"
        if p["status"] == "Doubtful" or code == "DNP":
            return "Serious"
        if code == "FP" or p["status"] == "Cleared":
            return "Good"
        return "Fair"

    def trail_for(p):
        days = log["players"].get(p["id"], {})
        return [{"d": date.fromisoformat(d).strftime("%a"), "s": s} for d, s in sorted(days.items())][-3:]

    # 8. Hand corrections (data/overrides.json) for cases the official label gets wrong
    overrides = {}
    for o in load_json(OVERRIDES_PATH, {"players": []}).get("players", []):
        okey = (norm(o.get("name", "")), o.get("team", ""))
        if o.get("until") and date.fromisoformat(o["until"]) < today_et:
            checks.append({"name": o.get("name"), "team": o.get("team"), "check": "override_expired",
                           "detail": f"expired {o['until']}", "action": "ignored; delete or extend it"})
            continue
        overrides[okey] = o
    OVERRIDE_FIELDS = ("injury", "note", "typical", "reinjury", "evidence", "source", "source_url", "pickup")

    week_days = [d for d in team_gameday.values()]
    week_complete = bool(week_days) and today_et > max(week_days)

    def source_label(p):
        """Where a row's information comes from, in words: "Friday's final Week 6 report"."""
        sd = p.get("source_date")
        if p.get("news"):
            return f"Reported by {date.fromisoformat(sd):%A}" if sd else "Reported, not official yet"
        if p["status"] in ("IR", "PUP") or not sd:
            return ""
        d = date.fromisoformat(sd)
        if finals.get(p["team"]) == d:
            return f"{d:%A}’s final Week {week} report"
        return f"{d:%A}’s practice report"

    out = []
    for p in rows.values():
        p["injury"] = tidy(p["injury"])
        if non_injury(p["injury"]) and not p["injury"].lower().startswith("not injury"):
            p["injury"] = f"Not injury: {p['injury']}"
        item = {
            "name": p["name"], "pos": p["pos"], "team": p["team"], "status": p["status"],
            "condition": condition_for(p),
            "injury": p["injury"], "practice": p["practice"],
            "practice_code": PRACTICE_CODES.get(p["practice"], ""),
            "trail": trail_for(p),
            "back": back_text(p), "pickup": backup_for(p), "rank": p["rank"],
            "dyn_rank": p.get("dyn_rank"),
            "source_date": p.get("source_date"),   # date of the report or news this row is built from
            "source_label": source_label(p),       # the same, in words, for the page
        }
        if p.get("news"):
            item["news"] = True   # reported by the team/media; not on an official NFL report yet
        item.update(timeline_for(p))
        chk = lambda check, detail, action: checks.append(
            {"name": item["name"], "team": item["team"], "check": check, "detail": detail, "action": action})

        # Game already played: this week's designation is history, not a lineup decision
        played = (not item.get("news") and item["status"] in ("Out", "Doubtful", "Questionable", "Cleared")
                  and p["team"] in team_gameday and today_et > team_gameday[p["team"]])
        if played:
            item["game_played"] = True
            item["back"] = (f"Missed Week {week}. Week {week + 1} status TBD" if item["status"] == "Out"
                            else f"Week {week} game played. Week {week + 1} status TBD"
                            if item["status"] != "Cleared" else f"Cleared for Week {week}")

        if non_injury(item["injury"]):
            chk("non_injury", item["injury"], "no recovery note or re-injury risk")
            item["reinjury"] = "None"
            if item["status"] not in ("Out", "Doubtful"):
                item["pickup"] = ""

        o = overrides.get((norm(item["name"]), item["team"]))
        if o:
            for k in OVERRIDE_FIELDS:
                if k in o:
                    item[k] = o[k]
            if o.get("hold"):
                # Not confident in the details: show the official status and trail only
                item.update(note="", pickup="", evidence="estimate", source="", source_url="")
            # The hand correction replaces whatever the automatic note checks said about him
            checks[:] = [c for c in checks if not (c["name"] == item["name"] and c["team"] == item["team"]
                         and c["check"] in ("ir_longer_than_typical", "unmatched_label"))]
            chk("override", o.get("why", ""), "hand correction applied")

        # Last line of defense: these should never reach the page
        if item["pickup"] and norm(item["pickup"]) == norm(item["name"]):
            chk("self_pickup", item["pickup"], "pickup removed")
            item["pickup"] = ""
        if item["pickup"] and (norm(item["pickup"]), item["team"]) in unavailable_names:
            chk("pickup_unavailable", item["pickup"], "pickup removed (he's out too)")
            item["pickup"] = ""
        if item["trail"] and item["practice_code"] and item["trail"][-1]["s"] != item["practice_code"]:
            chk("trail_mismatch", f"latest {item['practice_code']}, trail ends {item['trail'][-1]['s']}",
                "flag only")
        out.append(item)
    out.sort(key=lambda x: (SEVERITY[x["status"]], x["rank"] or 1000 + (x["dyn_rank"] or 999)))

    # 9. What changed since the last run: each player's last-change time, plus the rolling
    #    7-day list behind the page's "What changed" box
    changes = track_changes(load_json(OUT_PATH, {}), out, SEASON, week, rank_source,
                            datetime.now(ET), seed=load_json(SEED_PATH, []))

    with open(LOG_PATH, "w") as f:
        json.dump(log, f, indent=1)

    payload = {
        "season": SEASON, "week": week, "rank_source": rank_source,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "week_complete": week_complete,               # every Week N game has been played
        "next_week": week + 1 if week_complete else week,
        "sources": "Official NFL injury reports, rosters, depth charts and schedule via nflverse; post-game injury news via Sleeper; FantasyPros consensus rankings via DynastyProcess; return timelines from data/return-timelines.json",
        "players": out,
        "recent_changes": changes,
    }
    # Rewrite every run so "Updated" on the page shows the latest check
    with open(OUT_PATH, "w") as f:
        json.dump(payload, f, indent=1)
    with open(CHECKS_PATH, "w") as f:
        json.dump({"week": week, "checks": checks}, f, indent=1)
    print(f"Week {week}: {len(out)} players written to {OUT_PATH}")
    print(f"Quality checks: {len(checks)} caught" + "".join(
        f"\n  - {c['name']} ({c['team']}): {c['check']}: {c['detail']} -> {c['action']}" for c in checks))


if __name__ == "__main__":
    main()
