"""YouTube ingestion under the 2026-06 granular quota system.

Quota strategy (review fix for the blocker — naive per-cycle comment scans cost
2,160-10,800 units/day and die exactly during viral events):

  * New-video detection: channel RSS feeds          → 0 units
  * Stats gate: videos.list 50 ids/call, every cycle → ~72-144 units/day
  * commentThreads ONLY for videos whose commentCount moved, tiered cadence
    (<72h old: every cycle; 72h-14d: first cycle of each hour)
  * Old-thread replies: totalReplyCount delta → comments.list(parentId)
  * Keyword discovery: search.list ≤5/day (its own 100-call bucket)
  * Hard guard: when today's main-pool units exceed the budget (app_config,
    default 5000), comment polling degrades to <72h videos only.

ToS: comment bodies are retention-purged at 30 days (retention job), deletion
requests honored daily — this module only ingests.
"""

from __future__ import annotations

import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

from . import db
from .config import Config, save_source_config
from .writer import MentionWriter, author_hash

API = "https://www.googleapis.com/youtube/v3"
_ATOM = "{http://www.w3.org/2005/Atom}"
_YT = "{http://www.youtube.com/xml/schemas/2015}"
_MEDIA = "{http://search.yahoo.com/mrss/}"

MAX_COMMENT_PAGES = 5
MAX_REPLY_PAGES = 2
MAX_TRACKED_THREADS = 200
SEARCH_MIN_INTERVAL_H = 4.8


class CommentsDisabled(Exception):
    pass


def run(cfg: Config, stats) -> None:
    writer = MentionWriter(cfg, stats)
    budget = int(cfg.ingest.get("yt_daily_unit_budget", 5000))
    used_before = _units_used_today()
    stats["yt_units_before"] = used_before

    def units_now() -> int:
        return used_before + int(stats.get("yt_units", 0))

    channel_sources = [s for s in cfg.sources if s["platform"] == "youtube" and s["kind"] == "channel"]
    search_sources = [s for s in cfg.sources if s["platform"] == "youtube" and s["kind"] == "search"]

    # 1. RSS new-video detection (0 units) + search discovery (own bucket).
    #    Skip the write phases once the daily write cap is exhausted (mirrors
    #    ingest_reddit) so we never register videos we couldn't store.
    for src in channel_sources:
        if writer.remaining == 0:
            break
        _poll_channel_rss(src, cfg, writer, stats)
    for src in search_sources:
        if writer.remaining == 0:
            break
        _maybe_search(src, cfg, writer, stats)

    # 2. Stats gate over every active video (cheap, every cycle)
    video_index = _collect_active_videos(cfg)
    _stats_gate(video_index, stats)

    # 3. Comments, gated + tiered + budget-guarded.
    #    Hourly tier uses a persisted timestamp, not minute<20 — GHA cron jitter
    #    (5-30 min) regularly pushes the :07 run past :20, silently skipping it.
    istate = db.get_app_config("ingest_state", default={})
    last_hourly = istate.get("yt_last_hourly_tier_at")
    hourly_tier_allowed = (last_hourly is None) or \
        (_age_hours(last_hourly) >= 0.92)  # ~55 min
    every_cycle_h = float(cfg.ingest.get("yt_every_cycle_hours", 72))
    hourly_tier_ran = False
    for (src, vid), v in video_index.items():
        if v.get("comments_disabled"):
            continue
        if int(v.get("comment_count", 0)) <= int(v.get("last_comment_count", 0)):
            continue
        age_h = _age_hours(v.get("published"))
        if age_h > every_cycle_h and not hourly_tier_allowed:
            continue
        degraded = units_now() >= budget
        if degraded and age_h > every_cycle_h:
            stats["yt_degraded"] = True
            continue
        if units_now() >= budget * 2:  # absolute stop, even for fresh videos
            stats["yt_degraded"] = True
            break
        if writer.remaining == 0:
            break
        try:
            capped = _fetch_comments(src, vid, v, writer, stats)
            if age_h > every_cycle_h:
                hourly_tier_ran = True
            # Only close the commentCount gate when we actually drained the new
            # comments. On cap (capped) or exception, leave it open so the next
            # cycle retries instead of permanently skipping the new comments.
            if not capped:
                v["last_comment_count"] = int(v.get("comment_count", 0))
        except CommentsDisabled:
            v["comments_disabled"] = True
            v["last_comment_count"] = int(v.get("comment_count", 0))
        except requests.RequestException:
            stats.incr("yt_comment_errors")  # gate stays open → retried next cycle

    # 4. Persist per-source video state + the hourly-tier timestamp
    for src, scfg in _DIRTY_SOURCE_CONFIGS.items():
        save_source_config(src, scfg)
    _DIRTY_SOURCE_CONFIGS.clear()
    if hourly_tier_ran:
        istate["yt_last_hourly_tier_at"] = db.iso(db.now_utc())
        db.client().table("app_config").upsert(
            {"key": "ingest_state", "value": istate}, on_conflict="key").execute()


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

