"""Weekly / monthly report generation.

Risk-first structure (operator's existing habit): ① alert digest ② per-entity
scorecards with WoW/MoM deltas ③ ours-vs-competitor side-by-side (independent
trends, NOT share-of-voice) ④ top negative/positive mentions ⑤ data-quality
notes.

YouTube quotes are ALWAYS LLM-paraphrased, never verbatim: reports live in
Storage forever, and embedding verbatim YT comment text would store
Non-Authorized Data past the 30-day policy line. Reddit quotes are fine.

Output: dark-theme HTML + Markdown → Storage bucket `reports` → dashboard.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta

import requests

from . import db
from .config import Config

GEMINI = "https://generativelanguage.googleapis.com/v1beta/models"


def run(cfg: Config, stats, period: str) -> None:
    assert period in ("weekly", "monthly")
    today = db.now_utc().date()
    if period == "weekly":
        end = today
        start = end - timedelta(days=7)
        prev_start = start - timedelta(days=7)
    else:
        end = today.replace(day=1)
        prev_end = end - timedelta(days=1)
        start = prev_end.replace(day=1)          # previous calendar month
        prev_start = (start - timedelta(days=1)).replace(day=1)

    scorecards = _scorecards(cfg, start, end, prev_start, start)
    alerts = _alerts(start, end)
    top_neg, top_pos = _top_mentions(cfg, start, end)
    quality = _quality_notes()

    narrative = _narrative(cfg, period, scorecards, alerts) or ""
    title = f"{'週報' if period == 'weekly' else '月報'} {start} → {end}"
    md = _render_md(title, narrative, scorecards, alerts, top_neg, top_pos, quality)
    html = _render_html(title, md)

    stamp = end.isoformat()
    for ext, payload, ctype in (("md", md.encode(), "text/markdown"),
                                ("html", html.encode(), "text/html")):
        db.client().storage.from_("reports").upload(
            f"{period}/{stamp}.{ext}", payload,
            {"content-type": ctype, "upsert": "true"},
        )
    stats["report"] = f"{period}/{stamp}"


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _sum_window(entity_id: int, start, end) -> dict:
    rows = (
        db.client().table("agg_daily")
        .select("mention_n, analyzed_n, pos_n, neg_n, sent_sum")
        .eq("entity_id", entity_id)
        .gte("bucket", start.isoformat()).lt("bucket", end.isoformat())
        .execute().data
    )
    n = sum(r["analyzed_n"] for r in rows)
    return {
        "mentions": sum(r["mention_n"] for r in rows),
        "analyzed": n,
        "neg": sum(r["neg_n"] for r in rows),
        "pos": sum(r["pos_n"] for r in rows),
        "avg": round(sum(float(r["sent_sum"]) for r in rows) / n, 3) if n else None,
    }


def _scorecards(cfg: Config, start, end, prev_start, prev_end) -> list[dict]:
    out = []
    for ent in cfg.entities.values():
        cur = _sum_window(ent["id"], start, end)
        prev = _sum_window(ent["id"], prev_start, prev_end)
        out.append({
            "name": ent["name"], "side": ent["side"],
            **cur,
            "avg_delta": (round(cur["avg"] - prev["avg"], 3)
                          if cur["avg"] is not None and prev["avg"] is not None else None),
            "volume_delta": cur["mentions"] - prev["mentions"],
        })
    out.sort(key=lambda s: (s["side"], -(s["neg"] or 0)))
    return out


def _alerts(start, end) -> list[dict]:
    return (
        db.client().table("alerts")
        .select("*, entities(name)")
        .gte("triggered_at", start.isoformat())
        .lt("triggered_at", end.isoformat())
        .order("severity", desc=False).order("triggered_at", desc=True)
        .execute().data
    )


def _top_mentions(cfg: Config, start, end) -> tuple[list[dict], list[dict]]:
    def base():  # query builders are mutable — build fresh each time
        return (
            db.client().table("mention_entities")
            .select("sentiment, label, entity_id, "
                    "mentions!inner(url, platform, title, body, published_at)")
            .eq("relevant", True).not_.is_("sentiment", "null")
            .gte("mentions.published_at", start.isoformat())
            .lt("mentions.published_at", end.isoformat())
        )

    neg = base().order("sentiment", desc=False).limit(5).execute().data
    pos = base().order("sentiment", desc=True).limit(3).execute().data

    def fmt(rows):
        out = []
        yt_to_paraphrase = []
        for r in rows:
            m = r["mentions"]
            ent = cfg.entities.get(r["entity_id"], {})
            item = {
                "entity": ent.get("name", "?"),
                "sentiment": r["sentiment"],
                "url": m.get("url"),
                "platform": m["platform"],
            }
            text = (m.get("title") or "") + " — " + (m.get("body") or "")
            if m["platform"] == "youtube":
                if m.get("body"):
                    yt_to_paraphrase.append((item, m["body"][:600]))
                    item["quote"] = None  # filled by paraphrase below
                else:
                    item["quote"] = "(原文已依 YouTube 政策清除;僅存情緒標記)"
            else:
                item["quote"] = text.strip()[:240]
            out.append(item)
        if yt_to_paraphrase:
            paras = _paraphrase(cfg, [t for _, t in yt_to_paraphrase])
            for (item, _), p in zip(yt_to_paraphrase, paras):
                item["quote"] = f"(改寫){p}"
        for item in out:
            if item["quote"] is None:
                item["quote"] = "(無法取得改寫)"
        return out

    return fmt(neg), fmt(pos)


def _quality_notes() -> list[str]:
    rows = db.client().table("pipeline_runs").select("job, status, stats") \
        .gte("started_at", db.iso(db.now_utc() - timedelta(days=7))) \
        .execute().data
    notes, fails = [], {}
    degraded = False
    for r in rows:
        if r["status"] == "error":
            fails[r["job"]] = fails.get(r["job"], 0) + 1
        s = r.get("stats") or {}
        degraded = degraded or s.get("yt_degraded") or s.get("write_cap_hit")
        if s.get("apify_spend_alert"):
            notes.append("Apify 備援支出超過警戒值")
        if s.get("db_size_alert"):
            notes.append("資料庫容量超過 350MB 警戒線")
    for job, n in fails.items():
        notes.append(f"{job} 本期失敗 {n} 次")
    if degraded:
        notes.append("本期曾觸發降級模式(配額/寫入上限),資料覆蓋率可能下降")
    return notes or ["無異常"]


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def _gemini_text(cfg: Config, prompt: str) -> str | None:
    try:
        resp = requests.post(
            f"{GEMINI}/{cfg.gemini.get('model', 'gemini-2.5-flash-lite')}:generateContent",
            params={"key": os.environ["GEMINI_API_KEY"]},
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"maxOutputTokens": 2048,
                                       "thinkingConfig": {"thinkingBudget": 0}}},
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:  # noqa: BLE001 — reports degrade gracefully without LLM
        return None


def _paraphrase(cfg: Config, texts: list[str]) -> list[str]:
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(texts))
    out = _gemini_text(cfg, "將每則留言改寫成一句中立轉述(保留情緒傾向與重點,"
                            "禁止逐字引用),回傳 JSON 陣列(字串,依編號順序):\n" + numbered)
    try:
        arr = json.loads(out.strip().removeprefix("```json").removesuffix("```"))
        if isinstance(arr, list) and len(arr) == len(texts):
            return [str(a)[:200] for a in arr]
    except (AttributeError, ValueError):
        pass
    return ["(改寫失敗)"] * len(texts)


def _narrative(cfg: Config, period: str, scorecards, alerts) -> str | None:
    compact = {
        "scorecards": scorecards,
        "alerts": [{"entity": (a.get("entities") or {}).get("name"),
                    "type": a["type"], "severity": a["severity"],
                    "status": a["status"]} for a in alerts],
    }
    return _gemini_text(cfg,
        "你是品牌健康監測分析師。以下是本期(" + period + ")各產品實體的情緒/聲量"
        "計分卡與警報。用繁體中文寫 150-250 字的管理層摘要:風險優先、點名實體、"
        "給出明確的下一步建議。不要逐項複述數字表。\n\n" +
        json.dumps(compact, ensure_ascii=False, default=str))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _render_md(title, narrative, scorecards, alerts, top_neg, top_pos, quality) -> str:
    L = [f"# {title}", "", narrative, "", "## ⚠ 風險警報"]
    if alerts:
        for a in alerts:
            name = (a.get("entities") or {}).get("name", "?")
            L.append(f"- **[{a['severity'].upper()}]** {name} · {a['type']}"
                     f" · {a['triggered_at'][:16]} · 狀態 {a['status']}")
    else:
        L.append("- 本期無警報")
    L += ["", "## 實體計分卡", "",
          "| 實體 | 陣營 | 聲量 | Δ | 平均情緒 | Δ | 負評 | 正評 |",
          "|---|---|---|---|---|---|---|---|"]
    for s in scorecards:
        L.append(f"| {s['name']} | {'我方' if s['side']=='ours' else '競品'} "
                 f"| {s['mentions']} | {s['volume_delta']:+d} "
                 f"| {s['avg'] if s['avg'] is not None else '—'} "
                 f"| {s['avg_delta'] if s['avg_delta'] is not None else '—'} "
                 f"| {s['neg']} | {s['pos']} |")
    L += ["", "## 最負面聲音"]
    for m in top_neg:
        L.append(f"- ({m['sentiment']}) **{m['entity']}** [{m['platform']}]({m['url']}):{m['quote']}")
    L += ["", "## 正面亮點"]
    for m in top_pos:
        L.append(f"- ({m['sentiment']}) **{m['entity']}** [{m['platform']}]({m['url']}):{m['quote']}")
    L += ["", "## 資料品質"]
    L += [f"- {q}" for q in quality]
    return "\n".join(x for x in L if x is not None)


_HTML_SHELL = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@500;600&family=IBM+Plex+Sans+TC:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{{--bg:#0d1017;--panel:#131721;--ink:#bfbdb6;--dim:#565b66;
--amber:#ffb454;--cyan:#59c2ff;--coral:#ff6b6b;--line:#1c212e}}
body{{background:var(--bg);color:var(--ink);font:15px/1.75 "IBM Plex Sans TC",sans-serif;
margin:0;padding:48px 24px}}
main{{max-width:860px;margin:0 auto}}
h1,h2{{font-family:"IBM Plex Mono",monospace;color:var(--amber);letter-spacing:.02em}}
h1{{font-size:22px;border-bottom:1px solid var(--line);padding-bottom:16px}}
h2{{font-size:15px;color:var(--cyan);margin-top:40px}}
table{{border-collapse:collapse;width:100%;font-size:13.5px}}
th,td{{border-bottom:1px solid var(--line);padding:7px 10px;text-align:left}}
th{{color:var(--dim);font-family:"IBM Plex Mono",monospace;font-weight:500}}
a{{color:var(--cyan);text-decoration:none}}
strong{{color:#e6e1cf}}
li{{margin:6px 0}}
</style></head><body><main>{body}</main></body></html>"""


