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

from datetime import date, datetime

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
# for College Football and for the NFL, even in the same week, since the
# slates (and the analysis behind them) don't overlap.
LEAGUES = {"CFB": "College Football", "NFL": "NFL"}
LEAGUE_ORDER = list(LEAGUES.keys())

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

app.jinja_env.globals.update(
    categories=CATEGORIES,
    category_order=CATEGORY_ORDER,
    sources=SOURCES,
    source_color=lambda s: SOURCE_STYLE.get(s, {}).get("color", "#8b94a7"),
    leagues=LEAGUES,
    league_order=LEAGUE_ORDER,
    results=RESULTS,
    result_label=lambda r: RESULT_LABELS.get(r, r),
    result_color=lambda r: RESULT_COLORS.get(r, "#8b94a7"),
    sportsbook=SPORTSBOOK,
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
    bet_type/bet_side/bet_line (set only for picks pulled via the
    DraftKings-odds widget). Spread/total lines use the standard "push on
    an exact tie" rule; moneyline pushes only on an actual tied score.
    """
    home, away = final["home_score"], final["away_score"]
    side, line = pick.get("bet_side"), pick.get("bet_line") or 0.0

    if pick.get("bet_type") == "moneyline":
        if home == away:
            return "push"
        home_won = home > away
        return "win" if (home_won if side == "home" else not home_won) else "loss"

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
    Every pick, grouped by its report's week_number, for the most recent
    `limit` weeks that have any picks in this league scope - newest week
    first, picks within a week newest-first. Each pick carries a `game`
    status badge (see attach_game_status) so a settled pick still shows
    the final score for context, not just the graded result.
    """
    reports = {r["id"]: r for r in data["reports"]}
    by_week = {}
    for p in data["picks"]:
        r = reports.get(p["report_id"])
        if not r or (league and r["league"] != league):
            continue
        wn = r["week_number"]
        group = by_week.setdefault(wn, {"week_number": wn, "label": f"Week {wn}", "picks": []})
        merged = dict(p)
        merged.update(source=r["source"], league=r["league"], report_date=r["report_date"])
        group["picks"].append(merged)

    weeks = sorted(by_week.values(), key=lambda g: g["week_number"], reverse=True)[:limit]
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
        if league and r["league"] != league:
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
        if not r or (league and r["league"] != league):
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
        if not r or (league and r["league"] != league):
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


def cumulative_units_chart(data, league=None):
    """Line-chart series: running units won/lost, per source, over time."""
    reports = {r["id"]: r for r in data["reports"]}
    rows = []
    for p in data["picks"]:
        if p["result"] not in ("win", "loss", "push"):
            continue
        r = reports.get(p["report_id"])
        if not r or (league and r["league"] != league):
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
            values.append((last / UNIT_SIZE) if started else None)
        series.append({"name": s, "slug": s.lower(), "color": SOURCE_STYLE[s]["color"], "values": values})

    return all_dates, series


def weekly_stats(data, league=None):
    """
    Per-week, per-source stats - the core "is one system trending better"
    view. Weeks are keyed by report.week_number (not the raw date), since
    that's what lines picks up across sources/leagues that report on
    slightly different calendar days for the "same" week.

    Returns (week_numbers sorted ascending, {week_number: label}, {(week_number, source): stats}).
    """
    reports = {r["id"]: r for r in data["reports"]}
    week_labels = {}
    result = {}
    for p in data["picks"]:
        if p["result"] not in ("win", "loss", "push"):
            continue
        r = reports.get(p["report_id"])
        if not r or (league and r["league"] != league):
            continue

        wn = r["week_number"]
        week_labels.setdefault(wn, r["week_label"] or f"Week {wn}")
        key = (wn, r["source"])
        if key not in result:
            result[key] = empty_stats(r["source"])
        _apply_result(result[key], p)

    for bucket in result.values():
        _finalize(bucket)

    week_numbers = sorted(week_labels.keys())
    return week_numbers, week_labels, result


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
        if not r or (league and r["league"] != league):
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
        if not r or (league and r["league"] != league):
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


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------
@app.route("/")
def dashboard():
    data = store.load_data()
    league = resolve_league(request.args.get("league"))

    locks = war_room_locks(data, league)

    stats = {s: source_stats(data, s, league) for s in SOURCES}
    ranked = rank_sources(stats)
    movement = rank_movement(data, league)

    chart_dates, chart_series = cumulative_units_chart(data, league)
    units_chart = charts.line_chart(chart_dates, chart_series, unit=" u") if chart_dates else None

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

    report_count = sum(1 for r in data["reports"] if not league or r["league"] == league)

    return render_template(
        "index.html",
        locks=locks,
        stats=stats,
        ranked=ranked,
        movement=movement,
        units_chart=units_chart,
        win_pct_chart=win_pct_chart,
        week_numbers=week_numbers,
        week_labels=week_labels,
        weekly_data=weekly_data,
        counts_chart=counts_chart,
        breakdown=breakdown,
        recent_weeks=recent_weeks,
        report_count=report_count,
        current_league=league,
    )


@app.route("/reports")
def reports_list():
    data = store.load_data()
    league = resolve_league(request.args.get("league"))

    reports = [r for r in data["reports"] if not league or r["league"] == league]
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
        current_league=league,
        auto_gradable=auto_gradable,
        graded=request.args.get("graded", type=int),
        still_pending=request.args.get("still_pending", type=int),
    )


