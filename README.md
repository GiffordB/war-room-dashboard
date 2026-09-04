# Football Betting War Room Dashboard

Tracks picks from the "Football Betting War Room" report format — logged
weekly from three AI sources (Claude, Grok, and ChatGPT), across College
Football, the NFL, the Premier League, and the Champions League — and
compares how they're actually performing against each other, week over
week. Same five pick categories and same three-source comparison across
all four leagues; only the odds market and the prediction research
behind a pick differ by sport (American football vs. soccer).

For the two soccer leagues there's also a **Team Intel** page: free,
public prediction research (form, league position, home/away splits,
squad + injury flags, manager history, club news, local beat coverage,
and match-day weather) pulled live so there's something to base a pick
on beyond the odds themselves. See **Team Intel** under Features.

## Running it

```bash
# 1. Create an isolated Python environment for this project
python3 -m venv venv

# 2. Install dependencies (Flask, gunicorn, requests)
./venv/bin/pip install -r requirements.txt

# 3. Start the app
./venv/bin/python app.py
```

Then open **http://127.0.0.1:5060** in your browser. Locally, data is
just a JSON file at `data/war_room.json` — no setup needed. See
**Persistence** below for how production is different.

## Running it on the web

This repo includes a `render.yaml` at the top level, so you can deploy it
to [Render](https://render.com) with one click from any browser:
**https://render.com/deploy?repo=https://github.com/GiffordB/war-room-dashboard**

## Persistence — no database, no paid plan

Render's free web service has no persistent disk: anything written to
local disk is gone on the next restart or redeploy. Rather than pay for a
database, this app persists its data by **committing a JSON file straight
into this repo** via the GitHub Contents API — the same trick this
account's other free-tier dashboards use (see `BriggsTrading`'s
`real_holdings_store.py`). Every add/settle/delete becomes a git commit
to `data/war_room.json`, visible in this repo's history. $0/month either
way, but only one of the two setups actually survives a restart:

- **`GITHUB_PAT` not set** → falls back to a local file, wiped on every
  restart/redeploy. Fine for kicking the tires, not for real tracking.
- **`GITHUB_PAT` set** → every change is a durable git commit. This is
  the one you want for actual weekly use.

**To turn on durable storage, after the first deploy:**
1. On GitHub, go to **Settings → Developer settings → Personal access
   tokens → Fine-grained tokens → Generate new token**.
