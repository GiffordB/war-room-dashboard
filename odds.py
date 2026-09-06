"""
Live sports data via ESPN's public site API - scores, odds, standings,
schedules, rosters, and news. No API key needed; unauthenticated, same
one that backs espn.com.

Covers two sport families:
  - American football (CFB, NFL): spread/total/moneyline priced by
    DraftKings, matching the original War Room report template.
  - Soccer (EPL, UCL): 3-way (home/draw/away) results, priced by ESPN
    BET (ESPN's soccer feed doesn't carry DraftKings lines - Bet 365 is
    there too, but only in fractional odds, so ESPN BET is the one that
    gives American-format numbers this app already knows how to grade).

It's undocumented and could change shape without notice, so every call
here is defensive: a bad/missing field just means an empty result, never
a crash.

Betting lookups (mirrors the original three-call shape):
  - scoreboard(league, date) -> games scheduled/played that day
  - game_odds(league, event_id) -> that game's spread/total/moneyline
    (+ draw, for soccer)
  - final_score(league, event_id) -> final score once the game is over,
    for auto-grading picks placed against a specific line

Prediction research, soccer only (see the Team Intel page):
  - standings(league) -> the league table
  - team_schedule(league, team_id) -> this season's fixtures/results
  - team_form(league, team_id) -> last N completed results, newest first
  - home_away_split(league, team_id) -> W/D/L and goals, home vs. away
  - team_roster(league, team_id) -> squad (with each player's ESPN
    injury/status flags where reported) + historical manager list

team_news(league, team_id) -> latest headlines from ESPN's own sports
  desk (form, transfers, injuries - whatever's in their feed), each with
  a link to the full article. Works for every configured league, not
  just soccer - used by the Team Intel page there, and by MyWallet's
  news watch for CFB/NFL.
  - local_news(club_name) -> beat-reporter/local-paper coverage of a
    club, via Google News (broader and more club-specific than ESPN's
    global desk - regional papers, the club's own site, etc.)
  - match_weather(city, country, kickoff) -> forecast conditions at
    kickoff (temperature, rain chance, wind), for reading how a total
    might play
  - head_to_head(league, event_id) -> ESPN's own recent-meetings summary
    for a specific matchup

Two non-ESPN sources back local_news() and match_weather() - both free,
unauthenticated, no key required:
  - Google News RSS (news.google.com/rss/search) for local_news(). Its
    feed is offered for personal, non-commercial use in a feed reader;
    this app uses it the same way (rendering headlines + links back to
    the original publisher, nothing republished or stored) for one
    operator's own betting research, not as a public news product.
  - Open-Meteo (open-meteo.com) for match_weather() - free for
    non-commercial use, no key. Geocoded at city level (the venue's
    exact lat/lon isn't in ESPN's data), so treat it as "weather in that
    city," not a stadium-precise reading, and it only covers kickoffs
    within Open-Meteo's ~16-day forecast window - anything further out
    returns None rather than guessing from climate averages.
"""

import json
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

TIMEOUT = 6

# family: the sports/{family} path segment ESPN's site API uses.
# path: the league slug within that family.
# provider: the odds provider this league's picks are priced against.
# extra_params: always-sent query params for this league's scoreboard.
LEAGUE_CONFIG = {
    "CFB": {"family": "football", "path": "college-football", "provider": "DraftKings", "extra_params": {"groups": "80"}},
    "NFL": {"family": "football", "path": "nfl", "provider": "DraftKings", "extra_params": {}},
    "EPL": {"family": "soccer", "path": "eng.1", "provider": "ESPN BET", "extra_params": {}},
    "UCL": {"family": "soccer", "path": "uefa.champions", "provider": "ESPN BET", "extra_params": {}},
}


def _get_json(url):
    # Deliberately no custom User-Agent: ESPN's edge (and some sandboxed
    # egress proxies) treat a spoofed/absent-looking UA as more suspicious
    # than Python's own honest default one.
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def _site_url(league, suffix):
    cfg = LEAGUE_CONFIG.get(league)
    if not cfg:
        return None
    return f"https://site.api.espn.com/apis/site/v2/sports/{cfg['family']}/{cfg['path']}/{suffix}"


def is_soccer(league):
    return (LEAGUE_CONFIG.get(league) or {}).get("family") == "soccer"


def sportsbook_for(league):
    return (LEAGUE_CONFIG.get(league) or {}).get("provider", "")


