"""
Football Betting War Room Dashboard
------------------------------------
Tracks picks from the "Football Betting War Room" report format, produced
weekly by three different AI sources (Claude, Grok, and ChatGPT) across
both College Football and the NFL, and compares how they perform against
each other - including whether any one of them is trending better or
worse week over week.

No database: data is a single JSON blob, committed straight into this
repo via the GitHub Contents API (see store.py) - a git commit is a fine
row store at this app's scale, and it survives Render's free tier having
no persistent disk. Locally (no GITHUB_PAT set), the same JSON just lives
in a file on disk instead.

Shape of this file:
  1. Imports & setup       - tools we're borrowing (Flask, store, etc.)
  2. Constants              - categories, sources, leagues, colors
  3. Betting math helpers   - American-odds payout + grading + aggregation
  4. Routes                 - one function per URL/page the app serves
  5. `if __name__ == ...`   - the line that actually starts the server
"""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

from flask import Flask, jsonify, redirect, render_template, request, url_for

import charts
import odds
import store

app = Flask(__name__)

# ---------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------
# $100 staked = 1 "unit", matching the report's own convention.
UNIT_SIZE = 100.0

# CFB/NFL picks are priced against DraftKings, per the original report
# template. EPL/UCL picks are priced against ESPN BET instead - ESPN's
# soccer feed doesn't carry DraftKings lines, and ESPN BET is the one
# odds.py sources for those two leagues (see odds.LEAGUE_CONFIG).
SPORTSBOOK = "DraftKings"

# The bettable pick types from the report template, in report order. Each
# report can also carry two non-bet analysis sections (Vegas Blind Spot,
# LSU Objective Review) - those are free-text notes on the report itself,
# not picks, since they're commentary rather than wagers.
CATEGORIES = {
    "totals_radar": {
        "label": "Totals Radar",
        "icon": "\U0001F4CA",
        "color": "#60a5fa",
        "guidance": "Up to 3 per report · selective over/unders",
    },
    "thor_hammer": {
        "label": "Thor Hammer Smash",
        "icon": "⚡",
        "color": "#f4c430",
        "guidance": "$500 · 5 units · extremely rare",
    },
    "best_bet": {
        "label": "Best Bet / Value Play",
        "icon": "\U0001F48E",
        "color": "#38bdf8",
        "guidance": "$50–150 · real edge, good number",
    },
    "sexy_moneyline": {
        "label": "Sexy Moneyline",
        "icon": "\U0001F525",
        "color": "#f97316",
        "guidance": "$25 normally · $50 max · big dog, big price",
    },
    "parlay": {
        "label": "Good-If-It-Goes Parlay",
        "icon": "\U0001F3AB",
        "color": "#d4a373",
        "guidance": "$10 max · 3+ legs · one per report",
    },
}
CATEGORY_ORDER = list(CATEGORIES.keys())

# The three AI sources being compared. Order here controls display order
# everywhere (cards, chart legends, table columns).
SOURCES = ["Claude", "Grok", "ChatGPT"]
SOURCE_STYLE = {
    "Claude": {"color": "#cc785c"},
    "Grok": {"color": "#38bdf8"},
    "ChatGPT": {"color": "#10a37f"},
}

# Every report belongs to one league - a source writes a separate report
# per league, even in the same week, since the slates (and the analysis
# behind them) don't overlap. EPL/UCL share the same report template and
# pick categories as CFB/NFL; only the odds market (odds.py) and the
# research data behind a pick (the Team Intel page) differ by sport.
LEAGUES = {"CFB": "College Football", "NFL": "NFL", "EPL": "Premier League", "UCL": "Champions League"}
LEAGUE_ORDER = list(LEAGUES.keys())
SOCCER_LEAGUES = frozenset(code for code in LEAGUE_ORDER if odds.is_soccer(code))
AMERICAN_LEAGUES = frozenset(code for code in LEAGUE_ORDER if not odds.is_soccer(code))

# Combined filters on top of the individual leagues above - "All
# Football" is CFB+NFL together, "All Futbol" is EPL+UCL together, so a
# viewer can jump to either sport without picking through its two
# individual league tabs one at a time. {key: (tab label, {league codes})}.
LEAGUE_GROUPS = {
    "football": ("All Football", AMERICAN_LEAGUES),
    "futbol": ("All Futbol", SOCCER_LEAGUES),
}

RESULTS = ["pending", "win", "loss", "push", "void"]
RESULT_LABELS = {
    "pending": "Pending",
    "win": "Win",
    "loss": "Loss",
    "push": "Push",
    "void": "Void",
}
RESULT_COLORS = {
    "pending": "#94a3b8",
    "win": "#22c55e",
    "loss": "#ef4444",
    "push": "#eab308",
    "void": "#64748b",
}

# The WR Confidence Score (0-100) at and above which a pick is "Lock"
# tier - see confidence_locks() below, which keys off this one constant
# so the dashboard's Locks section and a report's own WR badge always
# agree on what counts as a lock.
LOCK_CONFIDENCE = 90

# How many sources need to land on the same side of the same game/market
# for war_room_locks() to call it a consensus pick. Used to be "all
# three" - lowered to two since two-of-three agreeing is itself a real
# signal worth surfacing, not just a unanimous sweep.
CONSENSUS_MIN_SOURCES = 2

# --- WR Confidence Score adjustments -----------------------------------
# The score entered on a pick (see create_pick()) is a starting point, not
# the final word - it's adjusted live (never stored back onto the pick)
# by three things the source itself doesn't know about at write time:
#   - that source's own track record so far this season (WR_RECORD_*)
#   - whether other sources agree or conflict on the same game/market
#     (WR_AGREEMENT_BONUS / WR_CONFLICT_PENALTY)
#   - fresh news on either team, for a pending bet (WR_NEWS_*)
# See wr_confidence_effective() below for how these combine.

# A pick that was never much of a conviction play to begin with
# shouldn't swing much either way on a track record or a conflicting
# source - and a pick at or below this floor gets no adjustment at all,
# positive or negative. WR_IMPACT_SCALE() ramps linearly from 0 at the
# floor to 1 at 100, and every modifier below is multiplied through it.
WR_IMPACT_FLOOR = 50


def wr_impact_scale(base):
    """0 at WR_IMPACT_FLOOR or below, ramping linearly to 1 at a base score of 100."""
    if base is None or base <= WR_IMPACT_FLOOR:
        return 0.0
    return min((base - WR_IMPACT_FLOOR) / (100 - WR_IMPACT_FLOOR), 1.0)


WR_RECORD_MIN_SETTLED = 8  # below this many decided picks, a source's win% is too small a sample to trust
WR_RECORD_SCALE = 0.4  # points of adjustment per percentage-point of win% above/below 50
WR_RECORD_CAP = 10  # max swing from track record alone, either direction

WR_AGREEMENT_BONUS = 6  # per other source on the same side of the same game/market
WR_AGREEMENT_CAP = 12
WR_CONFLICT_PENALTY = 8  # per other source on the opposite side of the same game/market
WR_CONFLICT_CAP = 16

# Live-only (see wallet_news_alerts()): nudges the score shown next to a
# News Watch matchup, on top of the two adjustments above. Never touches
# the frozen wr_confidence_at_bet on an already-logged bet - that number
# is a snapshot of the moment the bet was placed and stays put by design.
WR_NEWS_POSITIVE_BONUS = 4
WR_NEWS_NEGATIVE_PENALTY = 6
WR_NEWS_CAP = 12

# How often capture_pregame_lines() will re-snapshot the same still-
# upcoming pick's line. There's no scheduled job sampling odds right at
# kickoff (this app is just a web service - see the module docstring),
# so this piggybacks on Auto-Grade instead: every time it runs, any
# pending pick whose game hasn't started yet gets its current line
# recaptured, throttled to this often, and whatever was captured last
# before the game actually starts becomes the de facto "closing" line -
# only as good as how recently before kickoff Auto-Grade happened to run.
PREGAME_ODDS_RECAPTURE_MINUTES = 15


def wr_confidence_label(score):
    """
    A WR Confidence Score (0-100) as a text tier, or None if unscored.
    This is the War Room's own holistic read on a pick - today, a
    judgment call weighing the edge, the roster notes, and the price
    discipline together (more structured inputs fold in later) - and,
    for EPL/UCL, the same number the prediction model itself reports:
    one score per pick, not two, since it's the same model either way.
    """
    if score is None:
        return None
    if score >= 85:
        return "Elite"
    if score >= 70:
        return "Strong"
    if score >= 55:
        return "Playable"
    return "Thin"


app.jinja_env.globals.update(
    categories=CATEGORIES,
    category_order=CATEGORY_ORDER,
    sources=SOURCES,
    source_color=lambda s: SOURCE_STYLE.get(s, {}).get("color", "#8b94a7"),
    leagues=LEAGUES,
    league_order=LEAGUE_ORDER,
    soccer_leagues=SOCCER_LEAGUES,
    league_group_order=list(LEAGUE_GROUPS.keys()),
    league_group_label=lambda key: LEAGUE_GROUPS.get(key, (key,))[0],
    results=RESULTS,
    result_label=lambda r: RESULT_LABELS.get(r, r),
    result_color=lambda r: RESULT_COLORS.get(r, "#8b94a7"),
    sportsbook=SPORTSBOOK,
    sportsbook_for=lambda league: odds.sportsbook_for(league) or SPORTSBOOK,
    lock_confidence=LOCK_CONFIDENCE,
    wr_confidence_label=wr_confidence_label,
    unit_size=UNIT_SIZE,
)


# ---------------------------------------------------------------------
# Betting math helpers
# ---------------------------------------------------------------------
def american_profit(stake, odds_value):
    """Profit (not counting the returned stake) on a winning bet."""
    if odds_value >= 0:
        return stake * odds_value / 100.0
    return stake * 100.0 / abs(odds_value)


def profit_for_result(stake, odds_value, result):
    if result == "win":
        return american_profit(stake, odds_value)
    if result == "loss":
        return -stake
    # push, void, pending: no money won or lost
    return 0.0


def grade_pick(pick, final):
    """
    Win/loss/push for one pick against a final score, per its structured
    bet_type/bet_side/bet_line (set only for picks whose odds were pulled
    from /api/games + /api/odds, e.g. via create_pick). Spread/total
    lines use the standard "push on an exact tie" rule; two-way moneyline
    pushes only on an actual tied score - that's the right rule for
    CFB/NFL, where a tie is a fluke. Soccer uses "match_result" instead:
    a real 3-way market (home/draw/away) where a draw is its own
    outcome, never a push.
    """
    home, away = final["home_score"], final["away_score"]
    side, line = pick.get("bet_side"), pick.get("bet_line") or 0.0

    if pick.get("bet_type") == "moneyline":
        if home == away:
            return "push"
        home_won = home > away
        return "win" if (home_won if side == "home" else not home_won) else "loss"

    if pick.get("bet_type") == "match_result":
        if side == "draw":
            return "win" if home == away else "loss"
        if side == "home":
            return "win" if home > away else "loss"
        if side == "away":
            return "win" if away > home else "loss"

    if pick.get("bet_type") == "spread":
        margin = (home - away) if side == "home" else (away - home)
        adjusted = margin + line
        return "win" if adjusted > 0 else "push" if adjusted == 0 else "loss"

    if pick.get("bet_type") == "total":
        total = home + away
        if side == "over":
            return "win" if total > line else "push" if total == line else "loss"
        if side == "under":
            return "win" if total < line else "push" if total == line else "loss"

    return None  # unrecognized bet_type - leave it pending


