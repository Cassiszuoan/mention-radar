"""Alert detector — runs every cycle right after aggregation.

Volume spike : the 3 most recent COMPLETE hour buckets are re-evaluated each
cycle (source indexing lag keeps filling buckets after they close; re-scoring
turns missed alerts into late alerts). Baselines are zero-filled in SQL.
Idempotency: alerts unique(entity_id, type, window_start) — re-evaluation
updates severity upward, never duplicates.

Sentiment drop: trailing 24h weighted average vs prior 7 days, skipped (and
flagged degraded) when analyzed_ratio < threshold, so a Gemini backlog can
never masquerade as a sentiment crash.

Severity (review fix): for quiet entities the sigma floor makes z explode
(z≥9 on 10 mentions), so `high` additionally requires an absolute count.
"""

from __future__ import annotations

import json
from datetime import timedelta

from . import db
from .config import Config, entity_thresholds


def run(cfg: Config, stats) -> None:
    defaults = cfg.alert_defaults
    _check_volume(cfg, defaults, stats)
    _check_sentiment(cfg, defaults, stats)


# ---------------------------------------------------------------------------

def _check_volume(cfg: Config, defaults: dict, stats) -> None:
    vol_defaults = defaults.get("volume", {})
    rows = db.client().rpc("fn_volume_check", {
        "p_buckets": int(vol_defaults.get("eval_trailing_buckets", 3)),
        "p_baseline_hours": int(vol_defaults.get("baseline_hours", 168)),
    }).execute().data or []

    warmup_h = float(defaults.get("new_entity_warmup_hours", 72))
    for r in rows:
        ent = cfg.entities.get(r["entity_id"])
        if ent is None:
            continue
        th = entity_thresholds(ent, defaults)
        v = th.get("volume", vol_defaults)
        if float(r.get("data_age_hours") or 0) < warmup_h:
            continue
        n, z = int(r["current_n"]), float(r["z"] or 0)
        if n < int(v.get("min_count", 10)) or z < float(v.get("z", 3.0)):
            continue
        severity = "high" if (z >= float(v.get("high_z", 5.0))
                              and n >= int(v.get("high_min_count", 20))) else "watch"
        _upsert_alert(
            cfg, stats,
            entity_id=r["entity_id"], type_="volume_spike", severity=severity,
            window_start=r["window_start"],
            window_end=None,
            observed=n, baseline=float(r["mu"] or 0), zscore=z,
            cooldown_h=float(th.get("cooldown_hours", 12)),
        )


def _check_sentiment(cfg: Config, defaults: dict, stats) -> None:
    sen_defaults = defaults.get("sentiment", {})
    rows = db.client().rpc("fn_sentiment_check", {
        "p_window_hours": int(sen_defaults.get("window_hours", 24)),
        "p_baseline_days": int(sen_defaults.get("baseline_days", 7)),
    }).execute().data or []

    warmup_h = float(defaults.get("new_entity_warmup_hours", 72))
    for r in rows:
        ent = cfg.entities.get(r["entity_id"])
        if ent is None:
            continue
        th = entity_thresholds(ent, defaults)
        s = th.get("sentiment", sen_defaults)
        if float(r.get("data_age_hours") or 0) < warmup_h:
            continue
        if r.get("current_avg") is None or r.get("baseline_avg") is None:
            continue
        ratio = float(r.get("analyzed_ratio") or 0)
        if ratio < float(s.get("min_analyzed_ratio", 0.8)):
            stats.incr("sentiment_skipped_degraded")
            continue
        if int(r["analyzed_n"]) < int(s.get("min_mentions", 15)):
            continue
        drop = float(r["baseline_avg"]) - float(r["current_avg"])
        if drop < float(s.get("drop", 0.25)):
            continue
        severity = "high" if drop >= float(s.get("high_drop", 0.40)) else "watch"
        _upsert_alert(
            cfg, stats,
            entity_id=r["entity_id"], type_="sentiment_drop", severity=severity,
            window_start=r["window_start"], window_end=None,
            observed=float(r["current_avg"]), baseline=float(r["baseline_avg"]),
            zscore=round(drop, 3),
            cooldown_h=float(th.get("cooldown_hours", 12)),
        )


# ---------------------------------------------------------------------------

_SEV_RANK = {"watch": 1, "high": 2}


def _upsert_alert(cfg: Config, stats, *, entity_id: int, type_: str, severity: str,
                  window_start: str, window_end, observed, baseline, zscore,
                  cooldown_h: float) -> None:
    c = db.client()
    existing = (
        c.table("alerts").select("id, severity, window_start")
        .eq("entity_id", entity_id).eq("type", type_)
        .eq("window_start", window_start)
        .execute().data
    )
    if existing:
        # idempotent re-evaluation: escalate severity / refresh numbers only
        cur = existing[0]
        if _SEV_RANK[severity] > _SEV_RANK.get(cur["severity"], 0):
            c.table("alerts").update({
                "severity": severity, "observed": observed,
                "baseline": baseline, "zscore": zscore,
            }).eq("id", cur["id"]).execute()
            stats.incr("alerts_escalated")
        return

    # cross-window cooldown: an open/recent alert of the same type suppresses
    # new windows (escalation above is exempt)
    cutoff = db.iso(db.now_utc() - timedelta(hours=cooldown_h))
    recent = (
        c.table("alerts").select("id")
        .eq("entity_id", entity_id).eq("type", type_)
        .gte("triggered_at", cutoff)
        .limit(1).execute().data
    )
    if recent:
        stats.incr("alerts_cooldown_suppressed")
        return

    evidence = _build_evidence(cfg, entity_id, window_start)
    c.table("alerts").insert({
        "entity_id": entity_id, "type": type_, "severity": severity,
        "window_start": window_start, "window_end": window_end,
        "observed": observed, "baseline": baseline, "zscore": zscore,
        "evidence": evidence,
    }).execute()
    stats.incr("alerts_created")


def _build_evidence(cfg: Config, entity_id: int, window_start: str) -> dict:
    """Denormalize evidence at creation time so the alert stays self-contained
    after mention rows are purged (YT 30d body / 180d row retention).
    Summaries are derived one-liners, never verbatim YouTube text."""
    rows = (
        db.client()
        .table("mention_entities")
        .select("sentiment, label, aspects, "
                "mentions!inner(id, url, platform, published_at, title, kind)")
        .eq("entity_id", entity_id)
        .eq("relevant", True)
        .gte("mentions.published_at",
             db.iso(db.now_utc() - timedelta(hours=26)))
        .order("sentiment", desc=False)
        .limit(5)
        .execute().data
    )
    items = []
    for r in rows:
        m = r["mentions"]
        aspects = r.get("aspects") or []
        aspect_str = ", ".join(
            f"{a.get('name')}({a.get('score')})" for a in aspects if isinstance(a, dict)
        )
        items.append({
            "mention_id": m["id"],
            "url": m.get("url"),
            "platform": m.get("platform"),
            "kind": m.get("kind"),
            "published_at": m.get("published_at"),
            "sentiment": r.get("sentiment"),
            "label": r.get("label"),
            # derived description only (YT-policy-safe); reddit titles are fine
            "summary": (m.get("title") or "")[:120] if m.get("platform") == "reddit"
                       else f"{r.get('label')} comment, aspects: {aspect_str or 'n/a'}",
        })
    return {"window_start": window_start, "items": items}
