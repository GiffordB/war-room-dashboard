"""
DraftKings odds lookup via ESPN's public scoreboard/summary endpoints.

ESPN's site API (the same one that backs espn.com) is unauthenticated -
no API key needed - and its per-event "pickcenter" data is priced by
DraftKings specifically, which lines up with this report template being
DraftKings-only. It's undocumented and could change shape without
notice, so every call here is defensive: a bad/missing field just means
an empty result, never a crash.

Three calls:
  - scoreboard(league, date) -> games scheduled/played that day
  - game_odds(league, event_id) -> that game's DraftKings spread/total/ML
  - final_score(league, event_id) -> final score once the game is over,
    for auto-grading picks that were placed against a specific line.
"""

import json
import urllib.parse
import urllib.request

SPORT_PATHS = {"CFB": "college-football", "NFL": "nfl"}
TIMEOUT = 6


def _get_json(url):
    # Deliberately no custom User-Agent: ESPN's edge (and some sandboxed
    # egress proxies) treat a spoofed/absent-looking UA as more suspicious
    # than Python's own honest default one.
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def scoreboard(league, date_str):
    """
    date_str: 'YYYYMMDD'. Returns a list of
    {id, matchup, home, away, kickoff, status}, home/away games first-listed
    away @ home to match how picks are usually written.
    """
    sport_path = SPORT_PATHS.get(league)
    if not sport_path or not date_str:
        return []

    params = {"dates": date_str}
    if league == "CFB":
        params["groups"] = "80"  # FBS - the level DraftKings actually prices
    url = f"https://site.api.espn.com/apis/site/v2/sports/football/{sport_path}/scoreboard?{urllib.parse.urlencode(params)}"

    data = _get_json(url)
    if not data:
        return []

    games = []
    for ev in data.get("events", []):
        try:
            comp = ev["competitions"][0]
            competitors = comp["competitors"]
            home = next(c for c in competitors if c["homeAway"] == "home")
            away = next(c for c in competitors if c["homeAway"] == "away")
            games.append(
                {
                    "id": ev["id"],
                    "home": home["team"]["displayName"],
                    "away": away["team"]["displayName"],
                    "matchup": f"{away['team']['displayName']} @ {home['team']['displayName']}",
                    "kickoff": ev.get("date"),
                    "status": ev.get("status", {}).get("type", {}).get("description", ""),
                }
            )
        except (KeyError, StopIteration, IndexError, TypeError):
            continue
    return games


def game_odds(league, event_id):
    """
    Current DraftKings line for one event, or None if unavailable (game
    already started, book pulled the line, ESPN has no odds for it, etc).
    Spread/moneyline are always from the HOME team's perspective - negative
    spread means the home team is favored.
    """
    sport_path = SPORT_PATHS.get(league)
    if not sport_path or not event_id:
        return None

    url = f"https://site.api.espn.com/apis/site/v2/sports/football/{sport_path}/summary?event={event_id}"
    data = _get_json(url)
    if not data:
        return None

    pickcenter = data.get("pickcenter") or []
    provider = next(
        (p for p in pickcenter if p.get("provider", {}).get("name") == "DraftKings"),
        pickcenter[0] if pickcenter else None,
    )
    if not provider:
        return None

    home_odds = provider.get("homeTeamOdds") or {}
    away_odds = provider.get("awayTeamOdds") or {}

    return {
        "provider": provider.get("provider", {}).get("name", "Unknown"),
        "spread_details": provider.get("details"),
        "home_spread": provider.get("spread"),
        "home_spread_odds": home_odds.get("spreadOdds"),
        "away_spread_odds": away_odds.get("spreadOdds"),
        "total": provider.get("overUnder"),
        "over_odds": provider.get("overOdds"),
        "under_odds": provider.get("underOdds"),
        "home_moneyline": home_odds.get("moneyLine"),
        "away_moneyline": away_odds.get("moneyLine"),
    }


def final_score(league, event_id):
    """
    {'completed': bool, 'home_score': int, 'away_score': int} for one
    event, or None if the game/event can't be found at all. `completed`
    is False for a game that's scheduled or in progress - callers should
    leave those picks pending rather than grading off a partial score.
    """
    sport_path = SPORT_PATHS.get(league)
    if not sport_path or not event_id:
        return None

    url = f"https://site.api.espn.com/apis/site/v2/sports/football/{sport_path}/summary?event={event_id}"
    data = _get_json(url)
    if not data:
        return None

    try:
        comp = data["header"]["competitions"][0]
        completed = bool(comp["status"]["type"]["completed"])
        competitors = comp["competitors"]
        home = next(c for c in competitors if c["homeAway"] == "home")
        away = next(c for c in competitors if c["homeAway"] == "away")
        return {
            "completed": completed,
            "home_score": int(home["score"]),
            "away_score": int(away["score"]),
        }
    except (KeyError, StopIteration, IndexError, TypeError, ValueError):
        return None