def auto_grade_pending(data, report_id=None):
    """
    Grade every pending pick (in `data`, modified in place) that carries
    structured ESPN bet data and whose game has finished. Returns
    (graded, still_pending) - the latter covers games not yet final and
    any it couldn't look up at all.
    """
    reports = {r["id"]: r for r in data["reports"]}
    candidates = [
        p
        for p in data["picks"]
        if p["result"] == "pending"
        and p.get("espn_event_id")
        and p.get("bet_type")
        and (report_id is None or p["report_id"] == report_id)
    ]

    score_cache = {}
    graded = still_pending = 0
    for pick in candidates:
        report = reports.get(pick["report_id"])
        if not report:
            still_pending += 1
            continue

        key = (report["league"], pick["espn_event_id"])
        if key not in score_cache:
            score_cache[key] = odds.final_score(report["league"], pick["espn_event_id"])
        final = score_cache[key]

        if not final or not final["completed"]:
            still_pending += 1
            continue

        result = grade_pick(pick, final)
        if result is None:
            still_pending += 1
            continue

        pick["result"] = result
        pick["profit_loss"] = profit_for_result(pick["stake"], pick["odds"], result)
        graded += 1

    return graded, still_pending


def _parallel_map(fn, items, max_workers=8):
    """
    Run fn(item) for every item concurrently and return {item: result}.
    These calls are all I/O-bound network round-trips to ESPN, so plain
    threads (not real parallelism, but enough while waiting on sockets)
    turn what used to be N sequential round-trips into roughly one
    round-trip's worth of wall-clock time. A failing call yields None
    for that item rather than blowing up the whole batch.
    """
    if not items:
        return {}
    results = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as pool:
        future_to_item = {pool.submit(fn, item): item for item in items}
        for future in as_completed(future_to_item):
            item = future_to_item[future]
            try:
                results[item] = future.result()
            except Exception:
                results[item] = None
    return results


def _pregame_odds_stale(pick):
    captured_at = pick.get("pregame_odds_captured_at")
    if not captured_at:
        return True
    try:
        captured = datetime.fromisoformat(captured_at)
    except ValueError:
        return True
    return (datetime.utcnow() - captured).total_seconds() > PREGAME_ODDS_RECAPTURE_MINUTES * 60


def capture_pregame_lines(data):
    """
    Best-effort line-movement tracking (see PREGAME_ODDS_RECAPTURE_MINUTES):
    for every pending, ESPN-linked pick whose game ESPN still shows as
    not-yet-started, snapshots its current line via odds.game_odds() onto
    the pick as `pregame_odds`/`pregame_odds_captured_at` - throttled so a
    pick that's still days out doesn't get re-fetched on every call. Once
    a game starts, game_odds() stops returning anything for it anyway, so
    the pick's last snapshot naturally stays put - see line_move() for
    how a pick's original bet_line is compared against it. Modifies
    `data` in place; returns how many picks got a fresh snapshot.
    """
    reports = {r["id"]: r for r in data["reports"]}
    eligible = {
        p["id"]: p
        for p in data["picks"]
        if p["result"] == "pending" and p.get("espn_event_id") and p.get("bet_type") and p["report_id"] in reports
    }
    if not eligible:
        return 0

    states = _parallel_map(
        lambda pid: odds.final_score(reports[eligible[pid]["report_id"]]["league"], eligible[pid]["espn_event_id"]),
        list(eligible.keys()),
    )
    due_ids = [
        pid
        for pid, state in states.items()
        if state and state.get("state") == "pre" and _pregame_odds_stale(eligible[pid])
    ]
    if not due_ids:
        return 0

    lines = _parallel_map(
        lambda pid: odds.game_odds(reports[eligible[pid]["report_id"]]["league"], eligible[pid]["espn_event_id"]),
        due_ids,
    )
    captured = 0
    for pid, line in lines.items():
        if not line:
            continue
        pick = eligible[pid]
        pick["pregame_odds"] = line
        pick["pregame_odds_captured_at"] = datetime.utcnow().isoformat(timespec="seconds")
        captured += 1
    return captured


def line_move(pick):
    """
    Closing-line value (CLV) for this pick: how much better or worse
    pick["bet_line"] was than whatever line was last captured before
    kickoff (see capture_pregame_lines) - the closest thing to true CLV
    this app can track without a scheduled job (it's "as of the last time
    someone checked the site before kickoff", not a guaranteed minute-of-
    kickoff read - see PREGAME_ODDS_RECAPTURE_MINUTES). Returns None if
    there's nothing to compare (no snapshot yet, or this pick's market
    isn't spread/total - moneyline/match_result CLV would need American-
    odds-to-probability math this app doesn't do yet). Positive means the
    number at bet time required less than the closing number would have
    (a better price than showing up right before kickoff); negative means
    the closing number was easier to clear than the one actually bet.
    """
    snapshot = pick.get("pregame_odds")
    if not snapshot or pick.get("bet_line") is None:
        return None

    bet_type, side = pick.get("bet_type"), pick.get("bet_side")
    if bet_type == "spread":
        current = snapshot.get("home_spread")
        if current is None:
            return None
        # home_spread is from the home team's perspective; an away-side
        # bet's line runs the opposite direction from the home number.
        # Both bet_line and current_for_side are "points added to this
        # side's margin" (see grade_pick) - the bigger that number, the
        # easier the cover, so a smaller closing number than what was bet
        # means the bet got the easier (better) side of the move.
        current_for_side = current if side == "home" else -current
        return pick["bet_line"] - current_for_side

    if bet_type == "total":
        current = snapshot.get("total")
        if current is None:
            return None
        # A lower total favors the over (less to clear); a higher total
        # favors the under (more room before it clears). So a closing
        # number that moved toward the other side of bet_line is the
        # good direction for whichever side was actually bet.
        delta = current - pick["bet_line"]
        return delta if side == "over" else -delta

    return None


app.jinja_env.globals["line_move"] = line_move


def attach_game_status(picks, league=None):
    """
    Score-badge copies of `picks`: for each ESPN-linked pick whose game
    has actually started, adds a `game` dict {status, label, score:
    'AWAY-HOME', final: bool} - regardless of whether the pick itself is
    still pending or already settled:
      - pending + game in progress -> a live preview ("if the game ended
        right now, is this pick good") computed with grade_pick, never
        touching the stored result - that only happens for real once
        Auto-Grade runs.
      - pending + game already final -> same preview, flagged final (a
        nudge that Auto-Grade hasn't run on it yet).
      - already settled (win/loss/push) -> the actual final score shown
        for context, using the pick's own stored result rather than
        recomputing anything.
    A game not yet started, a void pick, or a pick with no espn_event_id
    gets no badge (a 0-0 preview would be meaningless). `league` is used
    for every pick if given (report_detail, one league for the whole
    page); otherwise each pick's own 'league' key is used (the
    dashboard's cross-league recent list).

    Every distinct (league, event_id) that actually needs a live score
    is fetched in parallel up front - with a dozen-plus pending picks
    across a dozen-plus different games, one round-trip at a time was
    the single biggest thing slowing every page that shows recent picks.
    """
    settled_labels = {"win": "Won", "loss": "Lost", "push": "Push"}
    live_labels = {"win": "Winning", "loss": "Losing", "push": "Push"}

    needed_keys = set()
    for original in picks:
        pick_league = league or original.get("league")
        if (
            original["result"] != "void"
            and original.get("espn_event_id")
            and original.get("bet_type")
            and pick_league
        ):
            needed_keys.add((pick_league, original["espn_event_id"]))

    score_cache = _parallel_map(lambda key: odds.final_score(key[0], key[1]), list(needed_keys))

    result = []
    for original in picks:
        pick = dict(original)
        pick["game"] = None
        pick_league = league or pick.get("league")
        if (
            pick["result"] != "void"
            and pick.get("espn_event_id")
            and pick.get("bet_type")
            and pick_league
        ):
            key = (pick_league, pick["espn_event_id"])
            final = score_cache.get(key)
            if final and final["state"] in ("in", "post"):
                score_str = f"{final['away_score']}-{final['home_score']}"
                if pick["result"] == "pending":
                    outcome = grade_pick(pick, final)
                    if outcome:
                        pick["game"] = {
                            "status": outcome,
                            "label": live_labels[outcome],
                            "score": score_str,
                            "final": final["state"] == "post",
                        }
                else:
                    pick["game"] = {
                        "status": pick["result"],
                        "label": settled_labels[pick["result"]],
                        "score": score_str,
                        "final": True,
                    }
        result.append(pick)
    return result


def recent_picks_by_week(data, league=None, limit=4):
    """
    Every pick, grouped by (report.league, calendar week of report_date),
    for the most recent `limit` groups that have any picks in this league
    scope - truly-newest-first, picks within a group newest-first.
    Grouping is keyed by league as well as the week bucket - not the
    bucket alone - since two leagues' reports from the same calendar week
    must never land in the same group. The week itself is derived from
    report_date (see week_bucket_start) rather than each report's own
    hand-typed week_number, since two genuinely different weeks could
    otherwise collide on the same mistyped number. Groups are ordered by
    the latest report's created_at, so a same-week second card (e.g. a
    Friday slate added after a Thursday one) always sorts as most recent.
    The label spells out the league too whenever this view can span more
    than one (All Leagues, All Football/Futbol) so a date range is never
    ambiguous on screen; a single-league view keeps the plain label, and
    always reflects the most-recently-created report's own week_label
    (e.g. "Friday Card") rather than whichever report was seen first, or
    the week's date range if no report in it has one.

    Each pick carries a `game` status badge (see attach_game_status) so a
    settled pick still shows the final score for context, not just the
    graded result.
    """
    reports = {r["id"]: r for r in data["reports"]}
    multi_league = league is None or isinstance(league, (set, frozenset))
    by_week = {}
    for p in data["picks"]:
        r = reports.get(p["report_id"])
        if not r or not _league_matches(r["league"], league):
            continue
        wk = (r["league"], week_bucket_start(r["report_date"]))
        group = by_week.setdefault(wk, {"week_key": wk, "label": "", "latest_created_at": "", "picks": []})
        if r["created_at"] > group["latest_created_at"]:
            group["latest_created_at"] = r["created_at"]
            base_label = r["week_label"] or f"Week of {week_bucket_label(wk[1])}"
            group["label"] = f"{base_label} — {LEAGUES[r['league']]}" if multi_league else base_label
        merged = dict(p)
        merged.update(source=r["source"], league=r["league"], report_date=r["report_date"])
        group["picks"].append(merged)

    weeks = sorted(by_week.values(), key=lambda g: g["latest_created_at"], reverse=True)[:limit]
    for group in weeks:
        group["picks"].sort(key=lambda p: p["id"], reverse=True)
        group["picks"] = attach_game_status(group["picks"])
    return weeks


