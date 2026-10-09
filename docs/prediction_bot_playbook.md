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

**This run must never finish silently.** Several runs in a row produced
nothing at all — no report, no board leans, nothing visible on the
dashboard — even though the slate had real games every single time.
That is a bug, not a legitimate "no edge this week" outcome. Step 1
below is a hard checkpoint, not a formality: once you confirm real
games exist (nearly always true), you are committed to finishing steps
2-5 for the whole slate and leaving a visible trace — at minimum, a
report with board leans logged for every game you researched, even in
a week with zero real-money picks. Ending the run with nothing
submitted, while games existed, is the one outcome this playbook
exists to rule out.

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

If this comes back empty for every target date, double check you used
the right dates before concluding there's a genuine fixture-free week
(an international break) — that's rare, and it's the only legitimate
reason to end the run without a report.

## 2. Gather everything on each remaining game

For every game still to be played, pull all of this before forming an
opinion — don't skip straight to the odds:

1. **The line itself, pulled now and noted with a timestamp:**
   `GET /api/odds?league=<L>&event_id=<id>`
   → `home_spread`/`home_spread_odds`/`away_spread_odds`,
   `total`/`over_odds`/`under_odds`,
   `home_moneyline`/`away_moneyline`/`draw_moneyline`.
   A missing/404 result means no line is posted yet for that game — skip
   markets you don't have a number for; don't invent one. Note what you
   pulled here — you'll re-pull it in step 3.5 to check for movement.

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

Work through every game in this order. A memo scores far more sides than
it ever recommends — every side you evaluate here gets a score, and most
of them end up as board leans (step 4), not picks.

