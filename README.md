# Football Betting War Room Dashboard

Tracks picks from the "Football Betting War Room" report format — logged
weekly from three AI sources (Claude, Grok, and ChatGPT), across both
College Football and the NFL — and compares how they're actually
performing against each other, week over week.

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
- `odds.py` — pulls live DraftKings lines (spread/total/moneyline) and
  game schedules from ESPN's public scoreboard API. No API key needed.
- `charts.py` — hand-rolled SVG line/bar charts, no JS library or CDN.
- `templates/` — the HTML pages.
- `static/style.css` — the LSU purple/gold "war room" look.
- `data/war_room.json` — the data itself. In production this path is a
  file *inside this repo*, committed by the running app; locally it's
  just a file on disk, gitignored so your own test runs don't get
  committed over it.

## Data model

- **Reports** — one per source, per league, per week: `source`
  (Claude/Grok/ChatGPT), `league` (College Football or NFL — a source
  files a separate report for each, even in the same week, since the
  slates and analysis don't overlap), `report_date`, `week_number` (used
  for lining picks up week-to-week across sources/leagues), an optional
  free-text `week_label`, plus two free-text analysis fields straight off
  the report template — **Vegas Blind Spot** (teams the market keeps
  missing) and **LSU Objective Review** (no homer tax). Those two are
  commentary, not wagers, so they live on the report itself rather than
  as picks.
- **Picks** — the actual bets inside a report, each tagged with one of
  the five bettable categories from the template: Totals Radar, Thor
  Hammer Smash, Best Bet / Value Play, Sexy Moneyline, and Good-If-It-Goes
  Parlay. Every pick records the matchup, selection, American odds, and
  stake (all bets assumed to be on DraftKings, per the template). Grade a
  pick Win/Loss/Push/Void from the report page and the app computes
  profit/loss off the odds automatically — no plus/minus math by hand.
  A pick pulled from the odds-lookup widget also carries a structured
  bet type/side/line plus the ESPN event id, which is what lets it be
  graded automatically later (see Auto-Grade below) instead of by hand;
  a pick typed in manually, or edited by hand after being pulled, just
  stays on manual grading.

Units follow the report's own convention: **$100 staked = 1 unit.**

## Features

- **Dashboard** — three-way head-to-head cards (record, win rate, units,
  profit, ROI, total staked), a **win-rate-by-week line chart** and a
  weekly results table (the core "is one system trending better than the
  others" view), a cumulative-units-over-time chart, a picks-logged-by-
  category chart, a full category breakdown table, and a feed of the most
  recent picks across all three sources.
- **League tabs** — every view (dashboard, reports) can be filtered to
  All Leagues, College Football only, or NFL only, so you can see whether
  a source is stronger in one sport than the other.
- **Reports** — every report ever logged, with its league, record, and
  profit at a glance, and a link into the full detail page (notes + picks
  + grading).
- **Add Report / Add Pick** — simple forms matching the report template's
  own categories and stake guidance (shown right in the dropdown).
- **Look Up DraftKings Odds** — on the Add a Pick form, pick a date and
  hit "Find Games" to pull that day's slate (via ESPN's public API), then
  pick a game to see its live DraftKings spread/total/moneyline as
  clickable buttons that auto-fill the Matchup, Selection, and Odds
  fields — no more typing lines in by hand or mistyping a number. Picks
  filled this way show a green `🔗 auto` tag in the picks table.
- **Auto-Grade Finished Games** — a button on the Reports page (grades
  every report) and on each report's own page (grades just that report)
  that checks every pending, `🔗 auto`-tagged pick's game for a final
  score and settles it — win/loss/push computed against the exact
  spread/total/moneyline line it was placed on, no typing in results by
  hand. Games still in progress are left pending and counted separately
  in the confirmation banner. Manually-typed picks (no `🔗 auto` tag)
  are never touched by this and still grade from the dropdown.

## What's next

- Add authentication if more than one person needs to log picks.
- Track closing-line value: snapshot the line pulled at pick time vs. the
  closing line, and compare that alongside raw win/loss.
- Auto-refresh: a scheduled job that hits Auto-Grade automatically (e.g.
  once daily) instead of requiring a manual click.
- If this ever needs more concurrent writers than one commit-per-change
  comfortably supports, migrate `store.py` to a real database at that
  point — the JSON-in-git approach is a fine trade for staying free at
  this app's scale, not a permanent architectural bet.
