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

from datetime import datetime

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

# Combined filters on top of the individual leagues above - "All
# Football"/"All Futbol" are two labels for the same thing (EPL+UCL
# together), so a viewer can pick whichever word they think in without
# hunting for two different tabs. {key: (tab label, {league codes})}.
LEAGUE_GROUPS = {
    "football": ("All Football", SOCCER_LEAGUES),
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
    """
    settled_labels = {"win": "Won", "loss": "Lost", "push": "Push"}
    live_labels = {"win": "Winning", "loss": "Losing", "push": "Push"}

    score_cache = {}
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
            if key not in score_cache:
                score_cache[key] = odds.final_score(pick_league, pick["espn_event_id"])
            final = score_cache[key]
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
    Every pick, grouped by (report.league, report.week_number), for the
    most recent `limit` groups that have any picks in this league scope -
    truly-newest-first, picks within a group newest-first. Grouping is
    keyed by league as well as week_number - not week_number alone -
    since week numbering resets per league (EPL/UCL matchweeks vs.
    CFB/NFL season weeks aren't the same sequence, so two different
    leagues' "Week 3" must never land in the same bucket). Groups are
    ordered by the latest report's created_at, not by the raw week
    number, so a same-week second card (e.g. a Friday slate added after
    a Thursday one) always sorts as most recent instead of getting
    silently outranked by an unrelated league's higher week number. The
    label spells out the league too whenever this view can span more
    than one (All Leagues, All Football/Futbol) so "Week 3" is never
    ambiguous on screen; a single-league view keeps the plain label, and
    always reflects the most-recently-created report's own week_label
    (e.g. "Friday Card") rather than whichever report was seen first.

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
        wk = (r["league"], r["week_number"])
        group = by_week.setdefault(wk, {"week_key": wk, "label": "", "latest_created_at": "", "picks": []})
        if r["created_at"] > group["latest_created_at"]:
            group["latest_created_at"] = r["created_at"]
            base_label = r["week_label"] or f"Week {r['week_number']}"
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
    view. Weeks are keyed by (report.league, report.week_number) - not
    week_number alone - since week numbering resets per league (EPL/UCL
    matchweeks vs. CFB/NFL season weeks aren't the same sequence: a
    league's own "Week 3" must never merge with another league's "Week
    3"). The label spells out the league too whenever this view can span
    more than one (All Leagues, All Football/Futbol) so "Week 3" is never
    ambiguous on screen; a single-league view keeps the plain label.

    Returns (week_keys sorted, {week_key: label}, {(week_key, source): stats})
    where week_key is (league, week_number).
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

        wk = (r["league"], r["week_number"])
        # Use the most-recently-created report's label for this week, not
        # the first one seen - otherwise a week stays branded with its
        # earliest card's name (e.g. "Thursday Card") even after a later
        # card (e.g. "Friday Card") for the same week is added.
        if wk not in week_label_created_at or r["created_at"] > week_label_created_at[wk]:
            week_label_created_at[wk] = r["created_at"]
            base_label = r["week_label"] or f"Week {r['week_number']}"
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
    by all three sources on the same game, market, and side. Grouped by
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
        if not all(s in by_source for s in SOURCES):
            continue
        sample = next(iter(by_source.values()))
        locks.append(
            {
                "matchup": sample["pick"]["matchup"],
                "league": sample["report"]["league"],
                "category": sample["pick"]["category"],
                "by_source": by_source,
            }
        )

    locks.sort(key=lambda lock: lock["matchup"])
    return locks


def confidence_locks(data, league=None):
    """
    Every still-pending pick at Lock-tier WR Confidence Score
    (>= LOCK_CONFIDENCE) - not the same thing as war_room_locks() above
    (which requires all three sources to independently agree on the same
    game); this is any one pick's own highest-conviction score, standing
    alone. Applies across every league (WR Confidence Score isn't
    soccer-specific). Sorted by score, highest first.
    """
    reports = {r["id"]: r for r in data["reports"]}
    locks = []
    for p in data["picks"]:
        if p["result"] != "pending":
            continue
        score = p.get("wr_confidence")
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
            }
        )
    locks.sort(key=lambda lock: lock["wr_confidence"], reverse=True)
    return locks


def rank_sources(stats):
    """Sources ordered by profit, best first."""
    return sorted(SOURCES, key=lambda s: stats[s]["profit"], reverse=True)


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


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------
@app.route("/")
def dashboard():
    data = store.load_data()
    current_league, league = resolve_league_filter(request.args.get("league"))

    locks = war_room_locks(data, league)
    confidence_lock_picks = confidence_locks(data, league)

    stats = {s: source_stats(data, s, league) for s in SOURCES}
    ranked = rank_sources(stats)
    movement = rank_movement(data, league)

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


@app.route("/reports/<int:report_id>/auto_grade", methods=["POST"])
def auto_grade_report(report_id):
    data, token = store.load_for_update()
    graded, still_pending = auto_grade_pending(data, report_id=report_id)
    if graded:
        store.save(data, token, message=f"Auto-grade report #{report_id}: {graded} pick(s) settled")
    return redirect(
        url_for("report_detail", report_id=report_id, graded=graded, still_pending=still_pending)
    )


@app.route("/reports/auto_grade_all", methods=["POST"])
def auto_grade_all():
    data, token = store.load_for_update()
    graded, still_pending = auto_grade_pending(data)
    if graded:
        store.save(data, token, message=f"Auto-grade all reports: {graded} pick(s) settled")
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


if __name__ == "__main__":
    app.run(debug=True, port=5060)