1. **The fair number — built from more than one lens, not a single gut
   call.** Form at least two independent reads before you touch the
   market, and note where they land:
   - **A form/underlying-performance lens:** table position, last-5
     form, goal difference, home/away split, head-to-head — the
     statistical read.
   - **A context lens:** squad news, manager, motivation, schedule
     congestion, weather — the situational read, especially anything
     that wouldn't show up in a pure form model (a suspension, a
     relegation/European-spot incentive, a fixture pile-up).
   If the two lenses land in the same place, say so ("both reads have
   City comfortably ahead"); if they pull apart, say that too and
   explain which one you're weighting more and why ("form favors City
   but the context lens flags Spurs' injury list as decisive — weighting
   context here"). For a spread/total this becomes a line estimate with
   a range (e.g. "Man City -2, range -1.5 to -3"); for a 3-way
   `match_result`, a probability split (e.g. "City 70% / Draw 18% /
   Coventry 12%"). This becomes `war_room_line`.

   **Weight your own number against the market, not instead of it —
   especially early in a season.** The posted line already prices in
   information you don't have (public money, sharper models, insider
   line moves) — treat it as a third lens, not noise to override. How
   much to trust your own number over the market should scale with how
   much data you actually have: use `games_played / (games_played + 6)`
   (capped at 0.5) as your own number's weight, and blend it with the
   market's own implied number at that weight. Two games into a season
   that's a weight around 0.25 — your blended number should sit closer
   to the market's than to your raw read. By mid-season (~18+ games) it
   approaches the 0.5 cap — an even blend, never fully overriding the
   market on this app's own data alone.

   **For UCL specifically, `games_played` means each team's current
   domestic-league games played this season — not the UCL standings'
   own `played` field.** The UCL league-phase table resets to 0 at
   the start of every season regardless of how far into their
   domestic campaigns the teams actually are, so reading it literally
   zeroes out your own-number weight for every side on Matchday 1 (and
   keeps it artificially low for the rest of the early league phase)
   even though the teams have real, current-season form to draw on.
   Pull each team's domestic `played` count instead — from
   `/api/standings?league=EPL` for EPL sides, and via web research
   (official league site or a quick search of the current table) for
   every other domestic league, since this app only tracks EPL/UCL
   standings natively. A team with a long-running non-European-calendar
   season (e.g. Norway's Eliteserien, which runs March–December) will
   already have far more domestic games played than a team in a
   fresh European autumn-start league — use the real count either way,
   it's still capped at 0.5.

2. **The edge and the EV — both as numbers, not feelings.** Convert the
   posted American odds to an implied probability (`100/(odds+100)` for
   a dog, `-odds/(-odds+100)` for a favorite), and compare it to your
   blended probability from step 1. The gap is your edge — this becomes
   `edge` (e.g. "City's price implies ~62%; blended estimate ~68% →
   +6pp edge"). Then convert that into an actual expected value: with
   `d` the decimal odds (`d = odds/100 + 1` for a dog, `d = 100/-odds + 1`
   for a favorite) and `p` your blended probability, `EV% = p*d - 1`.
   State both the edge in points and the EV% in the `edge` field (e.g.
   "+6pp edge, EV +4.1%") — they usually move together but a short-price
   favorite can carry a real edge in points and still a thin EV%, which
   matters for step 3 below. If edge and EV are both basically zero, say
   so plainly ("no edge, pricing looks fair") rather than inventing
   daylight that isn't there. Also record your own **confidence** (0-100,
   your calibrated probability that this side is correct) — this becomes
   the pick's `wr_confidence`, the same field every other source's picks
   carry (see step 5).

3. **Rank by edge, then split into three tiers by score and EV — not
   one hard gate.** A no-pick week is not an acceptable default outcome;
   the goal is roughly **3 real-stake picks a week**, but plenty of
   other scored sides still belong on the board at $0. Across the whole
   slate:
   - Sort every side with genuine, sourced positive edge from strongest
     to weakest.
   - **Real stake:** confidence ≥ 60% AND EV ≥ +2%. Take the strongest
     ~3 of these as your real-money picks, sized per step 4's table.
   - **Tracked, not staked:** a real, sourced edge that falls short of
     either bar above (e.g. confident but thin EV on a short price, or
     decent EV but confidence under 60%) — still submit it as a pick
     (so it's visible, graded, and feeds CLV/calibration), but at
     `"stake": 0`. Say so plainly in the notes — "a lean, not a stake"
     or "no bet without [the missing piece]" reads fine and is exactly
     what this tier is for. This is the tier that replaces forcing real
     money onto a borderline side just to hit a pick count.
   - **Everything else you scored** — no real edge, or edge too thin to
     bother tracking — becomes a board lean (step 4), not a pick.
   - If the whole slate genuinely has nothing with real, sourced edge
     anywhere (every price already looks fair once blended against your
     own number), zero real-stake picks is a legitimate outcome — but
     it should be rare, and it does **not** mean posting nothing: you
     still owe a report (zero or $0-tracked picks) plus board leans for
     the slate. Don't manufacture edge to avoid a thin week, and don't
     confuse "I like this team" with "this price is wrong."

4. **A context check — is there a structural reason the numbers are
   wrong for this specific matchup?** A new manager's tactical shift, a
   fixture pile-up, a squad story, a team that's been unlucky (or
   lucky) relative to its underlying performance. If you invoke this to
   override what steps 1–3 said, say exactly what the adjustment is and
   why in the pick's notes, with a source and how recent it is (a knock
   reported an hour ago from the official club site outweighs a stat
   from August) — an overlay must be explainable and dated, not just
   asserted. Skip this step when nothing structural applies; most games
   don't need it.

5. **The line-movement and execution check — what you're actually about
   to submit.** Re-pull the odds one more time (step 2.1) immediately
   before you submit, in case the number moved while you were
   researching. If it moved meaningfully since your first pull and you
   don't have a sourced reason for the move (official news, a lineup
   leak, anything citable), treat the new price with suspicion rather
   than reflexively fading it or chasing it — note the move and the
   absence of a reason in the pick's notes, and let that pull your
   confidence down rather than up. If you do have a sourced reason, cite
   it. Either way, submit against the live number, not the one you
   started analyzing with.

A few markets have effectively lumpy outcomes rather than a smooth
curve of probabilities — a 1-0 or 2-1 scoreline is far more common than
the spacing between scorelines suggests, so a spread/handicap pick that
wins by exactly the margin it needed (rather than comfortably) is a
normal, expected outcome, not a fluke to second-guess. Don't let a
narrow previous result talk you into being more conservative than your
actual numbers support on the next pick.

## 4. Decide what to recommend

Take the real-stake and tracked-not-staked sides step 3 identified and
assign each to a category, using the same five categories the rest of
this app already uses for CFB/NFL:

