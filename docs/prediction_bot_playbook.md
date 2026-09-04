# Prediction Bot Playbook — EPL / UCL

This is the process a Claude session follows when a scheduled Routine
fires it to evaluate a full slate of Premier League or Champions League
games and log picks to the War Room dashboard. It runs unattended, so
follow it exactly — there's no one to ask mid-run.

Two Routines run this:
- **Tuesday 8pm ET** → evaluates **Wednesday's** Champions League slate.
- **Friday 7pm ET** → evaluates the upcoming **weekend's** Premier
  League slate (Saturday through Monday — a Friday-night PL game has
  already kicked off by 7pm ET, so it's out of scope for this run).

Live app: `https://war-room-dashboard-fus7.onrender.com` (all API calls
below are against this host, not localhost). If any call in this doc
returns 404 on routes like `/api/standings`, the code this playbook
depends on hasn't been deployed yet — stop and say so rather than
guessing at a different API shape.

## 1. Work out the slate

- **UCL run:** target date = tomorrow (the Wednesday this run is prepping
  for), in `YYYYMMDD`.
- **PL run:** target dates = the upcoming Saturday, Sunday, and Monday,
  in `YYYYMMDD`.

For each target date:
```
GET /api/games?league=UCL&date=YYYYMMDD   (or league=EPL)
```
Each game in the response has `id`, `home`, `away`, `home_id`, `away_id`,
`matchup`, `kickoff`, `status`. Skip anything whose `status` shows it's
already finished or in progress — this run is about upcoming games only.

## 2. Gather everything on each remaining game

For every game still to be played, pull all of this before forming an
opinion — don't skip straight to the odds:

1. **The line itself:**
   `GET /api/odds?league=<L>&event_id=<id>`
   → `home_spread`/`home_spread_odds`/`away_spread_odds`,
   `total`/`over_odds`/`under_odds`,
   `home_moneyline`/`away_moneyline`/`draw_moneyline`.
   A missing/404 result means no line is posted yet for that game — skip
   markets you don't have a number for; don't invent one.

2. **The full research bundle:**
   `GET /api/match_intel?league=<L>&event_id=<id>`
   → for both teams: current standing (rank/points/record), last-5 form,
   home/away split (W-D-L and goals), full squad with any ESPN injury
   flag, historical managers, ESPN's own news, and Google-News local/beat
   coverage — plus head-to-head history and a match-day weather forecast
   (temperature/rain chance/wind) at the top level. `weather` and `h2h`
   can legitimately be `null` (forecast window, or no meeting history) —
   that's not an error, just less signal for that game.

3. **The official league site — required, not optional:**
   - **EPL games:** check **premierleague.com** directly — the fixture's
     match preview, each club's official news/team-news page, and press
     conference summaries for the matchweek. This is often where a
     manager confirms a starting-XI doubt or an injury/suspension before
     it shows up anywhere else. Use WebFetch/WebSearch for this; ESPN's
     feed alone is not a substitute.
   - **UCL games:** same idea via **uefa.com**'s Champions League site
     (official matchday previews, squad news).
   - For anything still unclear (a late fitness test, a rumored
     rotation), a general web search for `<club> press conference
     <date>` or `<club> team news <opponent>` is fair game too.

Weigh recency: a knock reported yesterday matters more than a stat from
August. If sources conflict on a fitness call, say so in the pick's notes
rather than picking one silently.

## 3. Evaluate every market on every remaining game

For each game, form an independent view on each market that has a
posted line:

- **Match result** (`match_result`): home / draw / away.
- **Total goals** (`total`): over / under, at the posted number.
- **Spread / handicap** (`spread`): the posted home-team line, either
  side.

For each side of each market, assign a **confidence** — your own
calibrated estimate (0-100) that this specific side, at this specific
line, is correct, grounded in what you gathered in step 2 (form, table
position, home/away trend, head-to-head, squad news, injuries, weather,
manager, motivation/context). Not a vibe — if asked "why 63 and not 58,"
you should have an answer.

## 4. Decide what to recommend

**Only a side with confidence ≥ 60% is eligible to become a pick.**
Anything under that line gets left alone — do not round up, do not force
a pick into a thin slate just to fill a category. A report with two
picks (or zero) is a correct outcome if that's all the slate supports.

Sort every eligible (≥60%) side across the whole slate by confidence,
then assign to the five categories using the same guidance the rest of
this app already uses for CFB/NFL — same rules, same stake conventions:

| Category | Guidance |
|---|---|
| **Totals Radar** (`totals_radar`) | Up to 3 per report, `total` market picks only, selective. |
| **Thor Hammer Smash** (`thor_hammer`) | $500 / 5 units, extremely rare — reserve for a genuine standout (typically 80%+ confidence with strong corroborating evidence), not one per slate by default. Zero is a normal outcome most weeks. |
| **Best Bet / Value Play** (`best_bet`) | $50–150, the core "real edge, good number" play(s) — your highest-conviction match_result or spread picks land here. |
| **Sexy Moneyline** (`sexy_moneyline`) | $25 normally, $50 max — a live underdog or draw pick at a big price, not a favorite. |
| **Good-If-It-Goes Parlay** (`parlay`) | $10 max, 3+ legs, one per report — combine legs you'd each independently back, not filler. |

Don't duplicate the same game+market+side across two categories. It's
fine for a category to end up empty.

## 5. Submit the report and picks

Compute `week_number` as the league's actual **matchweek number** — the
number a fan would recognize ("Matchweek 3"), not an arbitrary counter.
`GET /api/standings?league=<L>` already gives you this for free: take
the most common `played` value across the table entries and add 1 (a
team that's played 2 games is walking into matchweek 3). Use the most
common value rather than any single team's, since a postponed match can
leave one or two teams a game behind the rest of the table.

Leave `week_label` **empty** unless you have something genuinely
descriptive to add beyond the number — the dashboard already renders
"Week 3 — Premier League" on its own from `week_number` + `league`
wherever a view can show more than one league at once, and a set
`week_label` overrides that generated text instead of adding to it.

```
POST /api/reports
Content-Type: application/json
{
  "source": "Claude",
  "league": "UCL",            // or "EPL"
  "report_date": "YYYY-MM-DD", // today, ET
  "week_number": <matchweek>,
  "week_label": ""             // leave blank - see above
}
→ {"id": <report_id>}
```

Then, once per recommended pick:

```
POST /api/reports/<report_id>/picks
Content-Type: application/json
{
  "category": "best_bet",              // one of the five slugs above
  "matchup": "Tottenham Hotspur @ Manchester City",
  "selection": "Manchester City to Win",
  "odds": -135,                        // the American price you pulled in step 2
  "stake": 100,
  "confidence": 68,                    // your 0-100 estimate from step 3
  "notes": "Man City unbeaten in 9 at home; Spurs missing both starting CBs per official injury news.",
  "bet_type": "match_result",          // "match_result" | "total" | "spread"
  "bet_side": "home",                  // match_result: home/draw/away; total: over/under; spread: home/away
  "bet_line": null,                    // the posted number for total/spread; null for match_result
  "espn_event_id": "740613",
  "home_team": "Manchester City",
  "away_team": "Tottenham Hotspur"
}
→ {"id": <pick_id>}
```

Use the exact `bet_type`/`bet_side`/`bet_line`/`espn_event_id` from the
line you pulled — this is what lets the hourly auto-grade job settle the
pick automatically once the match finishes. A 400 response means a field
is wrong (bad category, missing required field) — fix and retry that one
pick; don't abandon the rest of the slate over one bad request.

If you catch a mistake in the report's own metadata after submitting
(wrong `week_number`, a typo in the notes), fix it in place rather than
deleting and resubmitting the whole report:
`PATCH /api/reports/<report_id>` with a JSON body of just the field(s)
to change (`week_number`, `week_label`, `blind_spot_notes`,
`lsu_review_notes`). It leaves the report's picks untouched. A wrong
`source`, `league`, or `report_date` isn't editable this way — that
needs a delete + resubmit instead.

## 6. Done

Nothing else to do — grading happens on its own via the hourly
`auto_grade_all` GitHub Action. If the slate had nothing clearing 60%
anywhere, it's still worth posting the report (zero picks) so the run is
visible in the Reports list, with a one-line note in `blind_spot_notes`
saying why (e.g. "Lines mostly chalk this week, nothing cleared 60%").