@app.route("/reports/add", methods=["GET", "POST"])
def add_report():
    if request.method == "POST":
        new_report = {
            "source": request.form.get("source", SOURCES[0]),
            "league": request.form.get("league", LEAGUE_ORDER[0]),
            "report_date": request.form["report_date"],
            "week_number": int(request.form.get("week_number") or 1),
            "week_label": request.form.get("week_label", "").strip(),
            "blind_spot_notes": request.form.get("blind_spot_notes", "").strip(),
            "lsu_review_notes": request.form.get("lsu_review_notes", "").strip(),
            "created_at": datetime.utcnow().isoformat(timespec="seconds"),
        }

        def _mutate(data):
            new_report["id"] = data["next_report_id"]
            data["next_report_id"] += 1
            data["reports"].append(new_report)
            return new_report["id"]

        report_id = store.mutate(
            _mutate,
            message=f"Add {new_report['source']} {new_report['league']} report ({new_report['report_date']})",
        )
        return redirect(url_for("report_detail", report_id=report_id))

    return render_template("add_report.html", today=date.today().isoformat())


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


@app.route("/reports/<int:report_id>/picks/add", methods=["POST"])
def add_pick(report_id):
    american_odds = int(request.form["odds"])
    stake = float(request.form["stake"])
    bet_line = request.form.get("bet_line", "").strip()

    new_pick = {
        "report_id": report_id,
        "category": request.form.get("category", CATEGORY_ORDER[0]),
        "matchup": request.form["matchup"].strip(),
        "selection": request.form["selection"].strip(),
        "odds": american_odds,
        "stake": stake,
        "result": "pending",
        "profit_loss": 0.0,
        "notes": request.form.get("notes", "").strip(),
        "espn_event_id": request.form.get("espn_event_id") or None,
        "bet_type": request.form.get("bet_type") or None,
        "bet_side": request.form.get("bet_side") or None,
        "bet_line": float(bet_line) if bet_line else None,
        "home_team": request.form.get("home_team") or None,
        "away_team": request.form.get("away_team") or None,
        "created_at": datetime.utcnow().isoformat(timespec="seconds"),
    }

    def _mutate(data):
        new_pick["id"] = data["next_pick_id"]
        data["next_pick_id"] += 1
        data["picks"].append(new_pick)

    store.mutate(_mutate, message=f"Add pick: {new_pick['matchup']} -- {new_pick['selection']}")
    return redirect(url_for("report_detail", report_id=report_id))


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


@app.route("/api/odds")
def api_odds():
    """Current DraftKings line for one game, keyed by ESPN event id."""
    league = resolve_league(request.args.get("league"))
    event_id = request.args.get("event_id", "")
    if not league or not event_id:
        return jsonify({"error": "league and event_id are required"}), 400
    line = odds.game_odds(league, event_id)
    if line is None:
        return jsonify({"error": "no DraftKings line available for this game"}), 404
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


if __name__ == "__main__":
    app.run(debug=True, port=5060)