| Category | Guidance |
|---|---|
| **Totals Radar** (`totals_radar`) | Up to 3 per report, `total` market picks only, selective. |
| **Thor Hammer Smash** (`thor_hammer`) | $500 / 5 units, extremely rare — reserve for a genuine standout (typically 80%+ confidence with strong corroborating evidence), not one per slate by default. Zero is a normal outcome most weeks. |
| **Best Bet / Value Play** (`best_bet`) | $50–150, the core "real edge, good number" play(s) — your highest-conviction match_result or spread picks land here. |
| **Sexy Moneyline** (`sexy_moneyline`) | $25 normally, $50 max — a live underdog or draw pick at a big price, not a favorite. |
| **Good-If-It-Goes Parlay** (`parlay`) | $10 max, 3+ legs, one per report — combine legs you'd each independently back, not filler. |

**Stake by tier, not by sliding confidence:**
- **Real stake** (confidence ≥ 60% AND EV ≥ +2%): the category's normal
  range, scaled within it by how strong the pick is — 70%+ confidence
  near the top of the range, 60-69% toward the lower end.
- **Tracked, not staked:** `"stake": 0` regardless of category. It still
  needs a category (pick the closest fit) and still needs
  `bet_type`/`bet_side`/`bet_line`/`espn_event_id` so it grades
  automatically — only the dollar amount is zero.

`thor_hammer` is the one exception — it stays reserved for genuine
80%+ standouts with real stake; never put a tracked-not-staked side
there no matter how strong its edge looks.

Don't duplicate the same game+market+side across two categories. It's
fine for a category to end up with fewer than 3, or for every real-stake
pick to be $0 tracked in a genuinely thin week.

## 5. Submit the report and picks

Get `week_number` from `GET /api/week?league=<L>` — it returns the
league's actual current matchweek (`{"week": 3}`), the number a fan
would recognize, not an arbitrary counter. Use this rather than
computing your own guess.

Leave `week_label` **empty** unless you have something genuinely
descriptive to add beyond the number — the dashboard already renders
"Week 3 — Premier League" on its own from `week_number` + `league`
wherever a view can show more than one league at once, and a set
`week_label` overrides that generated text instead of adding to it.

`philosophy` is a short one-line tagline shown under the report header
(e.g. "Form over reputation, real edges over vibes, no forced action.")
- optional, but a nice touch that sets the tone for the slate; leave it
out rather than forcing one if nothing fits.

`blind_spot_notes` is where the report's narrative voice lives — name
the single best near-miss on the slate even when it's a pass (what
score and EV it carried, exactly which bar it failed), not just "nothing
cleared this week." That near-miss is often more useful than the picks
themselves for judging whether the process is working.

```
POST /api/reports
Content-Type: application/json
{
  "source": "Claude - GB",    // NOT "Claude" - that's the separate CFB/NFL source;
                               // "Claude - GB" is this bot's own source, tracked separately
  "league": "UCL",            // or "EPL"
  "report_date": "YYYY-MM-DD", // today, ET
  "week_number": <matchweek>,
  "week_label": "",            // leave blank - see above
  "philosophy": ""              // optional one-line tagline, or omit
}
→ {"id": <report_id>}
```

Then, once per recommended pick (real-stake or tracked-not-staked):

```
POST /api/reports/<report_id>/picks
Content-Type: application/json
{
  "category": "best_bet",              // one of the five slugs above
  "matchup": "Tottenham Hotspur @ Manchester City",
  "selection": "Manchester City to Win",
  "odds": -135,                        // the American price you pulled in step 2
  "stake": 100,                        // or 0 for a tracked-not-staked side - see step 3.3
  "wr_confidence": 68,                 // your 0-100 estimate from step 3.2 - the app's one confidence
                                        // score, same field every other source's picks use; the app
                                        // layers CLV/track-record/agreement on top of this automatically
  "war_room_line": "City 70% / Draw 18% / Spurs 12% (blended)",  // your own number - step 3.1
  "edge": "City's price implies ~62%; blended estimate 70% -> +8pp edge, EV +5.7%",  // step 3.2
  "notes": "Man City unbeaten in 9 at home.\nSpurs missing both starting CBs per official injury news.\nSpurs also winless and scoreless through 2 games.",
  "price_discipline": "-135 or better: $100\n-150 to -136: $50\n-155 or worse: pass",
  "bet_type": "match_result",          // "match_result" | "total" | "spread"
  "bet_side": "home",                  // match_result: home/draw/away; total: over/under; spread: home/away
  "bet_line": null,                    // the posted number for total/spread; null for match_result
  "espn_event_id": "740613",
  "home_team": "Manchester City",
  "away_team": "Tottenham Hotspur"
}
→ {"id": <pick_id>}
```