def empty_stats(source=None):
    return {
        "source": source,
        "wins": 0,
        "losses": 0,
        "pushes": 0,
        "settled": 0,
        "pending": 0,
        "staked": 0.0,
        "profit": 0.0,
        "units": 0.0,
        "roi": None,
        "win_pct": None,
    }


def _finalize(stats):
    stats["settled"] = stats["wins"] + stats["losses"] + stats["pushes"]
    decided = stats["wins"] + stats["losses"]  # pushes don't count toward win%
    stats["win_pct"] = (stats["wins"] / decided * 100) if decided else None
    stats["units"] = stats["profit"] / UNIT_SIZE
    stats["roi"] = (stats["profit"] / stats["staked"] * 100) if stats["staked"] else None
    return stats


def _apply_result(stats, pick):
    """Fold one settled pick's result into a running stats dict."""
    if pick["result"] == "win":
        stats["wins"] += 1
    elif pick["result"] == "loss":
        stats["losses"] += 1
    elif pick["result"] == "push":
        stats["pushes"] += 1
    stats["staked"] += pick["stake"]
    stats["profit"] += pick["profit_loss"]


def source_stats(data, source, league=None):
    reports = {r["id"]: r for r in data["reports"]}
    stats = empty_stats(source)
    for p in data["picks"]:
        r = reports.get(p["report_id"])
        if not r or r["source"] != source:
            continue
        if not _league_matches(r["league"], league):
            continue
        if p["result"] == "pending":
            stats["pending"] += 1
        elif p["result"] in ("win", "loss", "push"):
            _apply_result(stats, p)
    return _finalize(stats)


def category_breakdown(data, league=None):
    """{category: {source: stats}} for every bettable category x source."""
    reports = {r["id"]: r for r in data["reports"]}
    result = {cat: {s: empty_stats(s) for s in SOURCES} for cat in CATEGORY_ORDER}
    for p in data["picks"]:
        r = reports.get(p["report_id"])
        if not r or not _league_matches(r["league"], league):
            continue
        cat, src = p["category"], r["source"]
        if cat not in result or src not in SOURCES:
            continue
        bucket = result[cat][src]
        if p["result"] == "pending":
            bucket["pending"] += 1
        elif p["result"] in ("win", "loss", "push"):
            _apply_result(bucket, p)

    for cat in result:
        for src in SOURCES:
            _finalize(result[cat][src])
    return result


def category_pick_counts(data, league=None):
    """Bar-chart series: how many picks each source has made per category."""
    reports = {r["id"]: r for r in data["reports"]}
    counts = {cat: {s: 0 for s in SOURCES} for cat in CATEGORY_ORDER}
    for p in data["picks"]:
        r = reports.get(p["report_id"])
        if not r or not _league_matches(r["league"], league):
            continue
        if p["category"] in counts and r["source"] in SOURCES:
            counts[p["category"]][r["source"]] += 1

    cat_labels = [CATEGORIES[c]["label"] for c in CATEGORY_ORDER]
    series = [
        {
            "name": s,
            "slug": s.lower(),
            "color": SOURCE_STYLE[s]["color"],
            "values": [counts[c][s] for c in CATEGORY_ORDER],
        }
        for s in SOURCES
    ]
    return cat_labels, series


def cumulative_profit_chart(data, league=None):
    """Line-chart series: running real-dollar profit/loss, per source, over time."""
    reports = {r["id"]: r for r in data["reports"]}
    rows = []
    for p in data["picks"]:
        if p["result"] not in ("win", "loss", "push"):
            continue
        r = reports.get(p["report_id"])
        if not r or not _league_matches(r["league"], league):
            continue
        rows.append((r["report_date"], p["id"], r["source"], p["profit_loss"]))
    rows.sort(key=lambda row: (row[0], row[1]))

    all_dates = sorted({row[0] for row in rows})
    if not all_dates:
        return [], []

    running = {s: 0.0 for s in SOURCES}
    by_source_date = {s: {} for s in SOURCES}
    for report_date, _pick_id, src, profit_loss in rows:
        if src not in SOURCES:
            continue
        running[src] += profit_loss
        by_source_date[src][report_date] = running[src]

    series = []
    for s in SOURCES:
        values = []
        last = None
        started = False
        for d in all_dates:
            if d in by_source_date[s]:
                last = by_source_date[s][d]
                started = True
            values.append(last if started else None)
        series.append({"name": s, "slug": s.lower(), "color": SOURCE_STYLE[s]["color"], "values": values})

    return all_dates, series


def weekly_stats(data, league=None):
    """
    Per-week, per-source stats - the core "is one system trending better"
    view. Weeks are keyed by (report.league, calendar week of
    report_date) - not the week bucket alone - since two leagues'
    reports from the same calendar week must never merge into one. The
    week itself comes from report_date (see week_bucket_start), not each
    report's own hand-typed week_number, since two genuinely different
    weeks could otherwise collide on the same mistyped number. The label
    spells out the league too whenever this view can span more than one
    (All Leagues, All Football/Futbol) so a date range is never ambiguous
    on screen; a single-league view keeps the plain label.

    Returns (week_keys sorted, {week_key: label}, {(week_key, source): stats})
    where week_key is (league, week bucket start date).
    """
    reports = {r["id"]: r for r in data["reports"]}
    multi_league = league is None or isinstance(league, (set, frozenset))
    week_labels = {}
    week_label_created_at = {}
    result = {}
    for p in data["picks"]:
        if p["result"] not in ("win", "loss", "push"):
            continue
        r = reports.get(p["report_id"])
        if not r or not _league_matches(r["league"], league):
            continue

        wk = (r["league"], week_bucket_start(r["report_date"]))
        # Use the most-recently-created report's label for this week, not
        # the first one seen - otherwise a week stays branded with its
        # earliest card's name (e.g. "Thursday Card") even after a later
        # card (e.g. "Friday Card") for the same week is added.
        if wk not in week_label_created_at or r["created_at"] > week_label_created_at[wk]:
            week_label_created_at[wk] = r["created_at"]
            base_label = r["week_label"] or f"Week of {week_bucket_label(wk[1])}"
            week_labels[wk] = f"{base_label} — {LEAGUES[r['league']]}" if multi_league else base_label
        key = (wk, r["source"])
        if key not in result:
            result[key] = empty_stats(r["source"])
        _apply_result(result[key], p)

    for bucket in result.values():
        _finalize(bucket)

    week_keys = sorted(week_labels.keys(), key=lambda wk: (wk[1], wk[0]))
    return week_keys, week_labels, result


def weekly_win_pct_chart(week_numbers, week_labels, data):
    """Line-chart series: win% per source, one point per week."""
    categories = [week_labels[wn] for wn in week_numbers]
    series = []
    for s in SOURCES:
        values = [data.get((wn, s), {}).get("win_pct") for wn in week_numbers]
        series.append({"name": s, "slug": s.lower(), "color": SOURCE_STYLE[s]["color"], "values": values})
    return categories, series


def war_room_locks(data, league=None):
    """
    Consensus picks: still pending (upcoming, not graded yet) and picked
    by at least CONSENSUS_MIN_SOURCES sources on the same game, market,
    and side - doesn't require a unanimous sweep, since two of three
    landing on the same side is itself a real signal. Grouped by
    espn_event_id rather than the free-text matchup, since that's the
    only reliable way to tell "same game" across sources that word their
    matchup text differently - so only picks pulled from the DraftKings-
    odds widget (the ones carrying that id) are eligible at all. The bet
    line itself isn't part of the match, since it can move slightly
    between when each source wrote its report; agreeing on the same team
    on the same side of the same market is what "consensus" means here.
    """
    reports = {r["id"]: r for r in data["reports"]}
    groups = {}
    for p in data["picks"]:
        if p["result"] != "pending" or not p.get("espn_event_id") or not p.get("bet_type"):
            continue
        r = reports.get(p["report_id"])
        if not r or not _league_matches(r["league"], league):
            continue

        key = (p["espn_event_id"], p["bet_type"], p["bet_side"])
        by_source = groups.setdefault(key, {})
        current = by_source.get(r["source"])
        if current is None or p["id"] > current["pick"]["id"]:
            by_source[r["source"]] = {"pick": p, "report": r}

    locks = []
    for by_source in groups.values():
        if len(by_source) < CONSENSUS_MIN_SOURCES:
            continue
        sample = next(iter(by_source.values()))
        locks.append(
            {
                "matchup": sample["pick"]["matchup"],
                "league": sample["report"]["league"],
                "category": sample["pick"]["category"],
                "by_source": by_source,
                "sources": [s for s in SOURCES if s in by_source],
                "unanimous": len(by_source) == len(SOURCES),
            }
        )

    locks.sort(key=lambda lock: (-len(lock["by_source"]), lock["matchup"]))
    return locks


def confidence_locks(data, league=None):
    """
    Every still-pending pick at Lock-tier WR Confidence Score
    (>= LOCK_CONFIDENCE) - not the same thing as war_room_locks() above
    (which requires several sources to independently agree on the same
    game); this is any one pick's own highest-conviction score, standing
    alone. Uses each pick's *effective* score (see wr_confidence_effective
    / annotate_wr_confidence) - callers must annotate `data["picks"]`
    before calling this. Applies across every league (WR Confidence Score
    isn't soccer-specific). Sorted by score, highest first.
    """
    reports = {r["id"]: r for r in data["reports"]}
    locks = []
    for p in data["picks"]:
        if p["result"] != "pending":
            continue
        score = p.get("wr_confidence_effective")
        if score is None or score < LOCK_CONFIDENCE:
            continue
        r = reports.get(p["report_id"])
        if not r or not _league_matches(r["league"], league):
            continue
        locks.append(
            {
                "report_id": r["id"],
                "source": r["source"],
                "league": r["league"],
                "category": p["category"],
                "matchup": p["matchup"],
                "selection": p["selection"],
                "odds": p["odds"],
                "wr_confidence": score,
                "wr_confidence_breakdown": p.get("wr_confidence_breakdown"),
            }
        )
    locks.sort(key=lambda lock: lock["wr_confidence"], reverse=True)
    return locks


def _clamp(value, low, high):
    return max(low, min(high, value))


def source_track_record(data):
    """
    {source: source_stats(data, source)} across every league - an AI
    source's skill is a property of the model, not the sport, so its
    track record modifier (see wr_confidence_effective) is computed
    holistically rather than split out per league.
    """
    return {s: source_stats(data, s, None) for s in SOURCES}


def pick_agreement_map(data):
    """
    (espn_event_id, bet_type) -> {bet_side: {source, ...}} across every
    still-pending, ESPN-linked pick - the same grouping war_room_locks()
    builds, exposed separately so wr_confidence_effective() can tell, for
    any one pick, which other sources are on its side (agreement) and
    which are on the other side of the same market (conflict).
    """
    reports = {r["id"]: r for r in data["reports"]}
    agreement = {}
    for p in data["picks"]:
        if p["result"] != "pending" or not p.get("espn_event_id") or not p.get("bet_type"):
            continue
        r = reports.get(p["report_id"])
        if not r:
            continue
        key = (p["espn_event_id"], p["bet_type"])
        agreement.setdefault(key, {}).setdefault(p.get("bet_side"), set()).add(r["source"])
    return agreement


