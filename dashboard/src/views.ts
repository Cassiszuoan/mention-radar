// View renderers. Each receives a container plus pre-fetched data.
import type { AggRow, Alert, Entity, MentionRow } from "./api";
import { api } from "./api";
import { mountChart, sparkOption, trendOption } from "./charts";

const esc = (s: unknown) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]!));

const fmtDelta = (d: number | null, suffix = "") => {
  if (d == null) return `<span class="flat">—</span>`;
  const cls = d > 0 ? "up" : d < 0 ? "down" : "flat";
  return `<span class="delta ${cls}">${d > 0 ? "+" : ""}${d}${suffix}</span>`;
};

const sentColor = (v: number | null) =>
  v == null ? "var(--dim)" : v < -0.2 ? "var(--coral)" : v > 0.2 ? "var(--green)" : "var(--ink)";

// ---------------------------------------------------------------------------
// shared: per-entity rollups from agg rows
// ---------------------------------------------------------------------------

export type EntityStats = {
  vol24: number; vol24Prev: number; sent24: number | null;
  spark: [string, number][];
};

export function rollup(rows: AggRow[], entityId: number, now: Date): EntityStats {
  const h24 = now.getTime() - 24 * 3600e3;
  const h48 = now.getTime() - 48 * 3600e3;
  let vol24 = 0, vol24Prev = 0, sum24 = 0, n24 = 0;
  const byBucket = new Map<string, { sum: number; n: number }>();
  for (const r of rows) {
    if (r.entity_id !== entityId) continue;
    const t = new Date(r.bucket).getTime();
    if (t >= h24) { vol24 += r.mention_n; sum24 += Number(r.sent_sum); n24 += r.analyzed_n; }
    else if (t >= h48) vol24Prev += r.mention_n;
    const day = r.bucket.slice(0, 10);
    const acc = byBucket.get(day) ?? { sum: 0, n: 0 };
    acc.sum += Number(r.sent_sum); acc.n += r.analyzed_n;
    byBucket.set(day, acc);
  }
  const spark: [string, number][] = [...byBucket.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([d, v]) => [d, v.n ? +(v.sum / v.n).toFixed(3) : 0]);
  return { vol24, vol24Prev, sent24: n24 ? +(sum24 / n24).toFixed(2) : null, spark };
}

// ---------------------------------------------------------------------------
// Overview
// ---------------------------------------------------------------------------

export function renderOverview(
  el: HTMLElement, entities: Entity[], agg: AggRow[], alerts: Alert[],
  quality: { job: string; status: string; stats: Record<string, unknown> }[],
): void {
  const now = new Date();
  const flagFor = (id: number) => {
    const open = alerts.filter((a) => a.entity_id === id && a.status === "open");
    if (open.some((a) => a.severity === "high")) return "high";
    if (open.length) return "watch";
    return null;
  };

  const card = (e: Entity) => {
    const s = rollup(agg, e.id, now);
    const flag = flagFor(e.id);
    return `<div class="card" data-slug="${esc(e.slug)}">
      ${flag ? `<span class="flag ${flag}">${flag === "high" ? "HIGH" : "WATCH"}</span>` : ""}
      <div class="name">${esc(e.name)}</div>
      <div class="nums">
        <div class="num" style="color:${sentColor(s.sent24)}">${s.sent24 ?? "—"}
          <small>24h 情緒</small></div>
        <div class="num">${s.vol24}<small>24h 聲量</small></div>
        <div class="num">${fmtDelta(s.vol24 - s.vol24Prev)}<small>vs 前 24h</small></div>
      </div>
      <div class="spark"></div>
    </div>`;
  };

  const ours = entities.filter((e) => e.side === "ours");
  const comp = entities.filter((e) => e.side === "competitor");

  el.innerHTML = `
    <div class="banner">${alerts.filter((a) => a.status === "open").map(alertStrip).join("")}</div>
    <h2 class="sect">我方 <em>ours</em></h2>
    <div class="cards">${ours.map(card).join("") || `<div class="empty">尚無實體 — 至 Supabase Studio 新增</div>`}</div>
    <h2 class="sect">競品 <em>competitors</em></h2>
    <div class="cards">${comp.map(card).join("") || `<div class="empty">尚無競品實體</div>`}</div>
    <h2 class="sect">資料品質 <em>pipeline</em></h2>
    <div class="quality">${qualityChips(quality)}</div>`;

  // sparklines + card navigation
  el.querySelectorAll<HTMLElement>(".card").forEach((c) => {
    const slug = c.dataset.slug!;
    const ent = entities.find((e) => e.slug === slug)!;
    const s = rollup(agg, ent.id, now);
    const sparkEl = c.querySelector<HTMLElement>(".spark")!;
    if (s.spark.length > 1) mountChart(sparkEl).setOption(sparkOption(s.spark));
    c.addEventListener("click", () => { location.hash = `#/entity/${slug}`; });
  });
}

