// View renderers. Each receives a container plus pre-fetched data.
import type {
  AggRow, Alert, DiscoverItem, Entity, EntityFull, Keyword, MentionRow, SearchFilters, SourceFull,
} from "./api";
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
  alive: () => boolean = () => true,
): Promise<void> {
  const days = { "24h": 1, "7d": 7, "30d": 30, "90d": 90 }[state.range] ?? 7;
  const since = new Date(Date.now() - days * 86400e3).toISOString();
  const agg = days <= 7 ? await api.aggHourly(since) : await api.aggDaily(since);
  if (!alive()) return;

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
      // honor the real window (volume=1h, sentiment=24h); fall back by type
      end: a.window_end ?? new Date(new Date(a.window_start).getTime()
        + (a.type === "sentiment_drop" ? 24 : 1) * 3600e3).toISOString(),
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
      <span style="float:right">${seg("order", ["latest", "negative", "engaged"], state.order)}</span></h2>
    <div id="aspects"></div>
    <div id="feed"><div class="empty">載入中…</div></div>`;

  if (sentiment.length || volume.length) {
    mountChart(el.querySelector<HTMLElement>("#trend")!)
      .setOption(trendOption(sentiment, volume, wins));
  } else {
    el.querySelector("#trend")!.innerHTML = `<div class="empty">此範圍尚無資料</div>`;
  }

  const feed = el.querySelector<HTMLElement>("#feed")!;
  const mentions = await api.mentions(
    entity.id, since, state.order as "latest" | "negative" | "engaged",
    state.platform || undefined,
    state.sourceId ? Number(state.sourceId) : undefined);
  if (!alive()) return;
  feed.innerHTML = mentions.length
    ? mentions.map(mentionItem).join("")
    : `<div class="empty">此範圍無 mention</div>`;

  // Top negative aspects (design §6): aggregate the negative-scored aspect
  // facets across the fetched mentions.
  const aspectAcc = new Map<string, { sum: number; n: number }>();
  for (const m of mentions) {
    for (const a of m.aspects ?? []) {
      if (typeof a?.score !== "number") continue;
      const acc = aspectAcc.get(a.name) ?? { sum: 0, n: 0 };
      acc.sum += a.score; acc.n += 1;
      aspectAcc.set(a.name, acc);
    }
  }
  const topNeg = [...aspectAcc.entries()]
    .map(([name, v]) => ({ name, avg: v.sum / v.n, n: v.n }))
    .filter((a) => a.avg < 0)
    .sort((a, b) => a.avg - b.avg)
    .slice(0, 8);
  const aspectsEl = el.querySelector<HTMLElement>("#aspects")!;
  aspectsEl.innerHTML = topNeg.length
    ? `<div class="quality">${topNeg.map((a) =>
        `<span><b>${esc(a.name)}</b> <span class="down">${a.avg.toFixed(2)}</span> ·${a.n}</span>`
      ).join("")}</div>`
    : "";
}

function mentionItem(m: MentionRow): string {
  const label = m.label
    ? `<span class="badge ${esc(m.label)}">${esc(m.label)} ${m.sentiment ?? ""}</span>` : "";
  const text = m.body_purged_at
    ? `<div class="body purged">原文已依資料保留政策清除(metadata 保留)</div>`
    : `<div class="body">${esc(((m.title ? m.title + " — " : "") + (m.body ?? "")).slice(0, 600))}</div>`;
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
            <span class="badge ${esc(ev.label ?? "neu")}">${esc(ev.label ?? "?")} ${ev.sentiment ?? ""}</span>
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
      try {
        const rows = await api.ackAlert(id, act as "ack" | "resolved");
        if (!rows.length) throw new Error("沒有更新任何列(權限?)");
        dispatchEvent(new CustomEvent("refresh"));
      } catch (err) {
        btn.textContent = "失敗";
        btn.title = (err as { message?: string })?.message ?? "未知錯誤";
      }
    });
  });
}

// ---------------------------------------------------------------------------
// Reports
// ---------------------------------------------------------------------------

export async function renderReports(el: HTMLElement, alive: () => boolean = () => true): Promise<void> {
  const reports = await api.reports();
  if (!alive()) return;
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

// ---------------------------------------------------------------------------
// Manage (operator CRUD: entities / keywords / sources + per-entity thresholds)
// ---------------------------------------------------------------------------

function val(root: ParentNode, sel: string): string {
  return (root.querySelector(sel) as HTMLInputElement | null)?.value.trim() ?? "";
}

export async function renderManage(el: HTMLElement, alive: () => boolean = () => true): Promise<void> {
  const [ents, kws, srcs] = await Promise.all([
    api.manageEntities(), api.manageKeywords(), api.manageSources(),
  ]);
  if (!alive()) return;
  const kwBy = new Map<number, Keyword[]>();
  for (const k of kws) {
    const arr = kwBy.get(k.entity_id);
    if (arr) arr.push(k); else kwBy.set(k.entity_id, [k]);
  }

  const entRow = (e: EntityFull) => {
    const mc = e.thresholds?.volume?.min_count ?? "";
    const dr = e.thresholds?.sentiment?.drop ?? "";
    const ks = kwBy.get(e.id) ?? [];
    return `<div class="mrow" data-ent="${e.id}">
      <div class="mhead">
        <b>${esc(e.name)}</b>
        <span class="label">${e.side === "ours" ? "我方" : "競品"}</span>
        <code>${esc(e.slug)}</code>
        <label class="chk"><input type="checkbox" data-f="active" ${e.active ? "checked" : ""}/> 啟用</label>
        <span class="thr">聲量 min_count <input type="number" min="0" data-f="min_count" value="${mc}"/></span>
        <span class="thr">情緒 drop <input type="number" step="0.05" data-f="drop" value="${dr}"/></span>
        <span style="flex:1"></span>
        <button data-act="ent-save">儲存</button>
        <button class="danger" data-act="ent-del">刪除</button>
      </div>
      <div class="kwbox">
        ${ks.map((k) => `<span class="kw" data-kw="${k.id}">${esc(k.keyword)}<i>${esc(k.match_type)}</i>
          <button data-act="kw-del" title="刪除關鍵字">×</button></span>`).join("")}
        <span class="kw add">
          <input data-f="kw" placeholder="新關鍵字"/>
          <select data-f="mt"><option value="phrase">phrase</option><option value="word">word</option><option value="regex">regex</option></select>
          <button data-act="kw-add">＋ 加字</button>
        </span>
      </div>
    </div>`;
  };

  el.innerHTML = `
    <h2 class="sect">監測標的 <em>entities + keywords</em></h2>
    <div class="addbar">
      <input id="ne-slug" placeholder="slug(英數-連字號,如 rog-ally)"/>
      <input id="ne-name" placeholder="顯示名稱"/>
      <select id="ne-side"><option value="ours">我方</option><option value="competitor">競品</option></select>
      <button class="primary" data-act="ent-add">＋ 新增產品</button>
    </div>
    <div class="mlist">${ents.map(entRow).join("") || `<div class="empty">尚無標的</div>`}</div>

    <h2 class="sect">來源 <em>sources</em></h2>
    <div class="addbar">
      <select id="ns-plat"><option value="reddit">reddit</option><option value="youtube">youtube</option></select>
      <select id="ns-kind"><option value="subreddit">subreddit</option><option value="search">search</option><option value="channel">channel</option></select>
      <input id="ns-key" placeholder="source_key(subreddit 名 / 搜尋詞 / YouTube 頻道ID)" style="min-width:240px"/>
      <button class="primary" data-act="src-add">＋ 新增來源</button>
    </div>
    <table class="data"><thead><tr><th>platform</th><th>kind</th><th>source_key</th><th>啟用</th><th></th></tr></thead>
    <tbody>${srcs.map((s) => `<tr data-src="${s.id}">
      <td>${esc(s.platform)}</td><td>${esc(s.kind)}</td>
      <td style="font-family:var(--mono)">${esc(s.source_key)}</td>
      <td><input type="checkbox" data-f="active" ${s.active ? "checked" : ""}/></td>
      <td><button data-act="src-save">儲存</button> <button class="danger" data-act="src-del">刪除</button></td>
    </tr>`).join("") || `<tr><td colspan="5" class="empty">尚無來源</td></tr>`}</tbody></table>
    <p class="hint">改完下一輪 cron(≤20 分)自動套用。刪除產品會連同其關鍵字與留言關聯一併移除;停用來源(取消勾選後儲存)會保留游標、暫停抓取。</p>`;

  wireManage(el);
}

async function mutate(btn: HTMLButtonElement, fn: () => Promise<{ id: number }[]>): Promise<void> {
  const orig = btn.textContent;
  btn.disabled = true; btn.textContent = "…";
  try {
    const rows = await fn();
    if (!rows.length) throw new Error("沒有更新任何列(權限不足?)");
    dispatchEvent(new CustomEvent("refresh"));
  } catch (err) {
    btn.disabled = false; btn.textContent = orig ?? "失敗";
    const msg = (err as { message?: string })?.message ?? "未知錯誤";
    btn.title = msg;
    alert("操作失敗:" + msg);
  }
}

function wireManage(el: HTMLElement): void {
  el.querySelectorAll<HTMLButtonElement>("button[data-act]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const act = btn.dataset.act!;
      if (act === "ent-add") {
        const slug = val(el, "#ne-slug"), name = val(el, "#ne-name");
        const side = (el.querySelector("#ne-side") as HTMLSelectElement).value;
        if (!slug || !name) { alert("請填 slug 與顯示名稱"); return; }
        void mutate(btn, () => api.addEntity({ slug, name, side }));
        return;
      }
      if (act === "src-add") {
        const platform = (el.querySelector("#ns-plat") as HTMLSelectElement).value;
        const kind = (el.querySelector("#ns-kind") as HTMLSelectElement).value;
        const source_key = val(el, "#ns-key");
        if (!source_key) { alert("請填 source_key"); return; }
        void mutate(btn, () => api.addSource({ platform, kind, source_key }));
        return;
      }
      const mrow = btn.closest<HTMLElement>(".mrow");
      if (act === "ent-save" && mrow) {
        const id = Number(mrow.dataset.ent);
        const active = (mrow.querySelector("[data-f=active]") as HTMLInputElement).checked;
        const mc = val(mrow, "[data-f=min_count]"), dr = val(mrow, "[data-f=drop]");
        const thresholds: Record<string, unknown> = {};
        if (mc !== "") thresholds.volume = { min_count: Number(mc) };
        if (dr !== "") thresholds.sentiment = { drop: Number(dr) };
        void mutate(btn, () => api.updEntity(id, { active, thresholds }));
        return;
      }
      if (act === "ent-del" && mrow) {
        if (!confirm("確定刪除此產品?其關鍵字與留言關聯會一併移除。")) return;
        void mutate(btn, () => api.delEntity(Number(mrow.dataset.ent)));
        return;
      }
      if (act === "kw-add" && mrow) {
        const box = btn.closest<HTMLElement>(".kw.add")!;
        const keyword = val(box, "[data-f=kw]");
        const match_type = (box.querySelector("[data-f=mt]") as HTMLSelectElement).value;
        if (!keyword) { alert("請填關鍵字"); return; }
        void mutate(btn, () => api.addKeyword({ entity_id: Number(mrow.dataset.ent), keyword, match_type }));
        return;
      }
      if (act === "kw-del") {
        const chip = btn.closest<HTMLElement>(".kw[data-kw]")!;
        void mutate(btn, () => api.delKeyword(Number(chip.dataset.kw)));
        return;
      }
      const tr = btn.closest<HTMLElement>("tr[data-src]");
      if (act === "src-save" && tr) {
        const active = (tr.querySelector("[data-f=active]") as HTMLInputElement).checked;
        void mutate(btn, () => api.updSource(Number(tr.dataset.src), { active }));
        return;
      }
      if (act === "src-del" && tr) {
        if (!confirm("確定刪除此來源?")) return;
        void mutate(btn, () => api.delSource(Number(tr.dataset.src)));
        return;
      }
    });
  });
}

// ---------------------------------------------------------------------------
// Search (free-text / faceted mention search across all entities)
// ---------------------------------------------------------------------------

export type SearchState = {
  text: string; entityId: string; platform: string; label: string; range: string;
};

export async function renderSearch(
  el: HTMLElement, state: SearchState, alive: () => boolean = () => true,
): Promise<void> {
  const ents = await api.manageEntities();
  if (!alive()) return;
  const opt = (v: string, label: string, cur: string) =>
    `<option value="${esc(v)}" ${v === cur ? "selected" : ""}>${esc(label)}</option>`;
  el.innerHTML = `
    <div class="filters">
      <input id="q" placeholder="搜尋留言內文 / 標題…" value="${esc(state.text)}" style="flex:1;min-width:180px"/>
      <select id="f-ent"><option value="">全部標的</option>${
        ents.map((e) => opt(String(e.id), e.name, state.entityId)).join("")}</select>
      <select id="f-plat">${["", "reddit", "youtube"].map((p) => opt(p, p || "全平台", state.platform)).join("")}</select>
      <select id="f-label">${([["", "全情緒"], ["pos", "正面"], ["neu", "中性"], ["neg", "負面"]] as [string, string][])
        .map(([v, l]) => opt(v, l, state.label)).join("")}</select>
      <select id="f-range">${([["7d", "近 7 天"], ["30d", "近 30 天"], ["90d", "近 90 天"], ["all", "全部"]] as [string, string][])
        .map(([v, l]) => opt(v, l, state.range)).join("")}</select>
      <button class="primary" id="go">搜尋</button>
    </div>
    <div id="results"><div class="empty">設定條件後按搜尋</div></div>`;

  const results = el.querySelector<HTMLElement>("#results")!;
  const nameById = new Map(ents.map((e) => [e.id, e.name]));
  const run = async () => {
    state.text = val(el, "#q");
    state.entityId = (el.querySelector("#f-ent") as HTMLSelectElement).value;
    state.platform = (el.querySelector("#f-plat") as HTMLSelectElement).value;
    state.label = (el.querySelector("#f-label") as HTMLSelectElement).value;
    state.range = (el.querySelector("#f-range") as HTMLSelectElement).value;
    const days = ({ "7d": 7, "30d": 30, "90d": 90 } as Record<string, number>)[state.range];
    const f: SearchFilters = {
      text: state.text || undefined,
      entityId: state.entityId ? Number(state.entityId) : undefined,
      platform: state.platform || undefined,
      label: state.label || undefined,
      sinceIso: days ? new Date(Date.now() - days * 86400e3).toISOString() : undefined,
    };
    results.innerHTML = `<div class="empty">搜尋中…</div>`;
    try {
      const rows = await api.searchMentions(f);
      results.innerHTML = rows.length
        ? `<div class="hint">${rows.length} 則(上限 100,依時間新→舊)</div>` +
          rows.map((m) => searchItem(m, nameById.get(m.entity_id) ?? m.name ?? "")).join("")
        : `<div class="empty">查無符合條件的留言</div>`;
    } catch (err) {
      results.innerHTML = `<div class="empty">搜尋失敗(${esc((err as { message?: string })?.message ?? "未知")})</div>`;
    }
  };
  el.querySelector<HTMLButtonElement>("#go")!.addEventListener("click", run);
  el.querySelector<HTMLInputElement>("#q")!.addEventListener("keydown", (e) => {
    if (e.key === "Enter") run();
  });
  if (state.text || state.entityId || state.platform || state.label) run();
}

function searchItem(m: MentionRow, entName: string): string {
  const label = m.label
    ? `<span class="badge ${esc(m.label)}">${esc(m.label)} ${m.sentiment ?? ""}</span>` : "";
  const text = m.body_purged_at
    ? `<div class="body purged">原文已依資料保留政策清除</div>`
    : `<div class="body">${esc(((m.title ? m.title + " — " : "") + (m.body ?? "")).slice(0, 600))}</div>`;
  return `<div class="mention">
    <div class="head">
      ${label}
      <a href="#/entity/${esc(m.slug)}" style="color:var(--amber)">${esc(entName)}</a>
      <span>${esc(m.platform)} · ${esc(m.kind)}</span>
      <span>${esc(m.published_at?.slice(0, 16).replace("T", " "))}</span>
      ${m.url ? `<a href="${esc(m.url)}" target="_blank" rel="noreferrer">原文 ↗</a>` : ""}
    </div>${text}</div>`;
}

// ---------------------------------------------------------------------------
// Discover (live ad-hoc search of Reddit/YouTube via the Worker /api/discover)
// ---------------------------------------------------------------------------

export type DiscoverState = { q: string; subreddits: string; platform: string };

export async function renderDiscover(
  el: HTMLElement, state: DiscoverState, _alive: () => boolean = () => true,
): Promise<void> {
  el.innerHTML = `
    <div class="filters">
      <input id="dq" placeholder="即時搜尋任何詞(直接打 Reddit / YouTube)…" value="${esc(state.q)}" style="flex:1;min-width:200px"/>
      <select id="d-plat">
        <option value="both">Reddit + YouTube</option>
        <option value="reddit">只 Reddit</option>
        <option value="youtube">只 YouTube</option>
      </select>
      <button class="primary" id="dgo">探索</button>
    </div>
    <div class="filters" style="padding-top:0">
      <span class="label">subreddits</span>
      <input id="dsubs" placeholder="逗號分隔,Reddit 全文搜尋需指定版面" value="${esc(state.subreddits)}" style="flex:1;min-width:240px"/>
    </div>
    <p class="hint">即時打外部 API、不寫入資料庫、無情緒分析。Reddit 在你指定的 subreddit 內搜;YouTube 為全站搜尋(需後端已設 YT_API_KEY)。</p>
    <div id="dresults"><div class="empty">輸入關鍵字後按探索</div></div>`;

  (el.querySelector("#d-plat") as HTMLSelectElement).value = state.platform || "both";
  const results = el.querySelector<HTMLElement>("#dresults")!;
  const run = async () => {
    state.q = val(el, "#dq");
    state.subreddits = val(el, "#dsubs");
    state.platform = (el.querySelector("#d-plat") as HTMLSelectElement).value;
    if (!state.q) { results.innerHTML = `<div class="empty">請輸入搜尋字</div>`; return; }
    results.innerHTML = `<div class="empty">即時搜尋中…(可能要幾秒)</div>`;
    try {
      const r = await api.discover({ q: state.q, platform: state.platform, subreddits: state.subreddits, limit: 10 });
      const items = [...(r.reddit ?? []), ...(r.youtube ?? [])];
      const notes = r.notes?.length ? `<div class="hint">${r.notes.map(esc).join(" · ")}</div>` : "";
      if (!items.length) { results.innerHTML = notes + `<div class="empty">查無即時結果</div>`; return; }
      const actions = `<div class="dactions">
        <span class="label">加入監測</span>
        <input id="pm-name" value="${esc(state.q)}" title="監測標的名稱" style="width:150px"/>
        <select id="pm-side"><option value="competitor">競品</option><option value="ours">我方</option></select>
        <button id="pm-go">＋ 監測此主題</button>
        <span style="flex:1"></span>
        <button id="snt-go">⚡ 跑情緒分析</button>
        <span id="dmsg" class="hint" style="margin:0"></span>
      </div>`;
      const rBlock = r.reddit?.length
        ? `<h2 class="sect">Reddit <em>${r.reddit.length}</em></h2>`
          + r.reddit.map((m, i) => discoverItem(m, i)).join("") : "";
      const yBlock = r.youtube?.length
        ? `<h2 class="sect">YouTube <em>${r.youtube.length}</em></h2>`
          + r.youtube.map((m, i) => discoverItem(m, (r.reddit?.length ?? 0) + i)).join("") : "";
      results.innerHTML = notes + actions + rBlock + yBlock;
      wireDiscoverActions(el, state, items);
    } catch (err) {
      results.innerHTML = `<div class="empty">探索失敗(${esc((err as { message?: string })?.message ?? "未知")})</div>`;
    }
  };
  el.querySelector<HTMLButtonElement>("#dgo")!.addEventListener("click", run);
  el.querySelector<HTMLInputElement>("#dq")!.addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });
  if (state.q) run();
}

function discoverItem(m: DiscoverItem, idx: number): string {
  const when = typeof m.created === "number"
    ? new Date(m.created * 1000).toISOString().slice(0, 16).replace("T", " ")
    : (typeof m.created === "string" ? m.created.slice(0, 16).replace("T", " ") : "");
  const src = m.platform === "reddit" ? `r/${esc(m.subreddit ?? "")}` : esc(m.channel ?? "youtube");
  const body = ((m.title ? m.title + " — " : "") + (m.body ?? "")).slice(0, 500);
  return `<div class="mention">
    <div class="head">
      <span class="badge sent" data-sent-idx="${idx}" style="display:none"></span>
      <span class="badge neu">${esc(m.platform)}·${esc(m.kind)}</span>
      <span>${src}</span>
      <span>${esc(when)}</span>
      ${m.url ? `<a href="${esc(m.url)}" target="_blank" rel="noreferrer">原文 ↗</a>` : ""}
    </div><div class="body">${esc(body)}</div></div>`;
}

const dslug = (s: string) =>
  s.toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60);

function wireDiscoverActions(el: HTMLElement, state: DiscoverState, items: DiscoverItem[]): void {
  const msg = el.querySelector<HTMLElement>("#dmsg")!;

  el.querySelector<HTMLButtonElement>("#pm-go")!.addEventListener("click", async (ev) => {
    const btn = ev.currentTarget as HTMLButtonElement;
    const name = (el.querySelector("#pm-name") as HTMLInputElement).value.trim();
    const side = (el.querySelector("#pm-side") as HTMLSelectElement).value;
    if (!name) { msg.textContent = "請填名稱"; return; }
    const slug = dslug(name) || ("ent-" + Date.now());
    btn.disabled = true; msg.textContent = "加入中…";
    try {
      let entId: number | undefined;
      try {
        const created = await api.addEntity({ slug, name, side });
        entId = created[0]?.id;
      } catch {
        const ex = await api.entityBySlug(slug);  // already exists → reuse
        entId = ex[0]?.id;
      }
      if (!entId) throw new Error("無法建立/找到標的(RLS 已套用?)");
      await api.addKeyword({ entity_id: entId, keyword: name, match_type: "phrase" });
      const subs = state.subreddits.split(",").map((s) => s.trim().replace(/^r\//i, "")).filter(Boolean);
      let added = 0;
      for (const sub of subs) {
        try { await api.ensureSource({ platform: "reddit", kind: "subreddit", source_key: sub }); added++; } catch { /* dup/err */ }
      }
      msg.textContent = `✓ 已監測「${name}」+ 確保 ${added} 來源,下輪 cron(≤20分)開始收集`;
    } catch (err) {
      msg.textContent = "失敗:" + ((err as { message?: string })?.message ?? "未知(RLS 未套用?)");
    } finally {
      btn.disabled = false;
    }
  });

  el.querySelector<HTMLButtonElement>("#snt-go")!.addEventListener("click", async (ev) => {
    const btn = ev.currentTarget as HTMLButtonElement;
    btn.disabled = true; msg.textContent = "分析中…";
    try {
      const texts = items.map((m) => ((m.title ? m.title + " " : "") + (m.body ?? "")).slice(0, 500));
      const res = await api.scoreSentiment(texts);
      if (!res.scores) { msg.textContent = res.note ?? "情緒分析未啟用"; return; }
      for (const s of res.scores) {
        const b = el.querySelector<HTMLElement>(`[data-sent-idx="${s.i}"]`);
        if (!b) continue;
        const lab = s.label === "pos" || s.label === "neg" ? s.label : "neu";
        b.textContent = `${lab} ${s.score}`;
        b.className = `badge ${lab} sent`;
        b.style.display = "";
      }
      msg.textContent = `✓ 已分析 ${res.scores.length} 則`;
    } catch (err) {
      msg.textContent = "失敗:" + ((err as { message?: string })?.message ?? "未知");
    } finally {
      btn.disabled = false;
    }
  });
}