def wr_confidence_effective(pick, source, track_record, agreement_map):
    """
    A pick's WR Confidence Score adjusted for what its own entered number
    can't know at write time: how well this source has actually done
    (source_track_record) and whether other sources agree or conflict on
    the same game/market (pick_agreement_map). Both are scaled by
    wr_impact_scale(base) first - a pick that wasn't much of a conviction
    play to begin with (WR_IMPACT_FLOOR or below) gets no adjustment at
    all, and the swing ramps up toward full size as the base score
    approaches 100, so a thin pick can't get yanked around as hard as an
    elite one. Returns (score, breakdown) - breakdown is a plain dict of
    the pieces that summed to it, for a tooltip; score is None
    (breakdown {}) if the pick has no base score to begin with, since
    there's nothing to adjust.
    """
    base = pick.get("wr_confidence")
    if base is None:
        return None, {}

    impact = wr_impact_scale(base)

    record = (track_record or {}).get(source) or {}
    record_mod = 0.0
    if record.get("settled", 0) >= WR_RECORD_MIN_SETTLED and record.get("win_pct") is not None:
        record_mod = _clamp((record["win_pct"] - 50.0) * WR_RECORD_SCALE, -WR_RECORD_CAP, WR_RECORD_CAP) * impact

    agreeing, opposing = [], []
    key = (pick.get("espn_event_id"), pick.get("bet_type"))
    if key[0] and key[1]:
        sides = (agreement_map or {}).get(key, {})
        own_side = pick.get("bet_side")
        agreeing = sorted((sides.get(own_side) or set()) - {source})
        for side, sources_on_side in sides.items():
            if side != own_side:
                opposing.extend(sources_on_side)
        opposing = sorted(set(opposing))

    agree_mod = min(WR_AGREEMENT_BONUS * len(agreeing), WR_AGREEMENT_CAP) * impact if agreeing else 0.0
    conflict_mod = -min(WR_CONFLICT_PENALTY * len(opposing), WR_CONFLICT_CAP) * impact if opposing else 0.0

    score = _clamp(base + record_mod + agree_mod + conflict_mod, 0, 100)
    breakdown = {
        "base": base,
        "impact_scale": impact,
        "record_mod": record_mod,
        "record_win_pct": record.get("win_pct"),
        "agree_mod": agree_mod,
        "agreeing_sources": agreeing,
        "conflict_mod": conflict_mod,
        "opposing_sources": opposing,
    }
    return score, breakdown


def wr_confidence_breakdown_text(source, breakdown):
    """Plain-English tooltip for a wr_confidence_effective() breakdown."""
    if not breakdown:
        return ""
    parts = [f"Base {breakdown['base']:.0f}"]
    if breakdown["base"] <= WR_IMPACT_FLOOR:
        parts.append(f"no adjustment (at or below {WR_IMPACT_FLOOR})")
        return " · ".join(parts)
    if breakdown["record_mod"]:
        parts.append(f"{source} track record ({breakdown['record_win_pct']:.0f}% win rate) {breakdown['record_mod']:+.0f}")
    if breakdown["agree_mod"]:
        parts.append(f"agrees with {', '.join(breakdown['agreeing_sources'])} {breakdown['agree_mod']:+.0f}")
    if breakdown["conflict_mod"]:
        parts.append(f"conflicts with {', '.join(breakdown['opposing_sources'])} {breakdown['conflict_mod']:.0f}")
    return " · ".join(parts)


def annotate_wr_confidence(data):
    """
    Attaches wr_confidence_effective (float or None) and
    wr_confidence_breakdown_text (str) to every pick in data["picks"], in
    place. Cheap and network-free (everything it needs is already in
    `data`), so safe to call on every page that shows a WR Confidence
    badge. Returns `data` for convenient chaining.
    """
    reports = {r["id"]: r for r in data["reports"]}
    track_record = source_track_record(data)
    agreement_map = pick_agreement_map(data)
    for p in data["picks"]:
        r = reports.get(p["report_id"])
        source = r["source"] if r else None
        score, breakdown = wr_confidence_effective(p, source, track_record, agreement_map)
        p["wr_confidence_effective"] = score
        p["wr_confidence_breakdown"] = wr_confidence_breakdown_text(source, breakdown)
    return data


def rank_sources(stats):
    """Sources ordered by profit, best first."""
    return sorted(SOURCES, key=lambda s: stats[s]["profit"], reverse=True)


def clv_by_source(data, league=None):
    """
    {source: {avg, count}} average closing-line value (see line_move())
    across every spread/total pick that has a captured pregame snapshot -
    win/loss doesn't matter here, CLV is about the price, not the
    outcome. A source with no snapshotted picks yet gets avg=None,
    count=0 rather than being left out, so a template can always index
    every source.
    """
    reports = {r["id"]: r for r in data["reports"]}
    totals = {s: {"sum": 0.0, "count": 0} for s in SOURCES}
    for p in data["picks"]:
        r = reports.get(p["report_id"])
        if not r or r["source"] not in SOURCES or not _league_matches(r["league"], league):
            continue
        move = line_move(p)
        if move is None:
            continue
        totals[r["source"]]["sum"] += move
        totals[r["source"]]["count"] += 1
    return {
        s: {"avg": (t["sum"] / t["count"]) if t["count"] else None, "count": t["count"]}
        for s, t in totals.items()
    }


def rank_movement(data, league=None):
    """
    {source: delta} where delta is +1 if that source's rank improved
    (moved up a spot) as of the very last settled pick to come in, -1 if
    it dropped a spot, 0 otherwise. Compares the standings right before
    that last result against the standings after it - "recently moved"
    meaning "the last result changed the order", not any fixed time
    window like "since yesterday".
    """
    reports = {r["id"]: r for r in data["reports"]}
    rows = []
    for p in data["picks"]:
        if p["result"] not in ("win", "loss", "push"):
            continue
        r = reports.get(p["report_id"])
        if not r or not _league_matches(r["league"], league):
            continue
        rows.append((r["report_date"], p["id"], r["source"], p["profit_loss"]))
    rows.sort(key=lambda row: (row[0], row[1]))

    if not rows:
        return {s: 0 for s in SOURCES}

    def ranks_at(upto):
        profit = {s: 0.0 for s in SOURCES}
        for _date, _id, src, pl in rows[:upto]:
            if src in SOURCES:
                profit[src] += pl
        order = sorted(SOURCES, key=lambda s: profit[s], reverse=True)
        return {s: i for i, s in enumerate(order)}

    before = ranks_at(len(rows) - 1)
    after = ranks_at(len(rows))
    return {s: before[s] - after[s] for s in SOURCES}  # positive = moved up


def resolve_league(value):
    return value if value in LEAGUE_ORDER else None


def resolve_league_filter(value):
    """
    For views that can show one league, a named group of leagues (see
    LEAGUE_GROUPS), or everything: returns (display_key, filter_value).

    display_key is the raw value to carry through to templates/URLs for
    tab-active-state - always a plain string or None, never a set, so
    `current_league == code` comparisons in templates keep working
    unchanged. filter_value is what report-matching code tests a
    report's league against - None (no filter), a single league code, or
    a frozenset of codes - see _league_matches().
    """
    if value in LEAGUE_ORDER:
        return value, value
    if value in LEAGUE_GROUPS:
        return value, LEAGUE_GROUPS[value][1]
    return None, None


def _league_matches(actual, league_filter):
    if league_filter is None:
        return True
    if isinstance(league_filter, (set, frozenset)):
        return actual in league_filter
    return actual == league_filter


def week_bucket_start(report_date):
    """
    The Tuesday ('YYYY-MM-DD') that starts the Tuesday-through-Monday
    week containing `report_date` - what "week" actually means for
    grouping reports (see weekly_stats/recent_picks_by_week), instead of
    each report's own hand-typed week_number. Two reports from genuinely
    different calendar weeks can both get typed in as "week 3" by
    mistake; two reports whose report_date falls in the same Tue-Mon
    span can't collide with anything else, since the bucket is derived
    from the date itself.
    """
    d = date.fromisoformat(report_date)
    days_since_tuesday = (d.weekday() - 1) % 7  # Monday=0 ... Sunday=6; Tuesday=1
    return (d - timedelta(days=days_since_tuesday)).isoformat()


def week_bucket_label(bucket_start):
    """'Sep 1–7' (or 'Sep 29 – Oct 5' across a month boundary) for a Tuesday-start date from week_bucket_start()."""
    start = date.fromisoformat(bucket_start)
    end = start + timedelta(days=6)
    if start.month == end.month:
        return f"{start.strftime('%b')} {start.day}–{end.day}"
    return f"{start.strftime('%b')} {start.day} – {end.strftime('%b')} {end.day}"


def report_week_label(report):
    """
    A single report's own display label: its explicit week_label if set,
    else the date range of the calendar week its report_date falls in -
    never the raw hand-typed week_number, which two genuinely different
    weeks could share by mistake (see week_bucket_start).
    """
    return report.get("week_label") or f"Week of {week_bucket_label(week_bucket_start(report['report_date']))}"


app.jinja_env.globals["report_week_label"] = report_week_label


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------
@app.route("/")
def dashboard():
    data = store.load_data()
    annotate_wr_confidence(data)
    current_league, league = resolve_league_filter(request.args.get("league"))

    locks = war_room_locks(data, league)
    confidence_lock_picks = confidence_locks(data, league)

    stats = {s: source_stats(data, s, league) for s in SOURCES}
    ranked = rank_sources(stats)
    movement = rank_movement(data, league)
    clv = clv_by_source(data, league)

    chart_dates, chart_series = cumulative_profit_chart(data, league)
    profit_chart = charts.line_chart(chart_dates, chart_series, unit="$") if chart_dates else None

    week_numbers, week_labels, weekly_data = weekly_stats(data, league)
    if week_numbers:
        win_pct_labels, win_pct_series = weekly_win_pct_chart(week_numbers, week_labels, weekly_data)
        win_pct_chart = charts.line_chart(win_pct_labels, win_pct_series, unit="%", y_min=0, y_max=100)
    else:
        win_pct_chart = None

    count_labels, count_series = category_pick_counts(data, league)
    counts_chart = charts.grouped_bar_chart(count_labels, count_series) if any(
        v for s in count_series for v in s["values"]
    ) else None

    breakdown = category_breakdown(data, league)

    recent_weeks = recent_picks_by_week(data, league)

    report_count = sum(1 for r in data["reports"] if _league_matches(r["league"], league))

    return render_template(
        "index.html",
        locks=locks,
        confidence_lock_picks=confidence_lock_picks,
        stats=stats,
        ranked=ranked,
        movement=movement,
        clv=clv,
        profit_chart=profit_chart,
        win_pct_chart=win_pct_chart,
        week_numbers=week_numbers,
        week_labels=week_labels,
        weekly_data=weekly_data,
        counts_chart=counts_chart,
        breakdown=breakdown,
        recent_weeks=recent_weeks,
        report_count=report_count,
        current_league=current_league,
    )