function alertStrip(a: Alert): string {
  return `<div class="alert-strip ${a.severity}">
    <span class="sev">${a.severity.toUpperCase()}</span>
    <span><b>${esc(a.name)}</b> ${a.type === "volume_spike" ? "聲量暴衝" : "情緒驟降"}
      — 觀測 ${a.observed ?? "?"} / 基線 ${a.baseline ?? "?"}</span>
    <span class="meta">${esc(a.triggered_at?.slice(0, 16).replace("T", " "))} ·
      <a href="#/alerts" style="color:var(--cyan)">處理</a></span>
  </div>`;
}

function qualityChips(rows: { job: string; status: string; stats: Record<string, unknown> }[]): string {
  if (!rows.length) return `<span>尚無管線執行紀錄</span>`;
  return rows.map((r) => {
    const st = r.stats ?? {};
    const bad = r.status === "error" || st["yt_degraded"] || st["write_cap_hit"]
      || st["db_size_alert"] || st["apify_spend_alert"];
    const extras = [
      st["freshness_lag_h"] != null ? `lag ${st["freshness_lag_h"]}h` : "",
      st["yt_units"] != null ? `yt ${st["yt_units"]}u` : "",
      st["rss_last_status"] ? `rss ${st["rss_last_status"]}` : "",
    ].filter(Boolean).join(" · ");
    return `<span class="${bad ? "bad" : ""}"><b>${esc(r.job)}</b> ${r.status}${extras ? " · " + extras : ""}</span>`;
  }).join("");
}

// ---------------------------------------------------------------------------
// Entity detail
// ---------------------------------------------------------------------------