2. Give it access to only this repository (`war-room-dashboard`), with
   **Contents: Read and write** permission. (A classic PAT with the
   `repo` scope also works, if you'd rather use one you already have.)
3. On Render, open this service → **Environment** tab → add `GITHUB_PAT`
   with that token as the value → save (Render redeploys automatically).

`GITHUB_REPO` is already set to `GiffordB/war-room-dashboard` in
`render.yaml` — only `GITHUB_PAT` needs adding by hand, since a token
isn't something to put in a file that's itself committed to the repo.

## What's in here

- `app.py` — the Flask application: routes, American-odds payout math,
  and the Claude/Grok/ChatGPT comparison + weekly-trend stats.
- `store.py` — persistence: reads/writes the JSON data blob, either via
  the GitHub Contents API (production) or a local file (dev). See
  **Persistence** above.
- `odds.py` — pulls live lines (spread/total/moneyline, plus a draw
  market for soccer) and game schedules from ESPN's public scoreboard
  API — DraftKings for CFB/NFL, ESPN BET for EPL/UCL (ESPN's soccer feed
  doesn't carry DraftKings lines). Also backs the Team Intel page:
  standings, team schedules/form, rosters, and news, all from the same
  ESPN API, plus two more free/keyless sources — Open-Meteo for
  match-day weather and Google News for local/beat-reporter coverage.
  No API key needed anywhere in this file.
- `charts.py` — hand-rolled SVG line/bar charts, no JS library or CDN.
- `templates/` — the HTML pages.
- `static/style.css` — the LSU purple/gold "war room" look.
- `data/war_room.json` — the data itself. In production this path is a
  file *inside this repo*, committed by the running app; locally it's
  just a file on disk, gitignored so your own test runs don't get
  committed over it.

## Data model

- **Reports** — one per source, per league, per week: `source`
  (Claude/Grok/ChatGPT), `league` (College Football, NFL, Premier
  League, or Champions League — a source files a separate report for
  each, even in the same week, since the slates and analysis don't
  overlap), `report_date`, `week_number` (used for lining picks up
  week-to-week across sources/leagues), an optional free-text
  `week_label`, plus two free-text analysis fields straight off the
  report template — **Vegas Blind Spot** (teams the market keeps
  missing) and **LSU Objective Review** (no homer tax). Those two are
  commentary, not wagers, so they live on the report itself rather than
  as picks.
- **Picks** — the actual bets inside a report, each tagged with one of
  the five bettable categories from the template: Totals Radar, Thor
  Hammer Smash, Best Bet / Value Play, Sexy Moneyline, and Good-If-It-Goes
  Parlay — the same five categories regardless of league. Every pick
  records the matchup, selection, American odds, and stake (DraftKings
  for CFB/NFL, ESPN BET for EPL/UCL, per the sportsbook each league is
  actually priced against). Grade a pick Win/Loss/Push/Void from the
  report page and the app computes profit/loss off the odds
  automatically — no plus/minus math by hand. A pick pulled from the
  odds-lookup widget also carries a structured bet type/side/line plus
  the ESPN event id, which is what lets it be graded automatically later
  (see Auto-Grade below) instead of by hand; a pick typed in manually, or
  edited by hand after being pulled, just stays on manual grading.
  EPL/UCL picks can use `match_result` (a real 3-way home/draw/away
  market) alongside the same spread/total types CFB/NFL already use.

Units follow the report's own convention: **$100 staked = 1 unit.**

## Features

- **Dashboard** — three-way head-to-head cards (record, win rate, units,
  profit, ROI, total staked), a **win-rate-by-week line chart** and a
  weekly results table (the core "is one system trending better than the
  others" view), a cumulative-units-over-time chart, a picks-logged-by-
  category chart, a full category breakdown table, and a feed of the most
  recent picks across all three sources.
- **League tabs** — every view (dashboard, reports) can be filtered to
  All Leagues, College Football, NFL, Premier League, or Champions
  League, so you can see whether a source is stronger in one league than
  the others. **All Football** and **All Futbol** are two labels for the
  same combined view — Premier League + Champions League together — for
  whichever word you think in.
- **Reports** — every report ever logged, with its league, record, and
  profit at a glance, and a link into the full detail page (notes + picks
  + grading).
- **Add Report / Add Pick** — simple forms matching the report template's
  own categories and stake guidance (shown right in the dropdown).
- **Look Up Odds** — on the Add a Pick form, pick a date and hit "Find
  Games" to pull that day's slate (via ESPN's public API), then pick a
  game to see its live spread/total/moneyline as clickable buttons that
  auto-fill the Matchup, Selection, and Odds fields — no more typing
  lines in by hand or mistyping a number. Picks filled this way show a
  green `🔗 auto` tag in the picks table. For EPL/UCL this shows a real
  3-way home/draw/away market instead of a 2-way moneyline, and a
  **View Matchup Intel** link once a game is picked.
- **Team Intel** (EPL/UCL) — prediction research for one club or a
  specific matchup, at `/intel`: current league position and record,
  last-5 form, home/away W-D-L and goals split, full squad with age/
  nationality/status (and any injury flag ESPN's feed has — it's often
  thin, so treat it as a lead, not the last word), historical club
  managers, ESPN's own news for that club, local/beat-reporter coverage
  via Google News (regional papers, the club's own site — broader than
  ESPN's global desk), and — on a specific matchup page — ESPN's
  recent-meetings head-to-head and a match-day weather forecast
  (temperature, rain chance, wind) from Open-Meteo. All free, all live,
  no API keys. Everything here is read-only research; nothing on this
  page is stored or graded.
- **Latest** (`/latest`) — the most recent Premier League report and the
  most recent Champions League report, side by side, each with a link
  into its full page. The quickest way to check what the scheduled
  prediction-bot runs actually posted.
- **Auto-Grade Finished Games** — a button on the Reports page (grades
  every report) and on each report's own page (grades just that report)
  that checks every pending, `🔗 auto`-tagged pick's game for a final
  score and settles it — win/loss/push computed against the exact
  spread/total/moneyline line it was placed on, no typing in results by
  hand. Games still in progress are left pending and counted separately
  in the confirmation banner. Manually-typed picks (no `🔗 auto` tag)
  are never touched by this and still grade from the dropdown.
- **Runs on its own, too** — `.github/workflows/auto_grade.yml` hits the
  live app's auto-grade-all endpoint once an hour, every day, year-round,
  so finished games get settled without anyone clicking the button.
  Originally this only ran during the CFB/NFL football window, but
  EPL/UCL fixtures land on every day of the week for most of the year,
  so there's no clean "off" window left to skip. Free (GitHub Actions on
  a public repo), no secrets needed since the endpoint has no auth, and
  harmless to run when there's nothing to grade — it's a no-op. Trigger
  it by hand anytime from the repo's Actions tab (`workflow_dispatch`).

## Prediction bot (EPL / UCL)

Two scheduled Claude runs evaluate a full slate and log their own picks
automatically — Tuesday 8pm ET for Wednesday's Champions League games,
Friday 7pm ET for the weekend's Premier League games. The full process
(data gathered, markets evaluated, the 60%-confidence threshold, how
picks get assigned to the five categories, and how the report gets
submitted) is in
[`docs/prediction_bot_playbook.md`](docs/prediction_bot_playbook.md).
The bot writes through the same JSON API a human could script against:

- `GET /api/standings`, `/api/team_intel`, `/api/match_intel` — the Team
  Intel research, machine-readable.
- `GET /api/week` — the league's actual current week/matchweek (ESPN's
  own `week.number` for CFB/NFL; derived from the standings for EPL/UCL,
  which don't expose one directly). Also auto-fills Week # on the Add
  Report form as soon as a league/date is picked, so it's never
  hand-counted (and mislabeled) again.
- `POST /api/reports`, `POST /api/reports/<id>/picks` — create a report
  and its picks in one call each, no HTML form needed. Picks accept an
  optional `confidence` (0-100) alongside the usual fields; it's shown
  next to the pick but isn't used in grading or payout math.
- `PATCH /api/reports/<id>` — fix a report's own week_number/week_label/
  notes after the fact without touching its picks.

## What's next

- Add authentication if more than one person needs to log picks.
- Track closing-line value: snapshot the line pulled at pick time vs. the
  closing line, and compare that alongside raw win/loss.
- Team Intel is soccer-only right now (EPL/UCL) since it leans on ESPN's
  soccer-specific standings/schedule endpoints — CFB/NFL don't have an
  equivalent page yet.
- If this ever needs more concurrent writers than one commit-per-change
  comfortably supports, migrate `store.py` to a real database at that
  point — the JSON-in-git approach is a fine trade for staying free at
  this app's scale, not a permanent architectural bet.