@app.route("/reports")
def reports_list():
    data = store.load_data()
    current_league, league = resolve_league_filter(request.args.get("league"))

    reports = [r for r in data["reports"] if _league_matches(r["league"], league)]
    reports.sort(key=lambda r: (r["report_date"], r["id"]), reverse=True)

    report_stats = {}
    for r in reports:
        picks = [p for p in data["picks"] if p["report_id"] == r["id"]]
        s = {"wins": 0, "losses": 0, "pushes": 0, "pending": 0, "profit": 0.0}
        for p in picks:
            if p["result"] == "win":
                s["wins"] += 1
            elif p["result"] == "loss":
                s["losses"] += 1
            elif p["result"] == "push":
                s["pushes"] += 1
            elif p["result"] == "pending":
                s["pending"] += 1
            s["profit"] += p["profit_loss"]
        s["pick_count"] = len(picks)
        report_stats[r["id"]] = s

    auto_gradable = sum(1 for p in data["picks"] if p["result"] == "pending" and p.get("espn_event_id"))

    return render_template(
        "reports_list.html",
        reports=reports,
        report_stats=report_stats,
        current_league=current_league,
        auto_gradable=auto_gradable,
        graded=request.args.get("graded", type=int),
        still_pending=request.args.get("still_pending", type=int),
    )


def _latest_report(data, league):
    """(report, picks) for the most recent report in `league` (by report_date, then id), or (None, [])."""
    candidates = [r for r in data["reports"] if r["league"] == league]
    if not candidates:
        return None, []
    report = max(candidates, key=lambda r: (r["report_date"], r["id"]))
    picks = sorted((p for p in data["picks"] if p["report_id"] == report["id"]), key=lambda p: p["id"])
    picks = attach_game_status(picks, league=league)
    return report, picks


@app.route("/latest")
def latest_reports():
    data = store.load_data()
    annotate_wr_confidence(data)
    epl_report, epl_picks = _latest_report(data, "EPL")
    ucl_report, ucl_picks = _latest_report(data, "UCL")
    return render_template(
        "latest.html",
        epl_report=epl_report,
        epl_picks=epl_picks,
        ucl_report=ucl_report,
        ucl_picks=ucl_picks,
    )


@app.route("/disclaimer")
def disclaimer():
    return render_template("disclaimer.html")


def create_report(fields):
    """
    Shared by the HTML "Add Report" form and POST /api/reports: fields is
    a plain dict of already-typed values (see the two routes for how each
    builds it from its own request format). Raises ValueError on bad
    input - callers translate that into whatever error response fits
    their protocol (redirect vs. 400 JSON).
    """
    if fields.get("source") not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    if fields.get("league") not in LEAGUE_ORDER:
        raise ValueError(f"league must be one of {LEAGUE_ORDER}")
    if not fields.get("report_date"):
        raise ValueError("report_date is required")

    new_report = {
        "source": fields["source"],
        "league": fields["league"],
        "report_date": fields["report_date"],
        "week_number": int(fields.get("week_number") or 1),
        "week_label": (fields.get("week_label") or "").strip(),
        "philosophy": (fields.get("philosophy") or "").strip(),
        "blind_spot_notes": (fields.get("blind_spot_notes") or "").strip(),
        "lsu_review_notes": (fields.get("lsu_review_notes") or "").strip(),
        "created_at": datetime.utcnow().isoformat(timespec="seconds"),
    }

    def _mutate(data):
        new_report["id"] = data["next_report_id"]
        data["next_report_id"] += 1
        data["reports"].append(new_report)
        return new_report["id"]

    return store.mutate(
        _mutate,
        message=f"Add {new_report['source']} {new_report['league']} report ({new_report['report_date']})",
    )


@app.route("/reports/<int:report_id>")
def report_detail(report_id):
    data = store.load_data()
    annotate_wr_confidence(data)
    report = next((r for r in data["reports"] if r["id"] == report_id), None)
    if report is None:
        return redirect(url_for("reports_list"))

    picks = sorted((p for p in data["picks"] if p["report_id"] == report_id), key=lambda p: p["id"])
    auto_gradable = sum(1 for p in picks if p["result"] == "pending" and p.get("espn_event_id"))
    picks = attach_game_status(picks, league=report["league"])

    return render_template(
        "report_detail.html",
        report=report,
        picks=picks,
        auto_gradable=auto_gradable,
        graded=request.args.get("graded", type=int),
        still_pending=request.args.get("still_pending", type=int),
    )


@app.route("/reports/<int:report_id>/delete", methods=["POST"])
def delete_report(report_id):
    def _mutate(data):
        data["reports"] = [r for r in data["reports"] if r["id"] != report_id]
        data["picks"] = [p for p in data["picks"] if p["report_id"] != report_id]

    store.mutate(_mutate, message=f"Delete report #{report_id}")
    return redirect(url_for("reports_list"))


@app.route("/api/reports/<int:report_id>", methods=["PATCH"])
def api_update_report(report_id):
    """
    Correct a report's own metadata after the fact - week_number,
    week_label, philosophy, or the two notes fields - without touching
    its picks. Identity fields (source/league/report_date) aren't
    editable here; if one of those is wrong, delete and recreate the
    report instead. Only the fields present in the JSON body are
    changed.
    """
    editable = {"week_number", "week_label", "philosophy", "blind_spot_notes", "lsu_review_notes"}
    body = request.get_json(silent=True) or {}
    updates = {k: v for k, v in body.items() if k in editable}
    if not updates:
        return jsonify({"error": f"no editable fields given (allowed: {sorted(editable)})"}), 400

    data, token = store.load_for_update()
    report = next((r for r in data["reports"] if r["id"] == report_id), None)
    if report is None:
        return jsonify({"error": f"no report #{report_id}"}), 404

    if "week_number" in updates:
        try:
            report["week_number"] = int(updates["week_number"])
        except (TypeError, ValueError):
            return jsonify({"error": "week_number must be an integer"}), 400
    for key in ("week_label", "philosophy", "blind_spot_notes", "lsu_review_notes"):
        if key in updates:
            report[key] = str(updates[key]).strip()

    store.save(data, token, message=f"Update report #{report_id}")
    return jsonify({"id": report_id})


def create_pick(report_id, fields):
    """
    Shared by the HTML "Add a Pick" form and POST /api/reports/<id>/picks
    - see create_report() above for the same split. `war_room_line`/
    `edge`/`price_discipline` are shown on the report, never touched by
    grading. `war_room_line` is the model's own independent number for
    this market (e.g. "Miami -23 (range -20.5 to -26)" or, for a 3-way
    soccer market, a probability breakdown); `edge` is the gap between
    that and the posted line (e.g. "Stanford +1.5"); `price_discipline`
    is free text, one stake tier per line (e.g. "+25.5: $100\n+24: $25-
    50\n+23.5 or worse: pass").

    `wr_confidence` is the War Room's own holistic conviction rating
    (0-100), optional and purely informational - not used in grading or
    payout math. Today it's a judgment call made when the pick is logged
    (weighing the edge, the injury/roster notes, the price discipline,
    everything on the card), not a formula; more structured inputs will
    fold into it later. For EPL/UCL it's the same number the prediction
    model itself reports - one score per pick, not two.
    """
    if fields.get("category") not in CATEGORIES:
        raise ValueError(f"category must be one of {CATEGORY_ORDER}")
    if not fields.get("matchup") or not fields.get("selection"):
        raise ValueError("matchup and selection are required")

    bet_line = fields.get("bet_line")
    wr_confidence = fields.get("wr_confidence")
    if wr_confidence not in (None, "") and not (0 <= float(wr_confidence) <= 100):
        raise ValueError("wr_confidence must be between 0 and 100")

    new_pick = {
        "report_id": report_id,
        "category": fields["category"],
        "matchup": str(fields["matchup"]).strip(),
        "selection": str(fields["selection"]).strip(),
        "odds": int(fields["odds"]),
        "stake": float(fields["stake"]),
        "result": "pending",
        "profit_loss": 0.0,
        "notes": (fields.get("notes") or "").strip(),
        "wr_confidence": float(wr_confidence) if wr_confidence not in (None, "") else None,
        "war_room_line": (fields.get("war_room_line") or "").strip() or None,
        "edge": (fields.get("edge") or "").strip() or None,
        "price_discipline": (fields.get("price_discipline") or "").strip() or None,
        "espn_event_id": fields.get("espn_event_id") or None,
        "bet_type": fields.get("bet_type") or None,
        "bet_side": fields.get("bet_side") or None,
        "bet_line": float(bet_line) if bet_line not in (None, "") else None,
        "home_team": fields.get("home_team") or None,
        "away_team": fields.get("away_team") or None,
        "created_at": datetime.utcnow().isoformat(timespec="seconds"),
    }

    def _mutate(data):
        new_pick["id"] = data["next_pick_id"]
        data["next_pick_id"] += 1
        data["picks"].append(new_pick)
        return new_pick["id"]

    return store.mutate(_mutate, message=f"Add pick: {new_pick['matchup']} -- {new_pick['selection']}")


@app.route("/api/reports/<int:report_id>/picks/<int:pick_id>", methods=["PATCH"])
def api_update_pick(pick_id, report_id):
    """
    Correct a pick's own analysis fields after the fact - notes,
    wr_confidence, war_room_line, edge, price_discipline - without
    touching its bet/grading fields (odds, stake, bet_type/side/line,
    espn_event_id). If one of those is wrong, delete and re-add the
    pick instead - they're load-bearing for auto-grade and payout math,
    so this endpoint deliberately can't touch them.
    """
    editable = {"notes", "wr_confidence", "war_room_line", "edge", "price_discipline"}
    body = request.get_json(silent=True) or {}
    updates = {k: v for k, v in body.items() if k in editable}
    if not updates:
        return jsonify({"error": f"no editable fields given (allowed: {sorted(editable)})"}), 400

    data, token = store.load_for_update()
    pick = next((p for p in data["picks"] if p["id"] == pick_id and p["report_id"] == report_id), None)
    if pick is None:
        return jsonify({"error": f"no pick #{pick_id} on report #{report_id}"}), 404

    if "wr_confidence" in updates:
        value = updates["wr_confidence"]
        if value in (None, ""):
            pick["wr_confidence"] = None
        else:
            try:
                value = float(value)
            except (TypeError, ValueError):
                return jsonify({"error": "wr_confidence must be a number"}), 400
            if not (0 <= value <= 100):
                return jsonify({"error": "wr_confidence must be between 0 and 100"}), 400
            pick["wr_confidence"] = value
    for key in ("notes", "war_room_line", "edge", "price_discipline"):
        if key in updates:
            value = str(updates[key]).strip()
            pick[key] = value or None

    store.save(data, token, message=f"Update pick #{pick_id}")
    return jsonify({"id": pick_id})


def _safe_capture_pregame_lines(data):
    """capture_pregame_lines(), never allowed to break a grading request."""
    try:
        return capture_pregame_lines(data)
    except Exception:
        return 0


@app.route("/reports/<int:report_id>/auto_grade", methods=["POST"])
def auto_grade_report(report_id):
    data, token = store.load_for_update()
    graded, still_pending = auto_grade_pending(data, report_id=report_id)
    synced = sync_wallet_entries(data)
    captured = _safe_capture_pregame_lines(data)
    if graded or synced or captured:
        message = f"Auto-grade report #{report_id}: {graded} pick(s) settled, {synced} wallet entr{'y' if synced == 1 else 'ies'} synced"
        if captured:
            message += f", {captured} line{'s' if captured != 1 else ''} captured"
        store.save(data, token, message=message)
    return redirect(
        url_for("report_detail", report_id=report_id, graded=graded, still_pending=still_pending)
    )


