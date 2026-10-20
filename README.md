# FFD Injury Report data

This repo builds `data/injury-report.json`, which the Injury Report page on fantasyfootballdoctor.com reads. A GitHub Action runs `build.py` every 3 hours, plus extra runs Friday evening.

## Files

| File | What it is | Edit by hand? |
|---|---|---|
| `data/injury-report.json` | The report the page reads | No |
| `data/practice-log.json` | Each player's Wed/Thu/Fri practice status this week | No |
| `data/sleeper-injuries.json` | Once-a-day copy of Sleeper's post-game injury news | No |
| `data/checks.json` | What the quality checks caught on the latest run | No, read it |
| `data/return-timelines.json` | Typical games missed, re-injury risk and the note for each injury type | Yes |
| `data/overrides.json` | Corrections for when the official label is misleading | Yes |

## Quality checks

Every run, the script checks each player before writing him to the page and lists what it caught in `data/checks.json`:

- **non_injury / non_injury_news**: rest days, personal days and coach's decisions. Official designations stay on the page labeled "Not injury," with no recovery note, re-injury risk or pickup unless he's Out or Doubtful. Post-game Sleeper reports of a coach's decision are left off.
- **self_pickup / pickup_unavailable**: a player suggested as his own replacement, or a pickup who is also out. The pickup is removed.
- **ir_longer_than_typical**: a player on IR whose injury usually costs fewer than four games. The note says the stint is longer than the routine version.
- **unmatched_label**: the team named an injury that `return-timelines.json` doesn't cover. Add it there.
- **trail_mismatch**: the latest practice status doesn't match the end of the practice trail. Flag only; it should stay at zero.
- **override / override_expired**: a hand correction was applied, or one has passed its `until` date.

## Overrides

When the official label is wrong or too vague (Jalen Hurts is listed as "Calf" for a blood clot), add an entry to `data/overrides.json`:

```json
{
 "name": "Jalen Hurts",
 "team": "PHI",
 "until": "2026-12-31",
 "why": "Official label says calf; it's a blood clot",
 "injury": "Calf (blood clot)",
 "note": "Plain-English explanation and timeline."
}
```

Any of `injury`, `note`, `typical`, `reinjury`, `evidence`, `source`, `source_url` and `pickup` can be replaced. Add `"hold": true` to hide the note and pickup and show only the official status and practice trail. After the `until` date, the entry is ignored and shows up in `checks.json` as expired.

## Freshness fields

Each player in the report carries:

- `source_date`: the practice report or news day the row is built from
- `changed`: when his status, injury or practice status last changed
- `game_played`: true once his team has played this week, so the designation is history rather than a lineup call

The top level adds `week_complete`, `next_week` and `recent_changes`, a rolling 7-day list of status changes for a "What changed" section.

## Practice trail timing

nflverse posts each practice report overnight. The script logs what it reads under the team's most recent practice day *before* today, so Wednesday's report lands on Wednesday and the Friday final report isn't dropped.
