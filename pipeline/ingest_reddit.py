"""Reddit ingestion.

PRIMARY  — Arctic Shift API (subreddit posts + comments). Works from
           datacenter IPs; ~20-60 min indexing lag for comments, ~50 min posts.
COMPLEMENT — reddit.com search.rss for cross-reddit keyword hits. Best-effort:
           GitHub Actions shares Azure egress IPs, so 403/429 are expected
           sometimes; soft-skip and let the next cycle recover (sort=new,
           100-deep window).
NEVER    — reddit.com/*.json (hard-403 for scripts as of 2026, verified).

Cursor design (review fix): Arctic Shift indexes items AFTER the fact, so an
item with an older created_utc can appear later than a newer one. We therefore
always re-query an overlap window (cursor − margin) and let the unique
constraint absorb duplicates; the cursor itself advances to
min(max_created_seen, now − margin) — and on a write-cap stop, only to the
last safely-written item.
"""

from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

from . import db
from .config import Config, save_source_config
from .writer import MentionWriter, author_hash

ARCTIC = "https://arctic-shift.photon-reddit.com/api"
MAX_PAGES = 10


def _ua() -> str:
    contact = os.environ.get("CONTACT_EMAIL", "ops@example.invalid")
    return f"mention-radar/1.0 (sentiment monitor; contact: {contact})"


def _get(url: str, params: dict | None = None, timeout: int = 30) -> requests.Response:
    time.sleep(1.0)  # ≤1 req/s against Arctic Shift, always
    return requests.get(url, params=params, headers={"User-Agent": _ua()}, timeout=timeout)


def run(cfg: Config, stats) -> None:
    writer = MentionWriter(cfg, stats)
    freshest: list[float] = []  # max created_utc seen per arctic source
    arctic_ok = True

    for src in cfg.sources:
        if src["platform"] != "reddit":
            continue
        try:
            if src["kind"] == "subreddit":
                seen = _ingest_subreddit(src, cfg, writer, stats)
                if seen:
                    freshest.append(seen)
            elif src["kind"] == "search":
                _ingest_search_rss(src, cfg, writer, stats)
        except requests.RequestException as exc:
            arctic_ok = arctic_ok and src["kind"] != "subreddit"
            stats.incr("reddit_source_errors")
            print(f"[reddit] source {src['id']} error {type(exc).__name__}")
        if writer.remaining == 0:
            print("[reddit] daily write cap reached — stopping fetches")
            break

    _update_freshness_state(cfg, stats, freshest, arctic_ok)


# ---------------------------------------------------------------------------
# Arctic Shift: posts + comments per subreddit
# ---------------------------------------------------------------------------

def _ingest_subreddit(src: dict, cfg: Config, writer: MentionWriter, stats) -> float | None:
    scfg = dict(src.get("config") or {})
    now = time.time()
    max_seen_overall = 0.0

    for kind, endpoint, margin_key in (
        ("post", "posts", "reddit_post_margin_sec"),
        ("comment", "comments", "reddit_comment_margin_sec"),
    ):
        margin = int(cfg.ingest.get(margin_key, 7200))
        cursor_key = f"cursor_{kind}"
        cursor = float(scfg.get(cursor_key) or (now - 7 * 86400))  # first run: 7d backfill
        after = cursor - margin
        last_safe = cursor
        max_seen = 0.0

        for _ in range(MAX_PAGES):
            resp = _get(f"{ARCTIC}/{endpoint}/search", params={
                "subreddit": src["source_key"],
                "after": int(after),
                "sort": "asc",
                "limit": 100,
            })
            resp.raise_for_status()
            items = resp.json().get("data", [])
            if not items:
                break
            rows = [_arctic_row(it, kind, src) for it in items]
            result = writer.write_batch(rows)
            ts = [float(it.get("created_utc") or 0) for it in items]
            max_seen = max(max_seen, max(ts))
            if result.safe_count > 0:
                last_safe = max(last_safe, ts[result.safe_count - 1])
            if result.capped:
                break
            after = max(ts)
            if len(items) < 100:
                break

        if max_seen:
            max_seen_overall = max(max_seen_overall, max_seen)
            # normal advance: never past now−margin; cap stop: only to last safe item
            scfg[cursor_key] = min(last_safe if writer.remaining == 0 else max_seen,
                                   now - margin)
        scfg[f"freshness_{kind}"] = max_seen or scfg.get(f"freshness_{kind}")

    save_source_config(src["id"], scfg)
    return max_seen_overall or None