# ---------------------------------------------------------------------
# Betting lookups
# ---------------------------------------------------------------------
def scoreboard(league, date_str):
    """
    date_str: 'YYYYMMDD'. Returns a list of
    {id, matchup, home, away, kickoff, status}, home/away games first-listed
    away @ home to match how picks are usually written.
    """
    cfg = LEAGUE_CONFIG.get(league)
    if not cfg or not date_str:
        return []

    params = {"dates": date_str, **cfg["extra_params"]}
    url = f"{_site_url(league, 'scoreboard')}?{urllib.parse.urlencode(params)}"

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
                    "home_id": home["team"]["id"],
                    "away_id": away["team"]["id"],
                    "matchup": f"{away['team']['displayName']} @ {home['team']['displayName']}",
                    "kickoff": ev.get("date"),
                    "status": ev.get("status", {}).get("type", {}).get("description", ""),
                }
            )
        except (KeyError, StopIteration, IndexError, TypeError):
            continue
    return games


def season_week(league, date_str=None):
    """
    The league's current "week" - what a report's own week_number should
    be, so nobody has to guess or hand-count it (see the "week 0 vs. week
    1" CFB mislabeling this was added to fix).

    CFB/NFL: ESPN's scoreboard response carries this directly as
    `week.number` - authoritative, no guessing. `date_str` ('YYYYMMDD')
    scopes it to that day's slate; omit for "whatever week it is right
    now".

    EPL/UCL: ESPN's soccer scoreboard doesn't expose an equivalent field,
    so this falls back to the standings: the most common `played` count
    across the table, plus 1 (a team that's played 2 games is walking
    into matchweek 3). Uses the most common value rather than any single
    team's, since a postponed match can leave a team a game behind the
    rest of the table. `date_str` is ignored for soccer - there's no
    per-date lookup, just "the current matchweek".

    None if it can't be determined (bad/missing data, or an unrecognized
    league).
    """
    cfg = LEAGUE_CONFIG.get(league)
    if not cfg:
        return None

    if cfg["family"] == "football":
        params = dict(cfg["extra_params"])
        if date_str:
            params["dates"] = date_str
        url = _site_url(league, "scoreboard")
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        data = _get_json(url)
        if not data:
            return None
        return (data.get("week") or {}).get("number")

    if cfg["family"] == "soccer":
        played = [t["played"] for t in standings(league) if t.get("played") is not None]
        if not played:
            return None
        mode = max(set(played), key=played.count)
        return int(mode) + 1

    return None


def game_odds(league, event_id):
    """
    Current line for one event, or None if unavailable (game already
    started, book pulled the line, ESPN has no odds for it, etc).
    Spread/moneyline are always from the HOME team's perspective -
    negative spread means the home team is favored. `draw_moneyline` is
    only ever set for soccer leagues (a 3-way market); it's None for
    American football's 2-way moneyline.
    """
    cfg = LEAGUE_CONFIG.get(league)
    if not cfg or not event_id:
        return None

    url = f"{_site_url(league, 'summary')}?event={event_id}"
    data = _get_json(url)
    if not data:
        return None

    pickcenter = data.get("pickcenter") or data.get("odds") or []
    provider = next(
        (p for p in pickcenter if p.get("provider", {}).get("name") == cfg["provider"]),
        pickcenter[0] if pickcenter else None,
    )
    if not provider:
        return None

    home_odds = provider.get("homeTeamOdds") or {}
    away_odds = provider.get("awayTeamOdds") or {}
    draw_odds = provider.get("drawOdds") or {}

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
        "draw_moneyline": draw_odds.get("moneyLine"),
    }


def final_score(league, event_id):
    """
    {'completed': bool, 'state': 'pre'|'in'|'post', 'home_score': int,
    'away_score': int} for one event, or None if the game/event can't be
    found at all. `completed` is False for a game that's scheduled or in
    progress - callers grading a pick off this should only do so once
    it's True. `state` lets a caller show a *live* preview (score so far,
    winning/losing right now) for a game that's 'in' progress without
    treating that partial score as a final grade.
    """
    cfg = LEAGUE_CONFIG.get(league)
    if not cfg or not event_id:
        return None

    url = f"{_site_url(league, 'summary')}?event={event_id}"
    data = _get_json(url)
    if not data:
        return None

    try:
        comp = data["header"]["competitions"][0]
        status_type = comp["status"]["type"]
        competitors = comp["competitors"]
        home = next(c for c in competitors if c["homeAway"] == "home")
        away = next(c for c in competitors if c["homeAway"] == "away")
        return {
            "completed": bool(status_type["completed"]),
            "state": status_type.get("state", "pre"),
            "home_score": int(home["score"]),
            "away_score": int(away["score"]),
        }
    except (KeyError, StopIteration, IndexError, TypeError, ValueError):
        return None


