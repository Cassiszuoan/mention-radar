"""Per-mention sentiment via Gemini (free tier, synchronous, structured JSON).

* Batches of `gemini.batch_size` (default 20) candidate pairs per call.
* responseSchema forces a JSON array; `ref` is the in-batch index (token-lean).
* Client-side rate limit + model id + RPM all live in app_config (free-tier
  numbers shift; nothing is hardcoded).
* relevant=false rows are KEPT (prevents re-analysis loops, tracks precision).
* Failures get model='failed'; the daily job retries them once with the
  fallback model while they are <7 days old (before the YT 30-day purge can
  silently drop them from aggregates forever).

ToS note: free-tier inputs may be used by Google to improve products. Only
public social text ever goes in — never internal data.
"""

from __future__ import annotations

import json
import os
import time
from datetime import timedelta

import requests

from . import db
from .config import Config

GEMINI = "https://generativelanguage.googleapis.com/v1beta/models"

RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "ref":        {"type": "INTEGER"},
            "relevant":   {"type": "BOOLEAN"},
            "label":      {"type": "STRING", "enum": ["pos", "neu", "neg"]},
            "sentiment":  {"type": "NUMBER"},
            "confidence": {"type": "NUMBER"},
            "lang":       {"type": "STRING"},
            "aspects": {
                "type": "ARRAY", "maxItems": 4,
                "items": {
                    "type": "OBJECT",
                    "properties": {"name": {"type": "STRING"},
                                   "score": {"type": "NUMBER"}},
                    "required": ["name", "score"],
                },
            },
        },
        "required": ["ref", "relevant"],
    },
}

PROMPT = """You score social-media texts about consumer hardware products.
For each numbered item, judge ONLY the author's stance toward the named target
product (not other products mentioned). Detect sarcasm and gaming slang.
Texts may be English, Traditional Chinese, or any other language (auto-detect;
set `lang` to the BCP-47 code; if neither English nor Chinese, lower your
confidence).

Return one object per item:
- ref: the item number
- relevant: false if the text is not actually about the target product
  (then omit the other fields)
- label: pos / neu / neg
- sentiment: -1.0 (hostile) .. 1.0 (enthusiastic)
- confidence: 0.0-1.0
- aspects: up to 4 {name, score} pairs for concrete product facets mentioned
  (e.g. price, software, battery, build). Omit when none are explicit.

Items:
"""


class RateLimiter:
    def __init__(self, rpm: int):
        self.interval = 60.0 / max(1, rpm)
        self.last = 0.0

    def wait(self) -> None:
        delta = time.monotonic() - self.last
        if delta < self.interval:
            time.sleep(self.interval - delta)
        self.last = time.monotonic()


def run(cfg: Config, stats, model_override: str | None = None,
        only_failed: bool = False) -> None:
    gcfg = cfg.gemini
    model = model_override or gcfg.get("model", "gemini-2.5-flash-lite")
    batch_size = int(gcfg.get("batch_size", 20))
    limiter = RateLimiter(int(gcfg.get("rpm", 12)))

    pending = _fetch_pending(cfg, only_failed=only_failed)
    stats["pending"] = len(pending)
    if not pending:
        return

    for i in range(0, len(pending), batch_size):
        batch = pending[i : i + batch_size]
        try:
            limiter.wait()
            results = _analyze_batch(batch, cfg, model, gcfg)
        except Exception:  # noqa: BLE001 — one bad batch must not kill the cycle
            stats.incr("batches_failed")
            _mark_failed(batch)
            continue
        _persist(batch, results, model, stats)
    stats["model"] = model


def _fetch_pending(cfg: Config, only_failed: bool, limit: int = 600) -> list[dict]:
    q = (
        db.client()
        .table("mention_entities")
        .select("mention_id, entity_id, "
                "mentions(title, body, platform, published_at), entities(name, slug)")
        .limit(limit)
    )
    if only_failed:
        # daily retry pass: previously-failed rows, fallback model, one shot
        q = q.eq("model", "failed").is_("analyzed_at", "null")
    else:
        # never re-pull failed rows here (that would retry every 20 minutes)
        q = q.is_("analyzed_at", "null").is_("model", "null")
    rows = q.execute().data
    cutoff = db.iso(db.now_utc() - timedelta(days=7))
    out = []
    for r in rows:
        m = r.get("mentions") or {}
        if only_failed and (m.get("published_at") or "") < cutoff:
            continue  # too old to retry; will be skipped permanently
        e = r.get("entities") or {}
        text = f"{m.get('title') or ''}\n{m.get('body') or ''}".strip()
        if not text:
            continue
        out.append({
            "mention_id": r["mention_id"],
            "entity_id": r["entity_id"],
            "target": e.get("name") or e.get("slug"),
            "text": text[:1500],
        })
    return out


def _analyze_batch(batch: list[dict], cfg: Config, model: str, gcfg: dict) -> dict[int, dict]:
    lines = [PROMPT]
    for i, item in enumerate(batch):
        lines.append(f'--- item {i} | target product: "{item["target"]}" ---')
        lines.append(item["text"])
    payload = {
        "contents": [{"parts": [{"text": "\n".join(lines)}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
            "maxOutputTokens": int(gcfg.get("max_output_tokens", 4096)),
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    resp = requests.post(
        f"{GEMINI}/{model}:generateContent",
        params={"key": os.environ["GEMINI_API_KEY"]},
        json=payload,
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    parsed = json.loads(text)
    return {int(o["ref"]): o for o in parsed if isinstance(o, dict) and "ref" in o}


def _persist(batch: list[dict], results: dict[int, dict], model: str, stats) -> None:
    now = db.iso(db.now_utc())
    for i, item in enumerate(batch):
        o = results.get(i)
        if o is None:
            _mark_failed([item])
            continue
        relevant = bool(o.get("relevant"))
        upd = {
            "relevant": relevant,
            "model": model,
            "analyzed_at": now,
        }
        if relevant:
            sent = o.get("sentiment")
            upd.update({
                "label": o.get("label") if o.get("label") in ("pos", "neu", "neg") else "neu",
                "sentiment": max(-1.0, min(1.0, float(sent))) if sent is not None else None,
                "confidence": o.get("confidence"),
                "aspects": o.get("aspects"),
            })
        db.client().table("mention_entities").update(upd) \
            .eq("mention_id", item["mention_id"]).eq("entity_id", item["entity_id"]).execute()
        lang = o.get("lang")
        if lang:
            db.client().table("mentions").update({"lang": str(lang)[:16]}) \
                .eq("id", item["mention_id"]).execute()
        stats.incr("analyzed")
        if not relevant:
            stats.incr("irrelevant")


def _mark_failed(batch: list[dict]) -> None:
    for item in batch:
        db.client().table("mention_entities").update({"model": "failed"}) \
            .eq("mention_id", item["mention_id"]).eq("entity_id", item["entity_id"]) \
            .is_("analyzed_at", "null").execute()
