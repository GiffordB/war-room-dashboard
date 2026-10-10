"""
Model-backed read of the argument behind a pick.

The WR rating is a checklist: it scores track record, agreement, line
value, form, injuries and weather, but it never reads the card. This
module hands one pick - its selection, price, stake, and the source's
own stated number, edge and price discipline, plus the report's
philosophy - to Claude and gets back a 0-10 quality grade for the
argument, a verdict (edge / mixed / story), and a one-line reason.

The grade is written onto the pick once (`ai_grade`) and never
re-run, so it is frozen at the moment the pick was read, like the
initial WR rating. app.py feeds it ONLY to the "WR AI Test" ticket,
which runs beside the main War Room card with its own record; the main
rating and the main card never see it until that test says it helps.

Opt-in by environment: with no ANTHROPIC_API_KEY set the grader reports
itself unavailable and nothing is graded. Every failure returns None so
no save ever depends on it.
"""
import json
import os

MODEL = "claude-opus-5-5"
LAST_ERROR = None  # repr of the most recent failure, for /api/ai_status

_SCHEMA = {
    "type": "object",
    "properties": {
        "quality": {"type": "integer", "minimum": 0, "maximum": 10},
        "verdict": {"type": "string", "enum": ["edge", "mixed", "story"]},
        "reason": {"type": "string"},
    },
    "required": ["quality", "verdict", "reason"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are a sharp, skeptical sports-betting reviewer. You are given one recommended bet and the author's own "
    "stated reasoning. Grade the ARGUMENT, not the team: is there a real, specific edge against the posted number, "
    "or is this a story? Reward: a concrete model number or fair price that differs from the market with the gap "
    "stated; sourced, dated availability facts; key-number awareness; acknowledging what the market already knows; "
    "honest price discipline (when to pass). Penalise: narrative (revenge, momentum, 'due'), public-money takes with "
    "no number, injuries assumed rather than sourced, stale lines, confidence that outruns the stated evidence, "
    "reasoning that contradicts its own conclusion (e.g. the author admits the position fails their own rule). "
    "quality: 0-2 = pure story or self-contradicting; 3-4 = weak, mostly narrative; 5 = average, some substance; "
    "6-7 = solid, specific, honest; 8-10 = rigorous with a clear quantified edge. verdict: edge (6+), mixed (4-5), "
    "story (0-3). Judge only what is written; do not add your own opinion of the matchup. One short sentence of reason."
)


def available():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _clip(text, n):
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + " [...]"


def grade_pick_argument(pick, report):
    """{"quality": 0-10, "verdict": str, "reason": str, "model": MODEL} or None when unavailable or the call fails."""
    if not available():
        return None
    try:
        import anthropic
    except ImportError:
        return None
    parts = [
        f"League: {report.get('league')}. Source: {report.get('source')}. Report: {report.get('week_label') or ''} ({report.get('report_date')}).",
        f"Bet: {pick.get('matchup')} -- {pick.get('selection')} at {pick.get('odds'):+d}, stake ${pick.get('stake', 0):.0f}, category {pick.get('category')}.",
    ]
    if pick.get("war_room_line"):
        parts.append(f"Author's own number / score: {_clip(pick['war_room_line'], 1500)}")
    if pick.get("edge"):
        parts.append(f"Author's stated edge: {_clip(pick['edge'], 1500)}")
    if pick.get("price_discipline"):
        parts.append(f"Author's price discipline: {_clip(pick['price_discipline'], 800)}")
    if pick.get("notes"):
        parts.append(f"Notes: {_clip(pick['notes'], 600)}")
    if report.get("philosophy"):
        parts.append(f"Report method (context only): {_clip(report['philosophy'], 2500)}")
    prompt = "\n\n".join(parts) + "\n\nGrade this argument."
    try:
        client = anthropic.Anthropic(max_retries=1, timeout=45.0)
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=1000,
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
        out = json.loads(text)
        return {"quality": int(out["quality"]), "verdict": out["verdict"], "reason": (out.get("reason") or "").strip(), "model": response.model}
    except Exception as e:
        global LAST_ERROR
        LAST_ERROR = repr(e)[:600]
        return None