def match_info(league, event_id):
    """
    Everything the Matchup Intel page needs about one event up front:
    {name, city, country, kickoff, home_id, home_name, away_id,
    away_name}, or None if the event can't be found. Combines what used
    to be two separate lookups (venue + team ids) into the one summary
    call both come from.
    """
    cfg = LEAGUE_CONFIG.get(league)
    if not cfg or not event_id:
        return None

    url = f"{_site_url(league, 'summary')}?event={event_id}"
    data = _get_json(url)
    if not data:
        return None

    try:
        venue = data["gameInfo"]["venue"]
        address = venue.get("address") or {}
        comp = data["header"]["competitions"][0]
        competitors = comp["competitors"]
        home = next(c for c in competitors if c["homeAway"] == "home")
        away = next(c for c in competitors if c["homeAway"] == "away")
        return {
            "name": venue.get("fullName"),
            "city": address.get("city"),
            "country": address.get("country"),
            "kickoff": comp.get("date"),
            "home_id": home["team"]["id"],
            "home_name": home["team"]["displayName"],
            "away_id": away["team"]["id"],
            "away_name": away["team"]["displayName"],
        }
    except (KeyError, StopIteration, IndexError, TypeError):
        return None


def head_to_head(league, event_id):
    """
    ESPN's own recent-meetings summary for a specific matchup:
    {summary, series_score, meetings: [{date, matchup, score}]} newest
    first, or None if ESPN has nothing for this pairing (a first-ever
    meeting, or an event ESPN can't find).
    """
    cfg = LEAGUE_CONFIG.get(league)
    if not cfg or not event_id:
        return None

    url = f"{_site_url(league, 'summary')}?event={event_id}"
    data = _get_json(url)
    if not data:
        return None

    series = next(
        (s for s in (data.get("seasonseries") or []) if s.get("type") == "head-to-head"),
        None,
    )
    if not series:
        return None

    meetings = []
    for ev in series.get("events", [])[:5]:
        try:
            competitors = ev["competitors"]
            home = next(c for c in competitors if c["homeAway"] == "home")
            away = next(c for c in competitors if c["homeAway"] == "away")
            meetings.append(
                {
                    "date": ev.get("date"),
                    "matchup": f"{away['team']['displayName']} @ {home['team']['displayName']}",
                    "score": f"{away.get('score')}-{home.get('score')}",
                }
            )
        except (KeyError, StopIteration, IndexError, TypeError):
            continue

    return {
        "summary": series.get("summary"),
        "series_score": series.get("seriesScore"),
        "meetings": meetings,
    }


# ---------------------------------------------------------------------
# Prediction research (soccer)
# ---------------------------------------------------------------------
def standings(league):
    """
    The current league table: [{team_id, name, abbrev, logo, rank,
    played, wins, draws, losses, gf, ga, gd, points}], sorted by rank.
    Soccer leagues only - CFB/NFL don't use this endpoint shape and
    return [].
    """
    if not is_soccer(league):
        return []
    cfg = LEAGUE_CONFIG[league]
    url = f"https://site.api.espn.com/apis/v2/sports/soccer/{cfg['path']}/standings"
    data = _get_json(url)
    if not data:
        return []

    try:
        entries = data["children"][0]["standings"]["entries"]
    except (KeyError, IndexError, TypeError):
        return []

    table = []
    for e in entries:
        stats = {s["name"]: s.get("value") for s in e.get("stats", []) if s.get("name")}
        team = e.get("team", {})
        table.append(
            {
                "team_id": team.get("id"),
                "name": team.get("displayName"),
                "abbrev": team.get("abbreviation"),
                "logo": (team.get("logos") or [{}])[0].get("href"),
                "rank": stats.get("rank"),
                "played": stats.get("gamesPlayed"),
                "wins": stats.get("wins"),
                "draws": stats.get("ties"),
                "losses": stats.get("losses"),
                "gf": stats.get("pointsFor"),
                "ga": stats.get("pointsAgainst"),
                "gd": stats.get("pointDifferential"),
                "points": stats.get("points"),
            }
        )
    table.sort(key=lambda t: (t["rank"] is None, t["rank"]))
    return table


