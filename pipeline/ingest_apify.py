"""Apify Reddit fallback — fires only when the freshness probe says Arctic
Shift is stale/failing (see ingest_reddit.arctic_is_stale).

Cost ceiling is PER-ITEM, not per-run (review fix): the actor bills
$1.15/1k posts + $0.575/1k comments, so maxItems is the real cap.
Defaults (app_config.apify): max_items_per_run=500, max_runs_per_day=1
→ theoretical ceiling ≈ $10/month in a full-outage month, in exchange for
degraded coverage (posts-first). Spend > monthly_spend_alert_usd raises a
visible flag in run stats.

NOTE: the actor input schema below targets automation-lab/reddit-scraper as of
2026-06. Verify against the actor's current input schema before first use
(docs/SETUP.md step 7).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import requests

from . import db
from .config import Config
from .writer import MentionWriter, author_hash

APIFY = "https://api.apify.com/v2"


def run_if_needed(cfg: Config, stats, stale: bool) -> None:
    acfg = cfg.apify
    if not stale or not acfg.get("enabled_as_fallback"):
        return
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        stats["apify_skipped"] = "no token configured"
        return

    state = db.get_app_config("ingest_state", default={})
    today = db.now_utc().date().isoformat()
    runs_today = state.get("apify_runs", {}).get(today, 0)
    if runs_today >= int(acfg.get("max_runs_per_day", 1)):
        stats["apify_skipped"] = "daily run cap"
        return

    subreddits = [s["source_key"] for s in cfg.sources
                  if s["platform"] == "reddit" and s["kind"] == "subreddit"]
    if not subreddits:
        return
    max_items = int(acfg.get("max_items_per_run", 500))

    # Persist the run counter BEFORE the call returns: the actor is billed the
    # moment the POST starts, so if raise_for_status() (e.g. 408 on the 300s sync
    # endpoint) or the later write raises, the counter must already be incremented
    # — otherwise every 20-min cycle re-bills while Arctic stays stale (~72 runs/day
    # instead of the 1/day cost ceiling).
    runs = state.get("apify_runs", {})
    state["apify_runs"] = {today: runs_today + 1}  # keep only today
    db.client().table("app_config").upsert(
        {"key": "ingest_state", "value": state}, on_conflict="key").execute()

    # One run covering ALL subreddits (start fees are per run).
    actor = acfg.get("actor", "automation-lab~reddit-scraper")
    resp = requests.post(
        f"{APIFY}/acts/{actor}/run-sync-get-dataset-items",
        params={"token": token, "timeout": 300},
        json={
            "subreddits": subreddits,
            "maxItems": max_items,
            "sort": "new",
            "type": "posts",   # degraded coverage: posts first, comments sacrificed
        },
        timeout=330,
    )
    resp.raise_for_status()
    items = resp.json()
    stats["apify_items"] = len(items)

    writer = MentionWriter(cfg, stats)
    rows = []
    for it in items:
        ext = it.get("id") or it.get("postId") or ""
        if not ext:
            continue
        if not ext.startswith("t3_"):
            ext = f"t3_{ext}"
        created = it.get("createdAt") or it.get("created_utc")
        if isinstance(created, (int, float)):
            created = db.iso(datetime.fromtimestamp(created, tz=timezone.utc))
        title = it.get("title") or ""
        body = it.get("body") or it.get("selftext") or ""
        rows.append({
            "platform": "reddit",
            "source_id": None,
            "external_id": ext,
            "kind": "post",
            "parent_external_id": None,
            "url": it.get("url") or it.get("link"),
            "author_hash": author_hash("reddit", it.get("author") or it.get("username")),
            "title": title,
            "body": body[:5000],
            "published_at": created or db.iso(db.now_utc()),
            "metrics": {"score": it.get("score", 0)},
            "_match_text": f"{title}\n{body}",
        })
    if rows:
        rows.sort(key=lambda r: r["published_at"])
        writer.write_batch(rows)

    # spend estimate (re-read state to avoid clobbering the counter just written)
    state = db.get_app_config("ingest_state", default=state)
    month = today[:7]
    est = state.get("apify_month_spend_usd", {})
    spend = est.get(month, 0) + len(items) * 0.00115 + 0.003
    state["apify_month_spend_usd"] = {month: round(spend, 2)}
    db.client().table("app_config").upsert(
        {"key": "ingest_state", "value": state}, on_conflict="key").execute()
    stats["apify_month_spend_usd"] = round(spend, 2)
    if spend > float(acfg.get("monthly_spend_alert_usd", 4)):
        stats["apify_spend_alert"] = True