@app.route("/reports/auto_grade_all", methods=["POST"])
def auto_grade_all():
    data, token = store.load_for_update()
    graded, still_pending = auto_grade_pending(data)
    synced = sync_wallet_entries(data)
    captured = _safe_capture_pregame_lines(data)
    if graded or synced or captured:
        message = f"Auto-grade all reports: {graded} pick(s) settled, {synced} wallet entr{'y' if synced == 1 else 'ies'} synced"
        if captured:
            message += f", {captured} line{'s' if captured != 1 else ''} captured"
        store.save(data, token, message=message)
    return redirect(url_for("reports_list", graded=graded, still_pending=still_pending))


@app.route("/picks/<int:pick_id>/settle", methods=["POST"])
def settle_pick(pick_id):
    result_value = request.form.get("result", "pending")
    if result_value not in RESULTS:
        result_value = "pending"

    data, token = store.load_for_update()
    pick = next((p for p in data["picks"] if p["id"] == pick_id), None)
    if pick is None:
        return redirect(url_for("reports_list"))

    pick["result"] = result_value
    pick["profit_loss"] = profit_for_result(pick["stake"], pick["odds"], result_value)
    sync_wallet_entries(data)
    store.save(data, token, message=f"Settle pick #{pick_id}: {result_value}")
    return redirect(url_for("report_detail", report_id=pick["report_id"]))


@app.route("/api/games")
def api_games():
    """Games for a league/date, for the 'look up odds' widget on Add Pick."""
    league = resolve_league(request.args.get("league"))
    date_str = (request.args.get("date") or "").replace("-", "")
    if not league or not date_str:
        return jsonify([])
    return jsonify(odds.scoreboard(league, date_str))


@app.route("/api/week")
def api_week():
    """The league's actual current week/matchweek - see odds.season_week(). Auto-fills the Add Report form."""
    league = resolve_league(request.args.get("league"))
    date_str = (request.args.get("date") or "").replace("-", "")
    if not league:
        return jsonify({"error": "league is required"}), 400
    week = odds.season_week(league, date_str or None)
    if week is None:
        return jsonify({"error": "could not determine the current week for this league"}), 404
    return jsonify({"week": week})


@app.route("/api/odds")
def api_odds():
    """Current line for one game, keyed by ESPN event id."""
    league = resolve_league(request.args.get("league"))
    event_id = request.args.get("event_id", "")
    if not league or not event_id:
        return jsonify({"error": "league and event_id are required"}), 400
    line = odds.game_odds(league, event_id)
    if line is None:
        return jsonify({"error": f"no {odds.sportsbook_for(league) or SPORTSBOOK} line available for this game"}), 404
    return jsonify(line)


@app.route("/picks/<int:pick_id>/delete", methods=["POST"])
def delete_pick(pick_id):
    data, token = store.load_for_update()
    pick = next((p for p in data["picks"] if p["id"] == pick_id), None)
    if pick is None:
        return redirect(url_for("reports_list"))

    report_id = pick["report_id"]
    data["picks"] = [p for p in data["picks"] if p["id"] != pick_id]
    store.save(data, token, message=f"Delete pick #{pick_id}")
    return redirect(url_for("report_detail", report_id=report_id))


# ---------------------------------------------------------------------
# Team Intel — soccer prediction research (form, standings, squad,
# manager history, home/away trends, news, weather). Everything here is
# read-only lookups against odds.py; nothing is stored, so there's
# nothing to grade or settle.
# ---------------------------------------------------------------------
def _team_intel(league, team_id):
    table = odds.standings(league)
    standing = next((t for t in table if t["team_id"] == str(team_id)), None)
    club_name = standing["name"] if standing else None
    return {
        "team_id": team_id,
        "club_name": club_name,
        "standing": standing,
        "table_size": len(table),
        "form": odds.team_form(league, team_id),
        "split": odds.home_away_split(league, team_id),
        "roster": odds.team_roster(league, team_id),
        "espn_news": odds.team_news(league, team_id),
        "club_news": odds.local_news(club_name) if club_name else [],
    }


@app.route("/intel")
@app.route("/intel/<league>")
def intel_picker(league=None):
    league = resolve_league(league)
    if league not in SOCCER_LEAGUES:
        league = "EPL"
    return render_template("intel_picker.html", current_league=league, table=odds.standings(league))


@app.route("/intel/<league>/team/<team_id>")
def intel_team(league, team_id):
    league = resolve_league(league)
    if league not in SOCCER_LEAGUES:
        return redirect(url_for("intel_picker"))
    return render_template("intel_team.html", league=league, **_team_intel(league, team_id))


def _match_intel(league, event_id):
    """None if ESPN has no such event; otherwise everything the Matchup Intel page/API needs."""
    info = odds.match_info(league, event_id)
    if info is None:
        return None
    return {
        "league": league,
        "event_id": event_id,
        "info": info,
        "weather": odds.match_weather(info["city"], info["country"], info["kickoff"]),
        "h2h": odds.head_to_head(league, event_id),
        "home": _team_intel(league, info["home_id"]),
        "away": _team_intel(league, info["away_id"]),
    }


@app.route("/intel/<league>/match/<event_id>")
def intel_match(league, event_id):
    league = resolve_league(league)
    if league not in SOCCER_LEAGUES:
        return redirect(url_for("intel_picker"))

    intel = _match_intel(league, event_id)
    if intel is None:
        return redirect(url_for("intel_picker", league=league))

    return render_template("intel_match.html", **intel)


# ---------------------------------------------------------------------
# JSON API — everything above, machine-readable. Meant for the scheduled
# prediction-bot runs (see docs/prediction_bot_playbook.md): a fresh
# session with no local checkout can pull a full slate's research with
# plain HTTP calls against the live app instead of re-deriving it from
# ESPN/Open-Meteo/Google News itself.
# ---------------------------------------------------------------------
@app.route("/api/standings")
def api_standings():
    league = resolve_league(request.args.get("league"))
    if league not in SOCCER_LEAGUES:
        return jsonify({"error": f"league must be one of {sorted(SOCCER_LEAGUES)}"}), 400
    return jsonify(odds.standings(league))


@app.route("/api/team_intel")
def api_team_intel():
    league = resolve_league(request.args.get("league"))
    team_id = request.args.get("team_id", "")
    if league not in SOCCER_LEAGUES or not team_id:
        return jsonify({"error": f"league (one of {sorted(SOCCER_LEAGUES)}) and team_id are required"}), 400
    return jsonify(_team_intel(league, team_id))


@app.route("/api/match_intel")
def api_match_intel():
    league = resolve_league(request.args.get("league"))
    event_id = request.args.get("event_id", "")
    if league not in SOCCER_LEAGUES or not event_id:
        return jsonify({"error": f"league (one of {sorted(SOCCER_LEAGUES)}) and event_id are required"}), 400
    intel = _match_intel(league, event_id)
    if intel is None:
        return jsonify({"error": "no such event"}), 404
    return jsonify(intel)


# ---------------------------------------------------------------------
# JSON API — writes. Same create_report()/create_pick() the HTML forms
# use, so a bot-submitted report/pick shows up identically everywhere
# (dashboard, reports list, auto-grade) to one entered by hand.
# ---------------------------------------------------------------------
@app.route("/api/reports", methods=["POST"])
def api_create_report():
    body = request.get_json(silent=True) or {}
    try:
        report_id = create_report(body)
    except (ValueError, KeyError) as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"id": report_id}), 201


@app.route("/api/reports/<int:report_id>/picks", methods=["POST"])
def api_create_pick(report_id):
    data = store.load_data()
    if not any(r["id"] == report_id for r in data["reports"]):
        return jsonify({"error": f"no report #{report_id}"}), 404

    body = request.get_json(silent=True) or {}
    try:
        pick_id = create_pick(report_id, body)
    except (ValueError, KeyError, TypeError) as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"id": pick_id}), 201


# ---------------------------------------------------------------------
# Wallets - each person's own real-money bets, in their own separate,
# named collection. Always tied to a specific dashboard pick
# (fields["pick_id"]), but snapshotted into its own record rather than a
# live pointer: the odds/stake actually placed can differ from the
# report's own card number, and a bet's history has to survive even if
# the underlying pick is later deleted. Every wallet page shares the
# same logic and layout below - just scoped to its own entries, and
# reachable only by its own direct URL (never linked from the dashboard
# or from any other wallet).
WALLETS = {
    "mine": {
        "entries_key": "wallet_entries",
        "next_id_key": "next_wallet_id",
        "label": "My Wallet",
        "view_endpoint": "my_wallet",
        "add_endpoint": "add_wallet_entry",
        "delete_endpoint": "delete_wallet_entry",
        "settle_endpoint": "settle_wallet_entry",
    },
    "jesse": {
        "entries_key": "jesse_wallet_entries",
        "next_id_key": "next_jesse_wallet_id",
        "label": "Jesse's Wallet",
        "view_endpoint": "jesse_wallet",
        "add_endpoint": "add_wallet_entry_jesse",
        "delete_endpoint": "delete_wallet_entry_jesse",
        "settle_endpoint": "settle_wallet_entry_jesse",
    },
}


def create_wallet_entry(fields, wallet):
    """
    Two shapes, chosen by whether fields["pick_id"] is given:

    Linked (usual case) - tied to a specific dashboard pick, snapshotting
    its source/league/category/matchup/selection/wr_confidence at entry
    time, and settled automatically by sync_wallet_entries() once that
    pick grades.

    Custom (fields["pick_id"] empty/absent) - a bet on something none of
    the AI sources covered (a different game, a prop, an off-site play).
    Entered by hand in full (source/league/matchup/selection are free
    text, not looked up), starts pending with no WR score, and can only
    be settled by hand too - see settle_wallet_entry() - since there's no
    linked pick for auto-grading to ever catch up with.
    """
    pick_id = fields.get("pick_id")
    if pick_id not in (None, ""):
        pick_id = int(pick_id)
        data = store.load_data()
        annotate_wr_confidence(data)
        pick = next((p for p in data["picks"] if p["id"] == pick_id), None)
        if pick is None:
            raise ValueError(f"no pick #{pick_id}")
        report = next((r for r in data["reports"] if r["id"] == pick["report_id"]), None)
        if report is None:
            raise ValueError("pick has no report")

        odds = int(fields["odds"])
        stake = float(fields["stake"])
        new_entry = {
            "pick_id": pick["id"],
            "report_id": report["id"],
            "source": report["source"],
            "league": report["league"],
            "category": pick["category"],
            "matchup": pick["matchup"],
            "selection": pick["selection"],
            "odds": odds,
            "stake": stake,
            "result": pick["result"],
            "profit_loss": profit_for_result(stake, odds, pick["result"]) if pick["result"] != "pending" else 0.0,
            "notes": (fields.get("notes") or "").strip() or None,
            # A frozen snapshot of the pick's *effective* WR Confidence
            # Score (base score adjusted for source track record and
            # cross-source agreement/conflict - see wr_confidence_effective)
            # at the moment this bet was logged - deliberately never
            # touched again, even as the source's record or other sources'
            # picks keep moving after the fact. What mattered was the read
            # at bet time.
            "wr_confidence_at_bet": pick.get("wr_confidence_effective"),
            "created_at": datetime.utcnow().isoformat(timespec="seconds"),
        }
    else:
        source = (fields.get("source") or "").strip()
        league = (fields.get("league") or "").strip()
        matchup = (fields.get("matchup") or "").strip()
        selection = (fields.get("selection") or "").strip()
        if not source or not league or not matchup or not selection:
            raise ValueError("source, league, matchup, and selection are required for a custom bet")
        odds = int(fields["odds"])
        stake = float(fields["stake"])
        new_entry = {
            "pick_id": None,
            "report_id": None,
            "source": source,
            "league": league,
            "category": "custom",
            "matchup": matchup,
            "selection": selection,
            "odds": odds,
            "stake": stake,
            "result": "pending",
            "profit_loss": 0.0,
            "notes": (fields.get("notes") or "").strip() or None,
            "wr_confidence_at_bet": None,
            "created_at": datetime.utcnow().isoformat(timespec="seconds"),
        }

    entries_key, next_id_key = wallet["entries_key"], wallet["next_id_key"]

    def _mutate(data):
        new_entry["id"] = data[next_id_key]
        data[next_id_key] += 1
        data[entries_key].append(new_entry)
        return new_entry["id"]

    return store.mutate(
        _mutate,
        message=f"Log {wallet['label']} bet: {new_entry['matchup']} -- {new_entry['selection']} (${stake:.0f})",
    )


