// Shell: auth gate (email OTP) → hash router → 60s polling refresh.
// MVP deliberately uses polling instead of Supabase Realtime (design §1):
// alerts are produced by a 20-min cron, so Realtime saves at most 60s while
// adding the three hardest-to-debug failure modes (publication/JWT/resub).
import "./style.css";
import { api, sb } from "./api";
import type { Alert, Entity } from "./api";
import { disposeCharts } from "./charts";
import {
  renderAlerts, renderEntity, renderManage, renderOverview, renderReports, renderSearch,
} from "./views";
import type { SearchState } from "./views";

const app = document.getElementById("app")!;
const POLL_MS = 60_000;

let pollTimer: number | undefined;
let gen = 0;  // refresh generation: stale in-flight renders bail after each await
const detailState = { range: "7d", platform: "", sourceId: "", order: "latest" };
const searchState: SearchState = { text: "", entityId: "", platform: "", label: "", range: "7d" };

// ---------------------------------------------------------------------------
// Auth gate
// ---------------------------------------------------------------------------

async function boot(): Promise<void> {
  const session = await api.session();
  if (!session) {
    renderLogin();
    return;
  }
  renderShell(session.user.email ?? "");
  await refresh();
  schedulePoll();
}

function renderLogin(): void {
  app.innerHTML = `
    <div class="login-wrap"><div class="login">
      <h1>mention<span style="color:var(--amber)">·</span>radar</h1>
      <p>單一操作者儀表板 — 輸入信箱取得一次性登入碼</p>
      <input id="email" type="email" placeholder="you@example.com" autocomplete="email" />
      <div class="row">
        <input id="code" type="text" placeholder="6 位驗證碼(寄出後填)" />
        <button id="send" class="primary">寄出</button>
        <button id="verify">登入</button>
      </div>
      <div class="msg" id="msg"></div>
    </div></div>`;
  const msg = document.getElementById("msg")!;
  const email = document.getElementById("email") as HTMLInputElement;
  const code = document.getElementById("code") as HTMLInputElement;
  document.getElementById("send")!.addEventListener("click", async () => {
    const { error } = await sb.auth.signInWithOtp({
      email: email.value.trim(),
      options: { shouldCreateUser: false },  // operator account is pre-created
    });
    msg.textContent = error ? `寄送失敗:${error.message}` : "已寄出,檢查信箱(含垃圾郵件)";
  });
  document.getElementById("verify")!.addEventListener("click", async () => {
    const { error } = await sb.auth.verifyOtp({
      email: email.value.trim(), token: code.value.trim(), type: "email",
    });
    if (error) { msg.textContent = `登入失敗:${error.message}`; return; }
    boot();
  });
}

// ---------------------------------------------------------------------------
// Shell + router
// ---------------------------------------------------------------------------

function renderShell(email: string): void {
  app.innerHTML = `
    <header class="top">
      <span class="brand">mention<span class="dot">·</span>radar</span>
      <nav class="tabs">
        <a href="#/" data-tab="overview">Overview</a>
        <a href="#/search" data-tab="search">搜尋</a>
        <a href="#/alerts" data-tab="alerts">Alerts</a>
        <a href="#/reports" data-tab="reports">Reports</a>
        <a href="#/manage" data-tab="manage">管理</a>
      </nav>
      <span class="userbox">${email}<button id="logout">登出</button></span>
    </header>
    <main id="view"></main>`;
  document.getElementById("logout")!.addEventListener("click", async () => {
    await sb.auth.signOut();
    location.hash = "#/";
    boot();
  });
}

function activeTab(): string {
  const h = location.hash;
  if (h.startsWith("#/alerts")) return "alerts";
  if (h.startsWith("#/reports")) return "reports";
  if (h.startsWith("#/manage")) return "manage";
  if (h.startsWith("#/search")) return "search";
  if (h.startsWith("#/entity/")) return "overview";
  return "overview";
}

async function refresh(): Promise<void> {
  const view = document.getElementById("view");
  if (!view) return;
  // generation guard: five trigger sources (poll, hashchange, ack, visibility,
  // filters) can overlap; a stale in-flight render must not overwrite a newer
  // one. Bail after every await if a newer refresh has started.
  const my = ++gen;
  const alive = () => my === gen;
  disposeCharts();  // tear down previous render's chart instances first
  document.querySelectorAll<HTMLAnchorElement>("nav.tabs a").forEach((a) =>
    a.classList.toggle("active", a.dataset.tab === activeTab()));
  try {
    const h = location.hash;
    if (h.startsWith("#/entity/")) {
      const slug = decodeURIComponent(h.slice("#/entity/".length));
      const [entities, alerts, sources] = await Promise.all([
        api.entities(), api.alerts(false), api.sources(),
      ]);
      if (!alive()) return;
      const ent = entities.find((e: Entity) => e.slug === slug);
      if (!ent) { view.innerHTML = `<div class="empty">找不到實體 ${slug}</div>`; return; }
      await renderEntity(view, ent, alerts, sources, detailState, alive);
      if (!alive()) return;
      wireDetailFilters(view);
    } else if (h.startsWith("#/alerts")) {
      const alerts = await api.alerts(false);
      if (!alive()) return;
      renderAlerts(view, alerts);
    } else if (h.startsWith("#/reports")) {
      await renderReports(view, alive);
    } else if (h.startsWith("#/manage")) {
      await renderManage(view, alive);
    } else if (h.startsWith("#/search")) {
      await renderSearch(view, searchState, alive);
    } else {
      const since = new Date(Date.now() - 8 * 86400e3).toISOString();
      const [entities, agg, alerts, quality] = await Promise.all([
        api.entities(), api.aggHourly(since), api.alerts(true) as Promise<Alert[]>, api.quality(),
      ]);
      if (!alive()) return;
      renderOverview(view, entities, agg, alerts, quality);
    }
  } catch (err) {
    if (!alive()) return;
    view.innerHTML = `<div class="empty">讀取失敗(${(err as { message?: string })?.message ?? "未知錯誤"})
      — 60 秒後自動重試</div>`;
  }
}

function wireDetailFilters(view: HTMLElement): void {
  view.querySelectorAll<HTMLElement>(".seg").forEach((seg) => {
    seg.querySelectorAll("button").forEach((b) =>
      b.addEventListener("click", () => {
        const key = seg.dataset.seg as keyof typeof detailState;
        (detailState as Record<string, string>)[key] = (b as HTMLButtonElement).dataset.v ?? "";
        if (key === "platform") detailState.sourceId = "";
        refresh();
      }));
  });
  const sel = view.querySelector<HTMLSelectElement>("select[data-seg=source]");
  sel?.addEventListener("change", () => { detailState.sourceId = sel.value; refresh(); });
}

// Manage/Search hold un-saved form state; auto-refresh (poll, tab refocus) must
// not blow it away. Explicit triggers (hashchange, post-mutation "refresh") still pass.
function interactiveView(): boolean {
  const h = location.hash;
  return h.startsWith("#/manage") || h.startsWith("#/search");
}

function schedulePoll(): void {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = window.setInterval(() => { if (!interactiveView()) refresh(); }, POLL_MS);
}

addEventListener("hashchange", () => refresh());
addEventListener("refresh", () => refresh());
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && !interactiveView()) refresh();  // laptop wake → refresh (api retries 401)
});

boot();