`notes` renders as one bullet per line (`\n`-separated) under "The Case"
on the report - write it that way, short independent points, not one
long paragraph. `price_discipline` renders the same way under "Price
Discipline" - one stake tier per line, worst case last (a "pass" tier is
fine and often correct). Both are optional but this is most of what
makes the report worth reading, so skipping them should be rare, not the
default.

Use the exact `bet_type`/`bet_side`/`bet_line`/`espn_event_id` from the
line you pulled — this is what lets the hourly auto-grade job settle the
pick automatically once the match finishes. A 400 response means a field
is wrong (bad category, missing required field) — fix and retry that one
pick; don't abandon the rest of the slate over one bad request.

If you catch a mistake after submitting, fix it in place rather than
deleting and resubmitting:
- `PATCH /api/reports/<report_id>` for the report's own `source`,
  `week_number`, `week_label`, `philosophy`, `blind_spot_notes`, or
  `lsu_review_notes`. Double-check `source` is `"Claude - GB"` if you
  ever catch it posted as plain `"Claude"` (that's the CFB/NFL source,
  a different bucket entirely).
- `PATCH /api/reports/<report_id>/picks/<pick_id>` for a pick's `notes`,
  `wr_confidence`, `war_room_line`, `edge`, or `price_discipline`.

Both take a JSON body of just the field(s) to change and leave
everything else untouched. Neither can touch a report's `league`/
`report_date`, or a pick's `odds`/`stake`/`bet_type`/`bet_side`/
`bet_line`/`espn_event_id` - a mistake there needs a delete + resubmit
instead.

## 6. Board leans — everything else you scored

After the report and its picks are submitted, log every other side you
actually evaluated and scored in step 3 (0-100) as a board lean — the
sides that didn't make the pick cut, not just close calls. This is the
single biggest gap this playbook had: a memo scores far more games than
it recommends, and those unrecommended scores are real data this app
can use even though they're not picks.

```
POST /api/reports/<report_id>/leans
Content-Type: application/json
[
  {
    "matchup": "Brentford @ Aston Villa",
    "selection": "Aston Villa -1",
    "bet_type": "spread",             // "spread" | "total" | "moneyline" | "match_result"
    "bet_side": "home",
    "bet_line": -1,                    // null for moneyline/match_result
    "odds": -110,                      // optional, defaults to -110
    "score": 54,                       // your 0-100 score from step 3.2 for this side
    "espn_event_id": "401878776",
    "note": "Pass; 2-lens blend has Villa -0.6, market -1 -- not enough gap to act on."
  }
]
→ {"ids": [...], "created": <n>, "skipped": <n>}
```

Send one request per report with the full list of rows (a JSON array),
not one call per lean. A row is automatically skipped (not an error) if
you already logged a pick on that exact game+market, so there's no need
to filter picks out yourself — just include every side you scored, picks
and leans alike, and let the server sort it out. These never touch
`wr_confidence`'s picks-only sibling stats, `record`, or `profit_loss` —
they exist purely to train the WR probability calibration and to surface
cross-source agreement the pick-only view can't see (see
`aligned_leans()` on the dashboard).

Covering the real breadth of the slate here matters more than covering
it exhaustively to the last game — on a 10-game Premier League weekend,
scoring and logging a lean for every side of every market you actually
formed a view on (usually most of the slate) is the bar, the same way
the CFB desk logs a board lean for essentially every game it looked at.

## 7. Done

Grading happens on its own via the hourly `auto_grade_all` GitHub
Action, and so does everything downstream of a pick's `wr_confidence`:
the same hourly pass snapshots pregame lines for closing-line value, and
that CLV feeds into the pick's *live* War Room Confidence Score
(`wr_confidence_effective`) alongside weather, recent form, injuries,
home/away splits, quality of wins, cross-source agreement, and each
source's own track record. What you submit as `wr_confidence` is the
one-time, frozen starting number (what the research actually said the
day the pick went out); the dashboard shows that live-adjusted number
everywhere afterward, and it's what `confidence_locks()` and the Lock
tier are judged against — not a separate bot-only confidence system.
Board leans feed the same WR probability calibration fit (see
`wr_calibration_sample()`), with their own indicator term so a lean's
probability read is never confused with a pick's.

Before ending the run, confirm you have actually submitted something:
a report (even one with zero real-stake picks), and board leans covering
the slate you researched. A run that finds real games in step 1 and
ends without posting either of those did not do its job, regardless of
what the research concluded — fix that before finishing, not after.