def sync_wallet_entries(data):
    """
    Copy a pending wallet entry's result from its linked pick once that
    pick is itself no longer pending, across every configured wallet -
    using the entry's OWN odds/stake for the payout math, not the
    pick's, since what was actually wagered can differ from the report's
    card number. Called alongside auto_grade_pending() so everything
    settles together. Returns how many entries synced in total.
    """
    picks_by_id = {p["id"]: p for p in data["picks"]}
    synced = 0
    for wallet in WALLETS.values():
        for entry in data[wallet["entries_key"]]:
            if entry["result"] != "pending":
                continue
            pick = picks_by_id.get(entry["pick_id"])
            if not pick or pick["result"] == "pending":
                continue
            entry["result"] = pick["result"]
            entry["profit_loss"] = profit_for_result(entry["stake"], entry["odds"], pick["result"])
            synced += 1
    return synced


def wallet_overall_stats(entries):
    stats = empty_stats()
    for e in entries:
        if e["result"] == "pending":
            stats["pending"] += 1
        elif e["result"] in ("win", "loss", "push"):
            _apply_result(stats, e)
    return _finalize(stats)


def wallet_stats_by_source(entries):
    """{source: stats} - every AI source this wallet has ever placed a real bet on."""
    result = {s: empty_stats(s) for s in SOURCES}
    for e in entries:
        stats = result.setdefault(e["source"], empty_stats(e["source"]))
        if e["result"] == "pending":
            stats["pending"] += 1
        elif e["result"] in ("win", "loss", "push"):
            _apply_result(stats, e)
    for stats in result.values():
        _finalize(stats)
    return result


def wr_confidence_buckets():
    """The 10 fixed 10-point WR Confidence Score buckets, low to high, as (key, label) pairs."""
    buckets = []
    for lo in range(0, 100, 10):
        hi = lo + 9 if lo < 90 else 100
        buckets.append((f"{lo}-{hi}", f"{lo}–{hi}%"))
    return buckets


def wallet_stats_by_wr_bucket(entries):
    """
    {bucket_key: stats} for every 10-point WR Confidence Score bucket
    that has at least one bet in this wallet - keyed off
    wr_confidence_at_bet (the frozen score at the moment the bet was
    logged), not a pick's possibly-since-revised live score. Answers
    "does trusting a higher WR score actually pay off" - only meaningful
    once there's enough real betting history to fill more than one or
    two buckets.
    """
    keys = [k for k, _ in wr_confidence_buckets()]
    result = {}
    for e in entries:
        score = e.get("wr_confidence_at_bet")
        if score is None:
            continue
        key = keys[min(int(score) // 10, 9)]
        stats = result.setdefault(key, empty_stats())
        if e["result"] == "pending":
            stats["pending"] += 1
        elif e["result"] in ("win", "loss", "push"):
            _apply_result(stats, e)
    for stats in result.values():
        _finalize(stats)
    return result


def wallet_cumulative_chart(entries):
    """Line-chart series: running real-dollar profit/loss, per source, over the dates this wallet's bets were placed."""
    rows = []
    for e in entries:
        if e["result"] not in ("win", "loss", "push"):
            continue
        rows.append((e["created_at"][:10], e["id"], e["source"], e["profit_loss"]))
    rows.sort(key=lambda row: (row[0], row[1]))

    all_dates = sorted({row[0] for row in rows})
    if not all_dates:
        return [], []

    sources = sorted({row[2] for row in rows} | set(SOURCES))
    running = {s: 0.0 for s in sources}
    by_source_date = {s: {} for s in sources}
    for placed_date, _entry_id, src, profit_loss in rows:
        running[src] += profit_loss
        by_source_date[src][placed_date] = running[src]

    series = []
    for s in sources:
        values = []
        last = None
        started = False
        for d in all_dates:
            if d in by_source_date[s]:
                last = by_source_date[s][d]
                started = True
            values.append(last if started else None)
        series.append(
            {"name": s, "slug": s.lower(), "color": SOURCE_STYLE.get(s, {}).get("color", "#8b94a7"), "values": values}
        )

    return all_dates, series


# Keyword nudges, not verdicts: a headline mentioning one of these terms
# gets flagged for a closer read, since it's the kind of language that
# can move a bet's outcome - it's not judged for whether it actually
# applies to this specific game. Whole-word matches only (word
# boundaries) - a bare "out" or "starter" flags far too much unrelated
# coverage ("breakout", "starting the season 2-0"), so every entry here
# is a multi-word phrase specific enough to rarely appear outside real
# injury/lineup news. Two lists, not one: which way a headline should
# nudge the live read (see _news_modifier_for_pick) depends on whether
# it's bad news (negative) or good news (positive) for whichever team
# it's about.
_NEWS_NEGATIVE_PATTERNS = tuple(
    re.compile(r"\b" + re.escape(phrase) + r"\b")
    for phrase in (
        "ruled out", "will not play", "won't play", "out for the season",
        "out indefinitely", "injury", "injured", "questionable", "doubtful",
        "suspended", "benched", "surgery", "torn acl", "torn achilles",
        "fired", "resigns", "arrested", "ejected", "placed on ir",
        "injured reserve",
    )
)
_NEWS_POSITIVE_PATTERNS = tuple(
    re.compile(r"\b" + re.escape(phrase) + r"\b")
    for phrase in (
        "cleared to play", "removed from the injury report", "will play",
        "expected to play", "activated from ir", "back from suspension",
        "returns from injury", "upgraded to probable", "practiced fully",
        "no longer questionable", "reinstated",
    )
)


def _headline_sentiment(headline):
    """'negative', 'positive', or None - checked positive-first, since a
    genuinely good-news headline about a player's return very often
    contains a generic negative word too (e.g. "returns from injury",
    "cleared to play after injury scare" both contain "injury"), while
    the positive phrases here are specific multi-word ones unlikely to
    show up by accident inside real bad news."""
    text = (headline or "").lower()
    if any(p.search(text) for p in _NEWS_POSITIVE_PATTERNS):
        return "positive"
    if any(p.search(text) for p in _NEWS_NEGATIVE_PATTERNS):
        return "negative"
    return None


def _backed_team_id(pick, info):
    """
    The ESPN team id this pick actually backs, or None for a bet with no
    single team to back (a total, a soccer draw, or one this app can't
    place a side on) - those get no news modifier, just the headlines.
    """
    if pick.get("bet_type") not in ("spread", "moneyline", "match_result"):
        return None
    side = pick.get("bet_side")
    if side == "home":
        return info["home_id"]
    if side == "away":
        return info["away_id"]
    return None


def _news_modifier_for_pick(pick, info, headlines):
    """
    Net WR Confidence nudge from this event's headlines, signed correctly
    for the team this pick actually backs: good news for our team or bad
    news for the opponent both help; bad news for our team or good news
    for the opponent both hurt. Scaled by wr_impact_scale(pick's base
    score) same as wr_confidence_effective() - a thin pick doesn't swing
    as hard on a headline as an elite one, and one at WR_IMPACT_FLOOR or
    below doesn't move at all. Returns (modifier, contributions) -
    contributions is [(headline, delta), ...] for a tooltip, limited to
    the headlines that actually moved the number.
    """
    backed_team_id = _backed_team_id(pick, info)
    if backed_team_id is None:
        return 0.0, []
    impact = wr_impact_scale(pick.get("wr_confidence"))
    if impact == 0:
        return 0.0, []
    total = 0.0
    contributions = []
    for h in headlines:
        if not h.get("sentiment"):
            continue
        for_us = h.get("team_id") == backed_team_id
        if h["sentiment"] == "negative":
            delta = (-WR_NEWS_NEGATIVE_PENALTY if for_us else WR_NEWS_POSITIVE_BONUS) * impact
        else:
            delta = (WR_NEWS_POSITIVE_BONUS if for_us else -WR_NEWS_NEGATIVE_PENALTY) * impact
        total += delta
        contributions.append((h["headline"], delta))
    return _clamp(total, -WR_NEWS_CAP, WR_NEWS_CAP), contributions


def wallet_news_alerts(entries, picks_by_id, limit_per_team=4):
    """
    Recent headlines for both teams in every still-pending bet in this
    wallet, one lookup per unique linked game (two bets on the same game
    share it), plus - for any entry whose bet backs one specific team - a
    live-adjusted WR Confidence read layered on top of wr_confidence_at_bet:
    good news for the backed team or bad news for the opponent nudges it
    up, the reverse nudges it down (see _news_modifier_for_pick). This is
    a separate, always-moving number shown alongside the frozen at-bet
    score, never a replacement for it - wr_confidence_at_bet itself is
    never touched, by design (see create_wallet_entry).

    Returns (watches, live_reads): watches is the headline list for
    display, one per matchup; live_reads is {entry_id: {"score",
    "modifier", "text"}} for every still-pending linked entry that has
    one.

    Two API batches, each fetched in parallel rather than one round-trip
    at a time: first match_info() for every unique game (to get team
    ids), then team_news() for every unique (league, team) pair across
    all of them - the second batch has to wait on the first (it needs the
    team ids), but within each batch every call fires at once.
    """
    event_matchups = {}
    entries_by_event = {}
    for e in entries:
        if e["result"] != "pending":
            continue
        pick = picks_by_id.get(e["pick_id"])
        if not pick or not pick.get("espn_event_id"):
            continue
        key = (e["league"], pick["espn_event_id"])
        event_matchups.setdefault(key, e["matchup"])
        entries_by_event.setdefault(key, []).append((e, pick))

    if not event_matchups:
        return [], {}

    match_infos = _parallel_map(lambda key: odds.match_info(key[0], key[1]), list(event_matchups.keys()))

    team_lookup = {}
    for (event_league, _event_id), info in match_infos.items():
        if not info:
            continue
        team_lookup[(event_league, info["home_id"])] = info["home_name"]
        team_lookup[(event_league, info["away_id"])] = info["away_name"]

    news_by_team = _parallel_map(
        lambda key: odds.team_news(key[0], key[1], limit=limit_per_team), list(team_lookup.keys())
    )

    watches = []
    live_reads = {}
    for event_key, matchup in event_matchups.items():
        event_league, _event_id = event_key
        info = match_infos.get(event_key)
        if not info:
            continue

        headlines = []
        for team_id, team_name in ((info["home_id"], info["home_name"]), (info["away_id"], info["away_name"])):
            for article in news_by_team.get((event_league, team_id)) or []:
                if not article.get("headline"):
                    continue
                sentiment = _headline_sentiment(article["headline"])
                headlines.append(
                    {
                        "team": team_name,
                        "team_id": team_id,
                        "headline": article["headline"],
                        "link": article.get("link"),
                        "published": article.get("published"),
                        "sentiment": sentiment,
                        "flagged": sentiment is not None,
                    }
                )
        if not headlines:
            continue
        headlines.sort(key=lambda h: h["published"] or "", reverse=True)
        headlines.sort(key=lambda h: h["flagged"], reverse=True)
        watches.append({"matchup": matchup, "league": event_league, "headlines": headlines[:8]})

        for e, pick in entries_by_event.get(event_key, []):
            if e.get("wr_confidence_at_bet") is None:
                continue
            modifier, contributions = _news_modifier_for_pick(pick, info, headlines)
            if not contributions:
                continue
            score = _clamp(e["wr_confidence_at_bet"] + modifier, 0, 100)
            detail = "; ".join(f"{h[:60]}{'…' if len(h) > 60 else ''} ({d:+.0f})" for h, d in contributions)
            live_reads[e["id"]] = {
                "score": score,
                "modifier": modifier,
                "text": f"At bet {e['wr_confidence_at_bet']:.0f} · News {modifier:+.0f} — {detail}",
            }

    return watches, live_reads


def _pending_picks_for_picker(data):
    """
    Every currently-pending dashboard pick, newest first - the options
    list for any wallet's "Log a Bet" picker. Not wallet-specific: which
    picks exist is shared across wallets, only which ones a given person
    bet on (and at what price/stake) is per-wallet.
    """
    reports_by_id = {r["id"]: r for r in data["reports"]}
    pending_picks = []
    for p in data["picks"]:
        if p["result"] != "pending":
            continue
        r = reports_by_id.get(p["report_id"])
        if not r:
            continue
        pending_picks.append(
            {
                "id": p["id"],
                "league": r["league"],
                "source": r["source"],
                "category": CATEGORIES[p["category"]]["label"],
                "matchup": p["matchup"],
                "selection": p["selection"],
                "odds": p["odds"],
                "wr_confidence": p.get("wr_confidence_effective"),
            }
        )
    pending_picks.sort(key=lambda p: p["id"], reverse=True)
    return pending_picks


def _render_wallet(wallet_key):
    wallet = WALLETS[wallet_key]
    data = store.load_data()
    annotate_wr_confidence(data)
    entries = sorted(data[wallet["entries_key"]], key=lambda e: e["id"], reverse=True)
    picks_by_id = {p["id"]: p for p in data["picks"]}

    overall = wallet_overall_stats(entries)
    by_source = wallet_stats_by_source(entries)
    wr_buckets = wr_confidence_buckets()
    wr_bucket_stats = wallet_stats_by_wr_bucket(entries)
    news_alerts, news_live_reads = wallet_news_alerts(entries, picks_by_id)
    chart_dates, chart_series = wallet_cumulative_chart(entries)
    profit_chart = charts.line_chart(chart_dates, chart_series, unit="$") if chart_dates else None

    # Live/final score badges, same as the dashboard's own pick rows -
    # each entry borrows its linked pick's espn_event_id/bet_type/etc.,
    # tagged with the entry's own snapshotted league since a pick dict
    # alone doesn't carry one.
    status_inputs = []
    for e in entries:
        pick = picks_by_id.get(e["pick_id"])
        if pick:
            status_inputs.append({**pick, "wallet_entry_id": e["id"], "league": e["league"]})
    game_by_entry_id = {p["wallet_entry_id"]: p["game"] for p in attach_game_status(status_inputs)}
    entries = [
        {**e, "game": game_by_entry_id.get(e["id"]), "live_read": news_live_reads.get(e["id"])} for e in entries
    ]

    return render_template(
        "wallet.html",
        wallet_label=wallet["label"],
        add_endpoint=wallet["add_endpoint"],
        delete_endpoint=wallet["delete_endpoint"],
        settle_endpoint=wallet["settle_endpoint"],
        entries=entries,
        overall=overall,
        by_source=by_source,
        wr_buckets=wr_buckets,
        wr_bucket_stats=wr_bucket_stats,
        news_alerts=news_alerts,
        profit_chart=profit_chart,
        pending_picks=_pending_picks_for_picker(data),
    )


def _add_wallet_entry_form(wallet_key):
    wallet = WALLETS[wallet_key]
    create_wallet_entry(
        {
            "pick_id": request.form.get("pick_id"),
            "source": request.form.get("source", ""),
            "league": request.form.get("league_custom", ""),
            "matchup": request.form.get("matchup", ""),
            "selection": request.form.get("selection", ""),
            "odds": request.form.get("odds"),
            "stake": request.form.get("stake"),
            "notes": request.form.get("notes", ""),
        },
        wallet,
    )
    return redirect(url_for(wallet["view_endpoint"]))


def _delete_wallet_entry(entry_id, wallet_key):
    wallet = WALLETS[wallet_key]
    entries_key = wallet["entries_key"]
    data, token = store.load_for_update()
    data[entries_key] = [e for e in data[entries_key] if e["id"] != entry_id]
    store.save(data, token, message=f"Delete {wallet['label']} entry #{entry_id}")
    return redirect(url_for(wallet["view_endpoint"]))


def _settle_wallet_entry(entry_id, wallet_key):
    """
    Manually set a custom (not linked to a dashboard pick) entry's
    result - the only way one ever settles, since sync_wallet_entries()
    has no pick to grade it from. A no-op on a linked entry: that one
    settles automatically, and hand-editing it here would just drift
    from (or get overwritten by) the pick it's tied to.
    """
    wallet = WALLETS[wallet_key]
    result_value = request.form.get("result", "pending")
    if result_value not in RESULTS:
        result_value = "pending"

    data, token = store.load_for_update()
    entry = next((e for e in data[wallet["entries_key"]] if e["id"] == entry_id), None)
    if entry is None or entry.get("pick_id") is not None:
        return redirect(url_for(wallet["view_endpoint"]))

    entry["result"] = result_value
    entry["profit_loss"] = (
        profit_for_result(entry["stake"], entry["odds"], result_value) if result_value != "pending" else 0.0
    )
    store.save(data, token, message=f"Settle {wallet['label']} entry #{entry_id}: {result_value}")
    return redirect(url_for(wallet["view_endpoint"]))


def _api_create_wallet_entry(wallet_key):
    body = request.get_json(silent=True) or {}
    try:
        entry_id = create_wallet_entry(body, WALLETS[wallet_key])
    except (ValueError, KeyError, TypeError) as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"id": entry_id}), 201