def team_schedule(league, team_id):
    """
    This (current) season's fixtures/results for one team, oldest first:
    [{event_id, date, opponent, opponent_id, home_away, completed,
    team_score, opp_score, result}] - result is 'W'/'D'/'L' once
    completed, else None ('D' never happens for CFB/NFL in practice, but
    the field stays generic). Works for every configured league - the
    endpoint shape (competitions[0].competitors, each carrying a
    {'value': ...} score) is the same for CFB/NFL as for soccer.
    """
    if not team_id or league not in LEAGUE_CONFIG:
        return []

    url = f"{_site_url(league, f'teams/{team_id}/schedule')}"
    data = _get_json(url)
    if not data:
        return []

    def _score(competitor):
        # ESPN's own shape for this endpoint: a plain number/string most
        # of the time, but a {'value': ..., '$ref': ...} scoring-source
        # reference for some events - handle both.
        raw = competitor.get("score")
        if isinstance(raw, dict):
            raw = raw.get("value")
        if raw in (None, ""):
            return None
        return int(float(raw))

    games = []
    for ev in data.get("events", []):
        try:
            comp = ev["competitions"][0]
            competitors = comp["competitors"]
            me = next(c for c in competitors if c["team"]["id"] == str(team_id))
            opp = next(c for c in competitors if c["team"]["id"] != str(team_id))
            completed = bool(comp.get("status", {}).get("type", {}).get("completed"))
            team_score = _score(me) if completed else None
            opp_score = _score(opp) if completed else None
            result = None
            if team_score is not None and opp_score is not None:
                result = "W" if team_score > opp_score else "L" if team_score < opp_score else "D"
            games.append(
                {
                    "event_id": ev["id"],
                    "date": ev.get("date"),
                    "opponent": opp["team"]["displayName"],
                    "opponent_id": opp["team"]["id"],
                    "home_away": me.get("homeAway"),
                    "completed": completed,
                    "team_score": team_score,
                    "opp_score": opp_score,
                    "result": result,
                }
            )
        except (KeyError, StopIteration, IndexError, TypeError, ValueError):
            continue

    games.sort(key=lambda g: g["date"] or "")
    return games


def team_form(league, team_id, last=5):
    """Last `last` completed results for a team, most recent first."""
    played = [g for g in team_schedule(league, team_id) if g["completed"]]
    return list(reversed(played[-last:]))


def home_away_split(league, team_id):
    """{'home': {w,d,l,gf,ga}, 'away': {w,d,l,gf,ga}} from this season's completed games."""
    split = {"home": {"w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0}, "away": {"w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0}}
    for g in team_schedule(league, team_id):
        if not g["completed"]:
            continue
        side = "home" if g["home_away"] == "home" else "away"
        bucket = split[side]
        bucket["gf"] += g["team_score"] or 0
        bucket["ga"] += g["opp_score"] or 0
        if g["result"] == "W":
            bucket["w"] += 1
        elif g["result"] == "D":
            bucket["d"] += 1
        elif g["result"] == "L":
            bucket["l"] += 1
    return split


def team_roster(league, team_id):
    """
    {'players': [{name, position, age, jersey, nationality, status,
    injury}], 'coaches': [names]}. `status`/`injury` reflect whatever
    ESPN's feed has flagged for that player right now (often nothing,
    even for a genuinely knocked-up squad - ESPN's own injury reporting
    is thin for both soccer and CFB; team_news() and the club/program's
    own site are the more reliable check). `coaches` is ESPN's historical
    list for the team, not flagged with who's currently in the post -
    cross-check against news/standings before treating the top name as
    the incumbent.

    Works for every configured league, but the raw shape differs: a
    soccer roster is one flat list under "athletes"; a CFB/NFL roster
    groups athletes into position units (offense/defense/special teams),
    each with its own "items" list - both are flattened into the same
    `players` shape here.
    """
    if not team_id or league not in LEAGUE_CONFIG:
        return {"players": [], "coaches": []}

    url = _site_url(league, f"teams/{team_id}/roster")
    data = _get_json(url)
    if not data:
        return {"players": [], "coaches": []}

    raw_athletes = data.get("athletes", [])
    if raw_athletes and "items" in raw_athletes[0]:
        # CFB/NFL shape: [{position: "offense", items: [athlete, ...]}, ...]
        flat_athletes = [a for group in raw_athletes for a in group.get("items", [])]
    else:
        flat_athletes = raw_athletes

    players = []
    for a in flat_athletes:
        injuries = a.get("injuries") or []
        nationality = a.get("citizenship") or a.get("birthCountry")
        if isinstance(nationality, dict):
            nationality = nationality.get("abbreviation") or nationality.get("alternateId")
        players.append(
            {
                "name": a.get("fullName"),
                "position": (a.get("position") or {}).get("abbreviation"),
                "age": a.get("age"),
                "jersey": a.get("jersey"),
                "nationality": nationality,
                "status": (a.get("status") or {}).get("name"),
                "injury": (injuries[0].get("status") if injuries else None),
            }
        )

    coaches = [f"{c.get('firstName', '')} {c.get('lastName', '')}".strip() for c in (data.get("coach") or [])]
    return {"players": players, "coaches": coaches}


