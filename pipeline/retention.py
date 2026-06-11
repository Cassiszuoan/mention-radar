"""Daily retention / compliance / maintenance job.

1. YouTube bodies: purge at 30 days  (YT Dev Policy III.E.4.d — Non-Authorized
   Data ≤30 days; derived sentiment/aggregates are ours and stay forever).
2. YouTube deletion requests: DAILY existence check over stored <30d comments
   (policy III.E.4.g wants deletions honored within 7 days; daily ≪ 7d).
3. Reddit bodies: archive (gzip JSONL → Storage) then purge at 90 days.
4. Mention rows: delete at 180 days (review fix: 365d would crowd the 500MB
   free tier; trend history lives in agg_daily forever).
5. agg_hourly: trim to 90 days.
6. Reddit metric refresh for 24-48h-old mentions (Arctic Shift scores arrive
   ~36h late). Never touches body; never creates candidate pairs.
7. Failed sentiment rows: one retry with the fallback model (<7d old).
8. Disk watchdog: pg_database_size, alert >350MB.
9. This job's REST traffic doubles as the Supabase free-tier keep-alive.
"""

from __future__ import annotations

import gzip
import io
import json
import os
from datetime import timedelta

import requests

from . import db, sentiment
from .config import Config

YT_BODY_DAYS = 30
REDDIT_BODY_DAYS = 90
ROW_DAYS = 180
AGG_HOURLY_DAYS = 90
DB_ALERT_BYTES = 350 * 1024 * 1024
BATCH = 500


def run(cfg: Config, stats) -> None:
    _youtube_deletion_check(stats)
    _purge_youtube_bodies(stats)
    _archive_and_purge_reddit_bodies(stats)
    _delete_old_rows(stats)
    _trim_agg_hourly(stats)
    _refresh_reddit_metrics(stats)
    _retry_failed_sentiment(cfg, stats)
    _disk_watchdog(stats)


# ---------------------------------------------------------------------------

def _purge_youtube_bodies(stats) -> None:
    cutoff = db.iso(db.now_utc() - timedelta(days=YT_BODY_DAYS))
    res = db.client().table("mentions").update(
        {"body": None, "title": None, "body_purged_at": db.iso(db.now_utc())}
    ).eq("platform", "youtube").lt("published_at", cutoff) \
     .is_("body_purged_at", "null").execute()
    stats["yt_bodies_purged"] = len(res.data or [])


def _youtube_deletion_check(stats) -> None:
    """Comments deleted upstream must be purged here too. Batch-verify the
    existence of every stored, un-purged YT comment id (50 ids/call, 1 unit)."""
    key = os.environ.get("YT_API_KEY")
    if not key:
        return
    cutoff = db.iso(db.now_utc() - timedelta(days=YT_BODY_DAYS))
    rows = (
        db.client().table("mentions").select("id, external_id")
        .eq("platform", "youtube").eq("kind", "comment")
        .gte("published_at", cutoff).is_("body_purged_at", "null")
        .limit(3000).execute().data
    )
    purged = 0
    for i in range(0, len(rows), 50):
        chunk = rows[i : i + 50]
        try:
            resp = requests.get(
                "https://www.googleapis.com/youtube/v3/comments",
                params={"part": "id", "id": ",".join(r["external_id"] for r in chunk),
                        "key": key},
                timeout=30,
            )
            stats.incr("yt_units")
            resp.raise_for_status()
            alive = {item["id"] for item in resp.json().get("items", [])}
        except requests.RequestException:
            stats.incr("yt_deletion_check_errors")
            continue
        gone = [r["id"] for r in chunk if r["external_id"] not in alive]
        if gone:
            db.client().table("mentions").update(
                {"body": None, "body_purged_at": db.iso(db.now_utc())}
            ).in_("id", gone).execute()
            purged += len(gone)
    stats["yt_deleted_upstream_purged"] = purged