def _api_update_wallet_entry(entry_id, wallet_key):
    """
    Correct a wallet entry's own odds/stake/notes after the fact. `result`
    is also editable, but only for a custom entry (no pick_id) - a linked
    entry's result stays derived from the pick via sync_wallet_entries()
    and shouldn't be hand-set here. If the entry is already settled,
    changing odds/stake recomputes profit_loss against that same stored
    result immediately.
    """
    wallet = WALLETS[wallet_key]
    editable = {"odds", "stake", "notes", "result"}
    body = request.get_json(silent=True) or {}
    updates = {k: v for k, v in body.items() if k in editable}
    if not updates:
        return jsonify({"error": f"no editable fields given (allowed: {sorted(editable)})"}), 400

    data, token = store.load_for_update()
    entry = next((e for e in data[wallet["entries_key"]] if e["id"] == entry_id), None)
    if entry is None:
        return jsonify({"error": f"no {wallet['label']} entry #{entry_id}"}), 404

    if "result" in updates:
        if entry.get("pick_id") is not None:
            return jsonify({"error": "result is derived automatically for bets linked to a dashboard pick"}), 400
        if updates["result"] not in RESULTS:
            return jsonify({"error": f"result must be one of {RESULTS}"}), 400
        entry["result"] = updates["result"]
    if "odds" in updates:
        try:
            entry["odds"] = int(updates["odds"])
        except (TypeError, ValueError):
            return jsonify({"error": "odds must be an integer"}), 400
    if "stake" in updates:
        try:
            entry["stake"] = float(updates["stake"])
        except (TypeError, ValueError):
            return jsonify({"error": "stake must be a number"}), 400
    if "notes" in updates:
        entry["notes"] = str(updates["notes"]).strip() or None
    entry["profit_loss"] = (
        profit_for_result(entry["stake"], entry["odds"], entry["result"]) if entry["result"] != "pending" else 0.0
    )

    store.save(data, token, message=f"Update {wallet['label']} entry #{entry_id}")
    return jsonify({"id": entry_id})


@app.route("/MyWallet")
def my_wallet():
    return _render_wallet("mine")


@app.route("/JesseWallet")
def jesse_wallet():
    return _render_wallet("jesse")


@app.route("/wallet/add", methods=["POST"])
def add_wallet_entry():
    return _add_wallet_entry_form("mine")


@app.route("/wallet/<int:entry_id>/delete", methods=["POST"])
def delete_wallet_entry(entry_id):
    return _delete_wallet_entry(entry_id, "mine")


@app.route("/wallet/<int:entry_id>/settle", methods=["POST"])
def settle_wallet_entry(entry_id):
    return _settle_wallet_entry(entry_id, "mine")


@app.route("/api/wallet", methods=["POST"])
def api_create_wallet_entry():
    return _api_create_wallet_entry("mine")


@app.route("/api/wallet/<int:entry_id>", methods=["PATCH"])
def api_update_wallet_entry(entry_id):
    return _api_update_wallet_entry(entry_id, "mine")


@app.route("/jesse-wallet/add", methods=["POST"])
def add_wallet_entry_jesse():
    return _add_wallet_entry_form("jesse")


@app.route("/jesse-wallet/<int:entry_id>/delete", methods=["POST"])
def delete_wallet_entry_jesse(entry_id):
    return _delete_wallet_entry(entry_id, "jesse")


@app.route("/jesse-wallet/<int:entry_id>/settle", methods=["POST"])
def settle_wallet_entry_jesse(entry_id):
    return _settle_wallet_entry(entry_id, "jesse")


@app.route("/api/jesse-wallet", methods=["POST"])
def api_create_wallet_entry_jesse():
    return _api_create_wallet_entry("jesse")


@app.route("/api/jesse-wallet/<int:entry_id>", methods=["PATCH"])
def api_update_wallet_entry_jesse(entry_id):
    return _api_update_wallet_entry(entry_id, "jesse")


if __name__ == "__main__":
    app.run(debug=True, port=5060)