def team_news(league, team_id, limit=6):
    """
    Latest headlines for a team: [{headline, published, link}], newest
    first. Not soccer-specific despite living in the "soccer only" block
    above - ESPN's news endpoint is the same shape for every configured
    league (see wallet_news_alerts() in app.py, which uses this for
    CFB/NFL too), it just wasn't called for anything but the Team Intel
    page (soccer-only) until now.
    """
    if not team_id or league not in LEAGUE_CONFIG:
        return []

    url = f"{_site_url(league, 'news')}?team={team_id}"
    data = _get_json(url)
    if not data:
        return []

    articles = []
    for a in (data.get("articles") or [])[:limit]:
        web_href = ((a.get("links") or {}).get("web") or {}).get("href")
        articles.append(
            {
                "headline": a.get("headline"),
                "published": a.get("published"),
                "link": web_href,
            }
        )
    return articles


# ---------------------------------------------------------------------
# Prediction research, non-ESPN sources
# ---------------------------------------------------------------------
def local_news(club_name, limit=6):
    """
    Beat-reporter/local-paper coverage of a club via Google News RSS -
    broader than ESPN's global soccer desk, since it picks up regional
    papers and the club's own site too. [{headline, source, published,
    link}], newest first; [] if the query fails or turns up nothing.
    """
    if not club_name:
        return []

    query = urllib.parse.quote(f'"{club_name}" football')
    url = f"https://news.google.com/rss/search?q={query}&hl=en-GB&gl=GB&ceid=GB:en"

    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
            root = ET.fromstring(resp.read())
    except Exception:
        return []

    articles = []
    for item in root.findall(".//item")[:limit]:
        source_el = item.find("source")
        articles.append(
            {
                "headline": item.findtext("title"),
                "source": source_el.text if source_el is not None else None,
                "published": item.findtext("pubDate"),
                "link": item.findtext("link"),
            }
        )
    return articles


def _geocode_city(city, country=None):
    if not city:
        return None
    url = f"https://geocoding-api.open-meteo.com/v1/search?{urllib.parse.urlencode({'name': city, 'count': 5})}"
    data = _get_json(url)
    if not data or not data.get("results"):
        return None
    results = data["results"]
    if country:
        match = next((r for r in results if (r.get("country") or "").lower() == country.lower()), None)
        if match:
            return match
    return results[0]


def match_weather(city, country, kickoff_iso):
    """
    Forecast conditions at kickoff: {city, temp_c, precip_probability_pct,
    precip_mm, wind_kmh, cloud_cover_pct, forecast_hour}, or None if the
    city can't be geocoded, the kickoff is outside Open-Meteo's forecast
    window (~16 days out), or the lookup fails. City-level geocoding, not
    stadium-precise - see the module docstring.
    """
    if not city or not kickoff_iso:
        return None

    place = _geocode_city(city, country)
    if not place:
        return None

    try:
        kickoff = datetime.fromisoformat(kickoff_iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None

    params = {
        "latitude": place["latitude"],
        "longitude": place["longitude"],
        "hourly": "temperature_2m,precipitation_probability,precipitation,wind_speed_10m,cloud_cover",
        "forecast_days": 16,
        "timezone": "UTC",
    }
    url = f"https://api.open-meteo.com/v1/forecast?{urllib.parse.urlencode(params)}"
    data = _get_json(url)
    if not data or "hourly" not in data:
        return None

    hourly = data["hourly"]
    target = kickoff.strftime("%Y-%m-%dT%H:00")
    times = hourly.get("time") or []
    if target not in times:
        return None
    idx = times.index(target)

    def _at(key):
        values = hourly.get(key) or []
        return values[idx] if idx < len(values) else None

    return {
        "city": place.get("name"),
        "temp_c": _at("temperature_2m"),
        "precip_probability_pct": _at("precipitation_probability"),
        "precip_mm": _at("precipitation"),
        "wind_kmh": _at("wind_speed_10m"),
        "cloud_cover_pct": _at("cloud_cover"),
        "forecast_hour": target,
    }