def _arctic_row(it: dict, kind: str, src: dict) -> dict:
    created = datetime.fromtimestamp(float(it.get("created_utc") or 0), tz=timezone.utc)
    if kind == "post":
        ext_id = f"t3_{it['id']}"
        title = it.get("title") or ""
        body = it.get("selftext") or ""
        url = "https://www.reddit.com" + (it.get("permalink") or f"/comments/{it['id']}/")
        metrics = {"score": it.get("score", 0), "num_comments": it.get("num_comments", 0)}
        parent = None
    else:
        ext_id = f"t1_{it['id']}"
        title = None
        body = it.get("body") or ""
        link = (it.get("link_id") or "").removeprefix("t3_")
        url = ("https://www.reddit.com" + it["permalink"]) if it.get("permalink") \
            else f"https://www.reddit.com/comments/{link}/_/{it['id']}/"
        metrics = {"score": it.get("score", 0)}
        parent = it.get("link_id")
    return {
        "platform": "reddit",
        "source_id": src["id"],
        "external_id": ext_id,
        "kind": kind,
        "parent_external_id": parent,
        "url": url,
        "author_hash": author_hash("reddit", it.get("author")),
        "title": title,
        "body": body,
        "published_at": db.iso(created),
        "metrics": metrics,
        "_match_text": f"{title or ''}\n{body}",
    }


# ---------------------------------------------------------------------------
# reddit search.rss — best-effort cross-reddit keyword sweep
# ---------------------------------------------------------------------------

_ATOM = "{http://www.w3.org/2005/Atom}"
_TAG_RE = re.compile(r"<[^>]+>")
_ID_RE = re.compile(r"(t3_[a-z0-9]+)")


def _ingest_search_rss(src: dict, cfg: Config, writer: MentionWriter, stats) -> None:
    scfg = dict(src.get("config") or {})
    try:
        resp = requests.get(
            "https://www.reddit.com/search.rss",
            params={"q": src["source_key"], "sort": "new", "limit": 100},
            headers={"User-Agent": _ua()},
            timeout=30,
        )
    except requests.RequestException:
        stats.incr("rss_soft_skips")
        return
    if resp.status_code != 200:
        # Azure IP lottery — expected. Soft-skip; next cycle recovers the window.
        stats.incr("rss_soft_skips")
        stats["rss_last_status"] = resp.status_code
        return

    rows = []
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        stats.incr("rss_soft_skips")
        return
    for entry in root.findall(f"{_ATOM}entry"):
        raw_id = (entry.findtext(f"{_ATOM}id") or "")
        m = _ID_RE.search(raw_id)
        if not m:
            continue
        title = entry.findtext(f"{_ATOM}title") or ""
        content = _TAG_RE.sub(" ", entry.findtext(f"{_ATOM}content") or "")
        link_el = entry.find(f"{_ATOM}link")
        url = link_el.get("href") if link_el is not None else None
        published = entry.findtext(f"{_ATOM}published") or entry.findtext(f"{_ATOM}updated")
        author = entry.findtext(f"{_ATOM}author/{_ATOM}name")
        rows.append({
            "platform": "reddit",
            "source_id": src["id"],
            "external_id": m.group(1),
            "kind": "post",
            "parent_external_id": None,
            "url": url,
            "author_hash": author_hash("reddit", author),
            "title": title,
            "body": content.strip()[:5000],
            "published_at": published or db.iso(db.now_utc()),
            "metrics": {},
            "_match_text": f"{title}\n{content}",
        })
    if rows:
        rows.sort(key=lambda r: r["published_at"])
        writer.write_batch(rows)
    scfg["rss_last_success"] = db.iso(db.now_utc())
    save_source_config(src["id"], scfg)
    stats.incr("rss_entries", len(rows))


# ---------------------------------------------------------------------------
# Freshness probe (Pushshift died returning HTTP 200 — detect silent staleness)
# ---------------------------------------------------------------------------

def _update_freshness_state(cfg: Config, stats, freshest: list[float], arctic_ok: bool) -> None:
    state = db.get_app_config("ingest_state", default={})
    fail_hours = float(cfg.ingest.get("freshness_fail_hours", 6))
    lag_h = None
    if freshest:
        lag_h = (time.time() - max(freshest)) / 3600.0
        stats["freshness_lag_h"] = round(lag_h, 2)
    stale = (lag_h is None) or (lag_h > fail_hours) or (not arctic_ok)
    state["arctic_stale_cycles"] = (state.get("arctic_stale_cycles", 0) + 1) if stale else 0
    state["arctic_last_lag_h"] = round(lag_h, 2) if lag_h is not None else None
    db.client().table("app_config").upsert(
        {"key": "ingest_state", "value": state}, on_conflict="key"
    ).execute()
    if stale:
        stats["arctic_stale_cycles"] = state["arctic_stale_cycles"]


def arctic_is_stale(cfg: Config) -> bool:
    state = db.get_app_config("ingest_state", default={})
    return state.get("arctic_stale_cycles", 0) >= int(cfg.ingest.get("freshness_fail_cycles", 3))