def _render_html(title: str, md: str) -> str:
    # minimal md→html (headings, tables, lists, bold, links) — no extra deps
    import re
    lines, html, in_table, in_list = md.split("\n"), [], False, False

    def inline(s: str) -> str:
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2">\1</a>', s)
        return s

    for ln in lines:
        if ln.startswith("|"):
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if set("".join(cells)) <= {"-", " ", ":"}:
                continue
            tag = "th" if not in_table else "td"
            if not in_table:
                html.append("<table>")
                in_table = True
            html.append("<tr>" + "".join(f"<{tag}>{inline(c)}</{tag}>" for c in cells) + "</tr>")
            continue
        if in_table:
            html.append("</table>")
            in_table = False
        if ln.startswith("- "):
            if not in_list:
                html.append("<ul>")
                in_list = True
            html.append(f"<li>{inline(ln[2:])}</li>")
            continue
        if in_list:
            html.append("</ul>")
            in_list = False
        if ln.startswith("## "):
            html.append(f"<h2>{inline(ln[3:])}</h2>")
        elif ln.startswith("# "):
            html.append(f"<h1>{inline(ln[2:])}</h1>")
        elif ln.strip():
            html.append(f"<p>{inline(ln)}</p>")
    if in_table:
        html.append("</table>")
    if in_list:
        html.append("</ul>")
    return _HTML_SHELL.format(title=title, body="\n".join(html))
