"""
Persistence for the War Room dashboard, without a database.

Render's free web service has no persistent disk - anything written to
local disk vanishes on the next restart/redeploy. Rather than pay for a
database, this uses the same trick as this account's other dashboards
(e.g. BriggsTrading's real_holdings_store.py): the data lives as one JSON
file, committed straight into this repo via the GitHub Contents API. A
git commit is a perfectly good row store at this app's scale (a season
of reports is a few hundred picks, tens of KB of JSON), and it comes with
a free audit trail for nothing extra - every add/settle/delete is a
commit you can read in the repo's history.

Locally (no GITHUB_PAT set), the same JSON shape is just read from and
written to a file on disk instead - no token needed for local dev.

load_data() (every read-only GET route) is served from a short in-memory
cache rather than hitting the GitHub API on every single page view - see
_CACHE_TTL_SECONDS below. A burst of writes (several picks logged back to
back) was enough to trip GitHub's own rate limiting on this file and make
every page load crawl for minutes; the cache means most page views in a
normal browsing session never touch GitHub's API at all.
load_for_update() (every route that might write) always bypasses the
cache - a write has to see the true current content and sha, never a
cached one, or a save() could silently clobber a change made since the
cache was last filled. save() refreshes the cache immediately with
whatever it just wrote, so a redirect right after a POST/PATCH/DELETE
still shows the fresh state instead of waiting out the TTL.
"""

import base64
import json
import os
import time
from pathlib import Path

import requests

_API_BASE = "https://api.github.com"

# Kept short deliberately: long enough to absorb a burst of page views or
# writes within the same few seconds, short enough that nobody looking at
# two browser tabs a few seconds apart notices it's cached at all.
_CACHE_TTL_SECONDS = 5
_cache = {"text": None, "fetched_at": 0.0}


def _cache_is_fresh():
    return _cache["text"] is not None and (time.time() - _cache["fetched_at"]) < _CACHE_TTL_SECONDS


def _remember(data):
    """Refresh the cache with `data`, already-normalized, as of right now."""
    _cache["text"] = json.dumps(data)
    _cache["fetched_at"] = time.time()
DATA_PATH_IN_REPO = "data/war_room.json"
# Deliberately NOT data/war_room.json: that path is the tracked production
# file the live app commits to via the GitHub API. A local dev run (no
# GITHUB_PAT) must never share it - `git clean`/`rm -rf data` during local
# testing would otherwise delete or stage a deletion of real data.
LOCAL_PATH = Path(__file__).resolve().parent / ".local_dev_data" / "war_room.json"

GITHUB_PAT = os.environ.get("GITHUB_PAT", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "GiffordB/war-room-dashboard")

EMPTY_STATE = {
    "next_report_id": 1,
    "next_pick_id": 1,
    "next_wallet_id": 1,
    "next_jesse_wallet_id": 1,
    "reports": [],
    "picks": [],
    "wallet_entries": [],
    "jesse_wallet_entries": [],
}


def _normalize(data):
    """Backfill keys added after the original data file was created, so
    every caller can just do data["wallet_entries"] etc. unconditionally.
    Each missing list gets its own fresh [] - never the same list object
    EMPTY_STATE holds, which every caller would otherwise share and could
    mutate in place."""
    for key, default in EMPTY_STATE.items():
        if key not in data:
            data[key] = [] if isinstance(default, list) else default
    return data


def _use_github():
    return bool(GITHUB_PAT)


def _headers():
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_PAT}",
    }


def _contents_url():
    return f"{_API_BASE}/repos/{GITHUB_REPO}/contents/{DATA_PATH_IN_REPO}"


def _load_github():
    """
    Returns (data, sha), always freshly fetched from GitHub - sha is None
    if the file doesn't exist yet. Used directly by load_for_update()
    (a write can never trust a cached sha) and by _load_github_cached()
    below (which is what load_data() actually calls).
    """
    resp = requests.get(_contents_url(), headers=_headers(), timeout=10)
    if resp.status_code == 404:
        return dict(EMPTY_STATE), None
    resp.raise_for_status()
    payload = resp.json()
    content = base64.b64decode(payload["content"]).decode("utf-8")
    data = json.loads(content) if content.strip() else dict(EMPTY_STATE)
    return _normalize(data), payload["sha"]


def _load_github_cached():
    """load_data()'s path: served from the in-memory cache when fresh (see
    _CACHE_TTL_SECONDS), a real fetch otherwise. json.loads() on the cached
    text hands back an independent dict tree every call, same as a fresh
    fetch would, since callers throughout app.py mutate `data` in place
    (annotate_wr_confidence and friends) and must never share that mutable
    state across requests."""
    if _cache_is_fresh():
        return json.loads(_cache["text"])
    data, _sha = _load_github()
    _remember(data)
    return data


def _save_github(data, sha, message):
    body = {
        "message": message,
        "content": base64.b64encode(json.dumps(data, indent=2).encode("utf-8")).decode("utf-8"),
    }
    if sha:
        body["sha"] = sha
    resp = requests.put(_contents_url(), headers=_headers(), json=body, timeout=10)
    resp.raise_for_status()
    _remember(data)


def _load_local():
    if LOCAL_PATH.exists():
        text = LOCAL_PATH.read_text()
        data = json.loads(text) if text.strip() else dict(EMPTY_STATE)
        return _normalize(data)
    return dict(EMPTY_STATE)


def _save_local(data):
    LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_PATH.write_text(json.dumps(data, indent=2))


def load_data():
    """Read-only fetch of the full data blob, for GET routes that render
    a page and never write anything back - served from the short-lived
    in-memory cache when possible (see _load_github_cached)."""
    if _use_github():
        return _load_github_cached()
    return _load_local()


def load_for_update():
    """
    (data, token) for a route that might write back. `token` is opaque -
    pass it straight to save(). Fetching it fresh here (rather than
    reusing one from an earlier page load) is what keeps a save from
    clobbering a change made in between.
    """
    if _use_github():
        return _load_github()
    return _load_local(), None


def save(data, token, message):
    if _use_github():
        _save_github(data, token, message)
    else:
        _save_local(data)


def mutate(fn, message):
    """
    Convenience for the common case: load, let `fn(data)` modify it in
    place and return whatever the caller wants back (e.g. a newly
    assigned id), then unconditionally save. For a route that might have
    nothing to change (so should skip writing an empty commit), use
    load_for_update()/save() directly instead.
    """
    data, token = load_for_update()
    result = fn(data)
    save(data, token, message)
    return result