export async function renderEntity(
  el: HTMLElement, entity: Entity, alerts: Alert[],
  sources: { id: number; platform: string; kind: string; source_key: string }[],
  state: { range: string; platform: string; sourceId: string; order: string },
): Promise<void> {
  const days = { "24h": 1, "7d": 7, "30d": 30, "90d": 90 }[state.range] ?? 7;
  const since = new Date(Date.now() - days * 86400e3).toISOString();
  const agg = days <= 7 ? await api.aggHourly(since) : await api.aggDaily(since);

  const mine = agg.filter((r) =>
    r.entity_id === entity.id &&
    (!state.platform || r.platform === state.platform) &&
    (!state.sourceId || String(r.source_id) === state.sourceId));

  const byBucket = new Map<string, { sum: number; n: number; vol: number }>();
  for (const r of mine) {
    const acc = byBucket.get(r.bucket) ?? { sum: 0, n: 0, vol: 0 };
    acc.sum += Number(r.sent_sum); acc.n += r.analyzed_n; acc.vol += r.mention_n;
    byBucket.set(r.bucket, acc);
  }
  const buckets = [...byBucket.entries()].sort(([a], [b]) => a.localeCompare(b));
  const sentiment: [string, number][] = buckets.filter(([, v]) => v.n > 0)
    .map(([b, v]) => [b, +(v.sum / v.n).toFixed(3)]);
  const volume: [string, number][] = buckets.map(([b, v]) => [b, v.vol]);

  const wins = alerts
    .filter((a) => a.entity_id === entity.id)
    .map((a) => ({
      start: a.window_start,
      end: new Date(new Date(a.window_start).getTime() + 3600e3).toISOString(),
      high: a.severity === "high",
    }));

  const seg = (name: string, opts: string[], cur: string) =>
    `<div class="seg" data-seg="${name}">` +
    opts.map((o) => `<button class="${o === cur ? "on" : ""}" data-v="${esc(o)}">${esc(o || "全部")}</button>`).join("") +
    `</div>`;

  const srcOpts = sources
    .filter((s) => !state.platform || s.platform === state.platform)
    .map((s) => `<option value="${s.id}" ${String(s.id) === state.sourceId ? "selected" : ""}>
        ${esc(s.platform)}/${esc(s.source_key)}</option>`).join("");

  el.innerHTML = `
    <div class="filters">
      <a href="#/" style="color:var(--dim);font:500 12px var(--mono);text-decoration:none">← overview</a>
      <span style="font:600 16px var(--sans);color:var(--ink-strong)">${esc(entity.name)}</span>
      <span class="label">${entity.side === "ours" ? "我方" : "競品"}</span>
      <span style="flex:1"></span>
      <span class="label">range</span>${seg("range", ["24h", "7d", "30d", "90d"], state.range)}
      <span class="label">platform</span>${seg("platform", ["", "reddit", "youtube"], state.platform)}
      <span class="label">source</span>
      <select data-seg="source"><option value="">全部來源</option>${srcOpts}</select>
    </div>
    <div class="chart-lg" id="trend"></div>
    <h2 class="sect">mention 流
      <span style="float:right">${seg("order", ["latest", "negative"], state.order)}</span></h2>
    <div id="feed"><div class="empty">載入中…</div></div>`;

  if (sentiment.length || volume.length) {
    mountChart(el.querySelector<HTMLElement>("#trend")!)
      .setOption(trendOption(sentiment, volume, wins));
  } else {
    el.querySelector("#trend")!.innerHTML = `<div class="empty">此範圍尚無資料</div>`;
  }

  const feed = el.querySelector<HTMLElement>("#feed")!;
  const mentions = await api.mentions(
    entity.id, since, state.order as "latest" | "negative",
    state.platform || undefined,
    state.sourceId ? Number(state.sourceId) : undefined);
  feed.innerHTML = mentions.length
    ? mentions.map(mentionItem).join("")
    : `<div class="empty">此範圍無 mention</div>`;
}

function mentionItem(m: MentionRow): string {
  const label = m.label ? `<span class="badge ${m.label}">${m.label} ${m.sentiment ?? ""}</span>` : "";
  const text = m.body_purged_at
    ? `<div class="body purged">原文已依資料保留政策清除(metadata 保留)</div>`
    : `<div class="body">${esc((m.title ? m.title + " — " : "") + (m.body ?? "")).slice(0, 600)}</div>`;
  return `<div class="mention">
    <div class="head">
      ${label}
      <span>${esc(m.platform)} · ${esc(m.kind)}</span>
      <span>${esc(m.published_at.slice(0, 16).replace("T", " "))}</span>
      ${m.url ? `<a href="${esc(m.url)}" target="_blank" rel="noreferrer">原文 ↗</a>` : ""}
      <span style="margin-left:auto">${esc(m.lang ?? "")}</span>
    </div>${text}</div>`;
}

// ---------------------------------------------------------------------------
// Alerts
// ---------------------------------------------------------------------------