_DIRTY_SOURCE_CONFIGS: dict[int, dict] = {}


def _source_state(src: dict) -> dict:
    if src["id"] not in _DIRTY_SOURCE_CONFIGS:
        _DIRTY_SOURCE_CONFIGS[src["id"]] = dict(src.get("config") or {})
        _DIRTY_SOURCE_CONFIGS[src["id"]].setdefault("videos", {})
    return _DIRTY_SOURCE_CONFIGS[src["id"]]


def _yt_get(endpoint: str, params: dict, stats, unit_key: str = "yt_units") -> dict:
    p = dict(params)
    p["key"] = os.environ["YT_API_KEY"]
    resp = requests.get(f"{API}/{endpoint}", params=p, timeout=30)
    stats.incr(unit_key)
    if resp.status_code == 403:
        body = resp.text[:500]
        if "commentsDisabled" in body:
            raise CommentsDisabled()
        resp.raise_for_status()
    resp.raise_for_status()
    return resp.json()


def _units_used_today() -> int:
    midnight = db.now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (
        db.client().table("pipeline_runs")
        .select("stats")
        .eq("job", "ingest")
        .gte("started_at", db.iso(midnight))
        .execute().data
    )
    return sum(int((r.get("stats") or {}).get("yt_units", 0)) for r in rows)


def _age_hours(published_iso: str | None) -> float:
    if not published_iso:
        return 9_999
    try:
        dt = datetime.fromisoformat(published_iso.replace("Z", "+00:00"))
    except ValueError:
        return 9_999
    return (db.now_utc() - dt).total_seconds() / 3600.0


