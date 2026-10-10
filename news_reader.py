"""
Model-backed read of the news behind a pending bet.

The keyword flags in app.py (_headline_sentiment) only know that a
headline contains "injury" or "ruled out" - they can't tell whose injury
it is or whether it matters. This module hands a game's headlines (with
ESPN's one-paragraph description of each) to Claude once per game and
gets back, per headline, which team the news favors, how much, and why.
app.py turns that into the up/down arrows and the live WR nudge, signed
for the side each bet backs.

Opt-in by environment: with no ANTHROPIC_API_KEY set the reader reports
itself unavailable and app.py keeps the keyword read. Every failure here
(network, refusal, bad JSON) also returns None so a wallet page never
fails because of it.
"""
import json
import os

MODEL = "claude-opus-5-5"
LAST_ERROR = None  # repr of the most recent failure, for /api/ai_status

_SCHEMA = {
    "type": "object",
    "properties": {
        "reads": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "favors": {"type": "string", "enum": ["home", "away", "neither"]},
                    "severity": {"type": "integer", "description": "0-3"},
                    "reason": {"type": "string"},
                },
                "required": ["index", "favors", "severity", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["reads"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You read sports news for a bettor and judge, for each item, which team in one specific game it favors. "
    "Favor means the news makes that team more likely to win or cover: an injury or suspension to a team's "
    "player favors the OPPONENT; a player returning or being cleared favors their own team; a manager quote, "
    "transfer rumor, league roundup, or story about a different competition favors neither unless it names a "
    "concrete availability change for this game. Severity: 0 = irrelevant to this game; 1 = minor (a bench "
    "player, a doubt that is not confirmed, old news already priced in); 2 = a regular starter out or back, or a "
    "meaningful tactical/motivation change; 3 = a star or starting quarterback/goalkeeper/striker ruled out or "
    "returning, or a coaching change. Use only what the headline and description say; do not guess at facts "
    "they do not contain. Keep each reason to one short sentence that names the player or fact."
)


def available():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def read_game_news(league, home_name, away_name, kickoff, headlines):
    """
    headlines: [{headline, description, team}] in display order. Returns
    {index: {"favors": "home"|"away"|"neither", "severity": 0-3, "reason": str}}
    or None when the reader is unavailable or the call fails.
    """
    if not available() or not headlines:
        return None
    try:
        import anthropic
    except ImportError:
        return None

    items = "\n".join(
        f"[{i}] ({h.get('team') or 'unknown team'} feed) {h.get('headline') or ''}"
        + (f" -- {h['description']}" if h.get("description") else "")
        for i, h in enumerate(headlines)
    )
    prompt = (
        f"League: {league}. Game: {away_name} (away) at {home_name} (home). Kickoff: {kickoff or 'unknown'}.\n"
        f"Judge each news item below for THIS game only. Return one read per item, every index exactly once.\n\n{items}"
    )
    try:
        client = anthropic.Anthropic(max_retries=1, timeout=45.0)
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": _SCHEMA}},
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            return None
        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            return None
        reads = json.loads(text).get("reads") or []
    except Exception as e:
        global LAST_ERROR
        LAST_ERROR = repr(e)[:600]
        return None
    out = {}
    for r in reads:
        i = r.get("index")
        if isinstance(i, int) and 0 <= i < len(headlines) and i not in out:
            out[i] = {"favors": r.get("favors", "neither"), "severity": max(0, min(3, int(r.get("severity") or 0))), "reason": (r.get("reason") or "").strip()}
    return out if len(out) == len(headlines) else None