export function renderAlerts(el: HTMLElement, alerts: Alert[]): void {
  if (!alerts.length) {
    el.innerHTML = `<div class="empty">無警報紀錄</div>`;
    return;
  }
  el.innerHTML = `<h2 class="sect">警報 <em>alerts</em></h2>
    <table class="data"><thead><tr>
      <th>severity</th><th>entity</th><th>type</th><th>window</th>
      <th>observed / baseline</th><th>z</th><th>status</th><th></th>
    </tr></thead><tbody>${alerts.map((a) => `
      <tr data-id="${a.id}">
        <td><span class="badge ${a.severity === "high" ? "neg" : "neu"}">${a.severity}</span></td>
        <td><a href="#/entity/${esc(a.slug)}" style="color:var(--cyan)">${esc(a.name)}</a></td>
        <td>${a.type === "volume_spike" ? "聲量暴衝" : "情緒驟降"}</td>
        <td style="font-family:var(--mono);font-size:11px">${esc(a.window_start?.slice(0, 16).replace("T", " "))}</td>
        <td style="font-family:var(--mono)">${a.observed ?? "—"} / ${a.baseline ?? "—"}</td>
        <td style="font-family:var(--mono)">${a.zscore ?? "—"}</td>
        <td>${esc(a.status)}</td>
        <td>
          ${a.status === "open" ? `<button data-act="ack">ack</button>` : ""}
          ${a.status !== "resolved" ? `<button data-act="resolved">resolve</button>` : ""}
          <button data-act="evidence">證據</button>
        </td>
      </tr>
      <tr class="evidence-row" data-for="${a.id}" style="display:none"><td colspan="8">
        ${(a.evidence?.items ?? []).map((ev) => `
          <div class="mention"><div class="head">
            <span class="badge ${ev.label ?? "neu"}">${ev.label ?? "?"} ${ev.sentiment ?? ""}</span>
            <span>${esc(ev.platform)}</span><span>${esc(ev.published_at?.slice(0, 16))}</span>
            ${ev.url ? `<a href="${esc(ev.url)}" target="_blank" rel="noreferrer">原文 ↗</a>` : ""}
          </div><div class="body">${esc(ev.summary ?? "")}</div></div>`).join("")
          || `<div class="empty">無固化證據</div>`}
      </td></tr>`).join("")}
    </tbody></table>`;

  el.querySelectorAll<HTMLButtonElement>("button[data-act]").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      const tr = (e.target as HTMLElement).closest("tr")!;
      const id = Number(tr.dataset.id);
      const act = btn.dataset.act!;
      if (act === "evidence") {
        const row = el.querySelector<HTMLElement>(`.evidence-row[data-for="${id}"]`)!;
        row.style.display = row.style.display === "none" ? "" : "none";
        return;
      }
      await api.ackAlert(id, act as "ack" | "resolved");
      dispatchEvent(new CustomEvent("refresh"));
    });
  });
}

// ---------------------------------------------------------------------------
// Reports
// ---------------------------------------------------------------------------

export async function renderReports(el: HTMLElement): Promise<void> {
  const reports = await api.reports();
  if (!reports.length) {
    el.innerHTML = `<div class="empty">尚無報告(每週一 09:07 自動產出)</div>`;
    return;
  }
  el.innerHTML = `<h2 class="sect">報告 <em>reports</em></h2>
    <table class="data"><thead><tr><th>period</th><th>file</th><th></th></tr></thead>
    <tbody>${reports.map((r) => `
      <tr><td>${esc(r.period)}</td>
          <td style="font-family:var(--mono)">${esc(r.name)}</td>
          <td><button data-p="${esc(r.period)}" data-n="${esc(r.name)}">開啟 ↗</button></td></tr>`).join("")}
    </tbody></table>`;
  el.querySelectorAll<HTMLButtonElement>("button[data-p]").forEach((b) =>
    b.addEventListener("click", async () => {
      const url = await api.reportUrl(b.dataset.p!, b.dataset.n!);
      window.open(url, "_blank", "noreferrer");
    }));
}