def _z(dt: datetime) -> str:
    """Canonical RFC3339 'Z', whole seconds — the only form YouTube's
    publishedAfter reliably accepts (db.iso() emits microseconds + numeric
    offset, which can 400 invalidSearchFilter)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 1a. Channel RSS (0 quota)
# ---------------------------------------------------------------------------

def _poll_channel_rss(src: dict, cfg: Config, writer: MentionWriter, stats) -> None:
    state = _source_state(src)
    try:
        resp = requests.get(
            "https://www.youtube.com/feeds/videos.xml",
            params={"channel_id": src["source_key"]},
            timeout=30,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except (requests.RequestException, ET.ParseError):
        stats.incr("yt_rss_errors")
        return

    active_days = float(cfg.ingest.get("yt_active_video_days", 14))
    rows = []
    for entry in root.findall(f"{_ATOM}entry"):
        vid = entry.findtext(f"{_YT}videoId")
        if not vid:
            continue
        published = entry.findtext(f"{_ATOM}published") or db.iso(db.now_utc())
        title = entry.findtext(f"{_ATOM}title") or ""
        desc = entry.findtext(f"{_MEDIA}group/{_MEDIA}description") or ""
        author = entry.findtext(f"{_ATOM}author/{_ATOM}name")
        if vid in state["videos"] or _age_hours(published) > active_days * 24:
            continue
        rows.append({
            "platform": "youtube",
            "source_id": src["id"],
            "external_id": vid,
            "kind": "video",
            "parent_external_id": None,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "author_hash": author_hash("youtube", author),
            "title": title,
            "body": desc[:5000],
            "published_at": published,
            "metrics": {},
            "_match_text": f"{title}\n{desc}",
        })
    if rows:
        rows.sort(key=lambda r: r["published_at"])
        result = writer.write_batch([dict(r) for r in rows])
        # Register for comment tracking ONLY videos whose row was safely written
        # (within safe_count). On cap, unwritten videos stay unregistered and are
        # retried next cycle instead of silently lost.
        for r in rows[: result.safe_count]:
            state["videos"][r["external_id"]] = {
                "published": r["published_at"],
                "last_comment_count": 0,
                "last_comment_ts": None,
                "threads": {},
            }

    # prune videos out of the active window
    state["videos"] = {
        v: meta for v, meta in state["videos"].items()
        if _age_hours(meta.get("published")) <= active_days * 24
    }


# ---------------------------------------------------------------------------
# 1b. search.list discovery (separate 100-call/day bucket; we use ≤5)
# ---------------------------------------------------------------------------

def _maybe_search(src: dict, cfg: Config, writer: MentionWriter, stats) -> None:
    state = _source_state(src)
    last = state.get("last_search_at")
    if last and _age_hours(last) < SEARCH_MIN_INTERVAL_H:
        return
    # Overlap-cursor: search.list indexing lags, so query from cursor − margin
    # (canonical Z form) and let unique(platform,external_id) dedup the overlap.
    margin_h = float(cfg.ingest.get("yt_search_margin_hours", 12))
    cursor_iso = state.get("search_cursor")
    if cursor_iso:
        base_dt = datetime.fromisoformat(cursor_iso.replace("Z", "+00:00")) \
            - timedelta(hours=margin_h)
    else:
        base_dt = db.now_utc() - timedelta(days=7)
    try:
        data = _yt_get("search", {
            "part": "snippet",
            "q": src["source_key"],
            "type": "video",
            "order": "date",
            "maxResults": 50,
            "publishedAfter": _z(base_dt),
        }, stats, unit_key="yt_search_calls")
    except requests.RequestException:
        stats.incr("yt_search_errors")
        return
    state["last_search_at"] = db.iso(db.now_utc())

    rows = []
    for item in data.get("items", []):
        vid = (item.get("id") or {}).get("videoId")
        sn = item.get("snippet") or {}
        if not vid:
            continue
        published = sn.get("publishedAt") or _z(db.now_utc())
        title, desc = sn.get("title", ""), sn.get("description", "")
        rows.append({
            "platform": "youtube",
            "source_id": src["id"],
            "external_id": vid,
            "kind": "video",
            "parent_external_id": None,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "author_hash": author_hash("youtube", sn.get("channelId")),
            "title": title,
            "body": desc[:5000],
            "published_at": published,
            "metrics": {},
            "_match_text": f"{title}\n{desc}",
        })
    if rows:
        rows.sort(key=lambda r: r["published_at"])
        result = writer.write_batch([dict(r) for r in rows])
        safe = rows[: result.safe_count]
        for r in safe:
            if r["external_id"] not in state["videos"]:
                state["videos"][r["external_id"]] = {
                    "published": r["published_at"],
                    "last_comment_count": 0,
                    "last_comment_ts": None,
                    "threads": {},
                }
        # advance the cursor only over safely-written rows, monotonically
        if safe:
            newest = max(r["published_at"] for r in safe)
            if not cursor_iso or newest > cursor_iso:
                state["search_cursor"] = newest


# ---------------------------------------------------------------------------
# 2. Stats gate
# ---------------------------------------------------------------------------

def _collect_active_videos(cfg: Config) -> dict[tuple[int, str], dict]:
    index: dict[tuple[int, str], dict] = {}
    for src in cfg.sources:
        if src["platform"] != "youtube":
            continue
        state = _source_state(src)
        for vid, meta in state["videos"].items():
            index[(src["id"], vid)] = meta
    return index


def _stats_gate(video_index: dict[tuple[int, str], dict], stats) -> None:
    ids = sorted({vid for (_, vid) in video_index})
    for i in range(0, len(ids), 50):
        chunk = ids[i : i + 50]
        try:
            data = _yt_get("videos", {"part": "statistics", "id": ",".join(chunk),
                                      "maxResults": 50}, stats)
        except requests.RequestException:
            stats.incr("yt_stats_errors")
            continue
        by_id = {item["id"]: item.get("statistics", {}) for item in data.get("items", [])}
        changed: dict[str, dict] = {}
        for (sid, vid), meta in video_index.items():
            st = by_id.get(vid)
            if st is None:
                continue
            cc = int(st.get("commentCount", 0) or 0)
            vc = int(st.get("viewCount", 0) or 0)
            lc = int(st.get("likeCount", 0) or 0)
            # only push a metrics UPDATE when a number actually moved (avoids
            # one zero-effect PostgREST write per active video per cycle)
            if cc != meta.get("comment_count") or vc != meta.get("view_count") \
                    or lc != meta.get("like_count"):
                changed[vid] = {"views": vc, "likes": lc, "comments": cc}
            meta["comment_count"] = cc
            meta["view_count"] = vc
            meta["like_count"] = lc
        for vid, metrics in changed.items():
            db.client().table("mentions").update({"metrics": metrics}) \
                .eq("platform", "youtube").eq("external_id", vid).execute()
        stats.incr("yt_metric_updates", len(changed))


# ---------------------------------------------------------------------------
# 3. Comments (top-level via commentThreads, replies via comments.list)
# ---------------------------------------------------------------------------

def _fetch_comments(src_id: int, vid: str, meta: dict, writer: MentionWriter, stats) -> bool:
    """Returns True if the write was cap-truncated. On cap we commit NOTHING to
    meta (last_comment_ts / threads), so the caller leaves the commentCount gate
    open and the next cycle re-fetches; dedup absorbs the overlap."""
    last_ts = meta.get("last_comment_ts")
    threads: dict[str, int] = dict(meta.get("threads") or {})  # copy; commit only on success
    rows, page_token, newest_ts = [], None, last_ts
    reply_targets: list[str] = []

    for _ in range(MAX_COMMENT_PAGES):
        params = {"part": "snippet", "videoId": vid, "order": "time", "maxResults": 100}
        if page_token:
            params["pageToken"] = page_token
        data = _yt_get("commentThreads", params, stats)
        items = data.get("items", [])
        if not items:
            break
        page_older = False
        for th in items:
            sn = th["snippet"]
            top = sn["topLevelComment"]
            trc = int(sn.get("totalReplyCount", 0))
            cid = top["id"]
            published = top["snippet"].get("publishedAt")
            if trc > threads.get(cid, 0):
                reply_targets.append(cid)
            if trc > 0:
                threads[cid] = trc
            if last_ts and published and published <= last_ts:
                page_older = True
                continue  # finish scanning this page for reply deltas, then stop
            newest_ts = max(newest_ts or "", published or "")
            rows.append(_comment_row(top, vid, src_id))
        page_token = data.get("nextPageToken")
        if page_older or not page_token:
            break

    for cid in reply_targets[:50]:
        page_token = None
        for _ in range(MAX_REPLY_PAGES):
            params = {"part": "snippet", "parentId": cid, "maxResults": 100}
            if page_token:
                params["pageToken"] = page_token
            data = _yt_get("comments", params, stats)
            for c in data.get("items", []):
                rows.append(_comment_row(c, vid, src_id))
            page_token = data.get("nextPageToken")
            if not page_token:
                break

    capped = False
    if rows:
        rows.sort(key=lambda r: r["published_at"])
        result = writer.write_batch([dict(r) for r in rows])
        capped = result.capped
        stats.incr("yt_comments", result.inserted)

    if capped:
        return True  # commit nothing — gate stays open, next cycle re-fetches

    if newest_ts:
        meta["last_comment_ts"] = newest_ts
    # bound per-video thread state
    if len(threads) > MAX_TRACKED_THREADS:
        threads = dict(sorted(threads.items(), key=lambda kv: kv[1], reverse=True)[:MAX_TRACKED_THREADS])
    meta["threads"] = threads
    return False


def _comment_row(comment: dict, vid: str, src_id: int) -> dict:
    sn = comment["snippet"]
    text = sn.get("textOriginal") or sn.get("textDisplay") or ""
    cid = comment["id"]
    parent = sn.get("parentId") or vid
    return {
        "platform": "youtube",
        "source_id": src_id,
        "external_id": cid,
        "kind": "comment",
        "parent_external_id": parent,
        "url": f"https://www.youtube.com/watch?v={vid}&lc={cid}",
        "author_hash": author_hash("youtube", sn.get("authorChannelId", {}).get("value")
                                   if isinstance(sn.get("authorChannelId"), dict)
                                   else sn.get("authorDisplayName")),
        "title": None,
        "body": text[:5000],
        "lang": None,
        "published_at": sn.get("publishedAt") or db.iso(db.now_utc()),
        "metrics": {"likes": int(sn.get("likeCount", 0) or 0)},
        "_match_text": text,
    }