def _archive_and_purge_reddit_bodies(stats) -> None:
    cutoff = db.iso(db.now_utc() - timedelta(days=REDDIT_BODY_DAYS))
    rows = (
        db.client().table("mentions")
        .select("id, external_id, kind, url, title, body, published_at")
        .eq("platform", "reddit").lt("published_at", cutoff)
        .is_("body_purged_at", "null").not_.is_("body", "null")
        .limit(2000).execute().data
    )
    if not rows:
        return
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        for r in rows:
            gz.write((json.dumps(r, ensure_ascii=False, default=str) + "\n").encode())
    path = f"reddit/{db.now_utc().date().isoformat()}.jsonl.gz"
    try:
        db.client().storage.from_("archives").upload(
            path, buf.getvalue(), {"content-type": "application/gzip", "upsert": "true"}
        )
    except Exception:  # noqa: BLE001 — archive failure must NOT purge bodies
        stats["reddit_archive_failed"] = True
        return
    ids = [r["id"] for r in rows]
    for i in range(0, len(ids), BATCH):
        db.client().table("mentions").update(
            {"body": None, "body_purged_at": db.iso(db.now_utc())}
        ).in_("id", ids[i : i + BATCH]).execute()
    stats["reddit_bodies_archived"] = len(ids)
    stats["reddit_archive_path"] = path


def _delete_old_rows(stats) -> None:
    cutoff = db.iso(db.now_utc() - timedelta(days=ROW_DAYS))
    res = db.client().table("mentions").delete().lt("published_at", cutoff).execute()
    stats["rows_deleted"] = len(res.data or [])


def _trim_agg_hourly(stats) -> None:
    cutoff = db.iso(db.now_utc() - timedelta(days=AGG_HOURLY_DAYS))
    db.client().table("agg_hourly").delete().lt("bucket", cutoff).execute()


def _refresh_reddit_metrics(stats) -> None:
    """Arctic Shift scores finalize ~36h after posting; refresh the 24-48h band.
    Best-effort: the ids endpoint shape may evolve — soft-fail."""
    lo = db.iso(db.now_utc() - timedelta(hours=48))
    hi = db.iso(db.now_utc() - timedelta(hours=24))
    rows = (
        db.client().table("mentions").select("id, external_id, kind")
        .eq("platform", "reddit").eq("kind", "post")
        .gte("published_at", lo).lt("published_at", hi)
        .limit(500).execute().data
    )
    refreshed = 0
    contact = os.environ.get("CONTACT_EMAIL", "ops@example.invalid")
    for i in range(0, len(rows), 100):
        chunk = rows[i : i + 100]
        ids = ",".join(r["external_id"].removeprefix("t3_") for r in chunk)
        try:
            resp = requests.get(
                "https://arctic-shift.photon-reddit.com/api/posts/ids",
                params={"ids": ids},
                headers={"User-Agent": f"mention-radar/1.0 (contact: {contact})"},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
        except (requests.RequestException, ValueError):
            stats.incr("metric_refresh_errors")
            continue
        by_id = {f"t3_{d['id']}": d for d in data if d.get("id")}
        for r in chunk:
            d = by_id.get(r["external_id"])
            if not d:
                continue
            db.client().table("mentions").update({
                "metrics": {"score": d.get("score", 0),
                            "num_comments": d.get("num_comments", 0)}
            }).eq("id", r["id"]).execute()
            refreshed += 1
    stats["reddit_metrics_refreshed"] = refreshed


def _retry_failed_sentiment(cfg: Config, stats) -> None:
    fallback = cfg.gemini.get("fallback_model")
    if fallback:
        sentiment.run(cfg, stats, model_override=fallback, only_failed=True)


def _disk_watchdog(stats) -> None:
    try:
        size = db.client().rpc("fn_db_size", {}).execute().data
        stats["db_bytes"] = size
        if isinstance(size, int) and size > DB_ALERT_BYTES:
            stats["db_size_alert"] = True
            print(f"[retention] WARNING db size {size/1e6:.0f}MB > 350MB threshold")
    except Exception:  # noqa: BLE001
        stats["db_size_check_failed"] = True
