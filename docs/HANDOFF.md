# HANDOFF — 給接手的 AI agent / 新電腦

> 換電腦/換 session 後,**先讀這份**。`~/.claude/` 下的設計稿與記憶不會跟著 git 走,所以
> 本專案所有必要 context 都已收進 repo 內的 `docs/`。你不需要原電腦的任何東西。

## 一句話狀態

**mention-radar 已 code-complete 並通過驗證,但尚未部署。** 接手要做的事 = 照
[SETUP.md](SETUP.md) 把外部服務接起來、跑起來、調參。**程式不需要重寫。**

## 這是什麼

ROG(ASUS 電競)KOL 經理用的**獨立**社群情緒監測系統:近即時監測 Reddit + YouTube 上
我方/競品產品的情緒,出警報 + 週/月報。單人 + AI agent 維運,全月成本目標 US$5–20。
完整設計與「為什麼」在 [DESIGN.md](DESIGN.md)(核准規格)。

## 先讀的順序

1. **本檔(HANDOFF.md)** — 你在哪、做什麼
2. **[DESIGN.md](DESIGN.md)** — 架構、警報數學、ToS/保留規則、成本模型、所有設計取捨
3. **[SETUP.md](SETUP.md)** — 一次性建置步驟(操作者手動 + 你協助)
4. **[SOP.md](SOP.md)** — 上線後日常操作(改監測目標等)
5. **`../README.md`** — repo 結構速查

## 目前進度

| 階段 | 狀態 |
|---|---|
| Phase 0 schema/RLS/seed/docs | ✅ 完成 |
| Phase 1 擷取(YouTube/Arctic Shift/RSS/Apify) | ✅ 程式完成 |
| Phase 2 情緒管線 + 冪等聚合 | ✅ 程式完成 |
| Phase 3a 警報偵測 + 儀表板 SPA | ✅ 程式完成 |
| Phase 3b 收權(OTP/RLS 收緊) | ⬜ 待部署時做(SETUP 步驟 1.5–1.6) |
| Phase 4 retention/報表/GHA workflows | ✅ 程式完成 |
| **部署**(Supabase/金鑰/Cloudflare) | ⬜ **待操作者手動,見 SETUP.md** |
| 程式碼對抗審查 | ✅ 24 條真 bug 全修(見 git log `f0ac2b3`) |

驗證已過:Python 全量 `py_compile`、`match.py` CJK+regex 單元測試、儀表板 `tsc + vite build`、
preview 登入閘無 runtime error。**尚未對真實 API/DB 跑過端到端**(需金鑰才能跑)。

## 接手後的待辦(依序)

1. **協助操作者完成 [SETUP.md](SETUP.md)**:Supabase 專案 + 跑 `supabase/migrations/0001_init.sql`、
   YouTube/Gemini 金鑰、GitHub Secrets、Cloudflare Pages。你能做的:產 migration、檢查 config、
   寫種子 SQL;你**不能**做的:開帳號、拿金鑰、按 OAuth 同意(這些是操作者的)。
2. **GitHub Actions 分鐘決策(未定)**:private repo 的 Free 方案只有 2,000 分/月,而 20 分輪詢
   ≈6,500 分/月,約第 9 天會停。若操作者非付費方案 → 把三個 workflow 的 cron 改 **90 分輪詢**
   (`*/90` 不合法,用 `7,37 */1 * * *` 之類達到 ~每 90 分;或 `7 */1 * * *` 每小時 ≈$1–3/月)。
   詳見 DESIGN.md §8。**動 cron 前先問操作者方案。**
3. **端到端冒煙 + 調參**:照 DESIGN.md「驗證方式」用真實 config 連跑 48h,首兩週是警報閾值調參期。
4. **首次啟用 Apify 前**:核對 actor input schema 與 `pipeline/ingest_apify.py` 的 payload(actor 會演進)。

## 不可重複踩 / 不要再研究的結論(2026-06 實測)

- **Reddit 官方 API 不可用**;`reddit.com/*.json` 對腳本**硬 403**(別再試);PullPush 2024-09 後已死。
- **Reddit 主路線 = Arctic Shift API**(`arctic-shift.photon-reddit.com`,免費、雲端 IP 可用)。
  輔 = `reddit.com/search.rss`(GHA Azure IP 看運氣,soft-skip);備援 = Apify(maxItems 按件封頂)。
- **YouTube** 用 stats-gate(commentCount 變化才抓留言),否則爆配額;留言原文受 ToS 限存 **30 天**。
- **情緒 = Gemini 2.5 Flash-Lite 免費層**;model/RPM/RPD 全在 `app_config`(免費層數字會變,別寫死)。
- 這些的完整理由與替代方案在 DESIGN.md §3/§4/§10。

## 鐵則(改任何程式都要守)

1. **監測目標存 DB 不存 repo**(`entities`/`keywords`/`sources`,走 Supabase Studio)。repo 種子是虛構的。
2. **日誌只印計數與 ID**,絕不印 mention 內文/作者/payload/金鑰(workflow log 即使 private 也是協作者可見;
   `pipeline/db.py` 的 `sanitized_run` 已統一處理例外路徑)。
3. **YouTube 留言原文 ≤30 天**;報表 YT 引文一律 LLM 改寫不逐字。retention job 是合規執行者,別讓它紅。
4. **Reddit 取數本質 best-effort**(無官方 API):Arctic Shift 主、RSS soft-skip、Apify 封頂備援。

## 已知的「正常異常」(別當 bug 修)

- `rss_soft_skips`:GHA IP 被 Reddit 擋,當輪跳過 → 下輪游標自動補。
- cron 偶發延遲 5–30 分:GitHub 高峰抖動,重疊游標設計保證不漏資料。
- Arctic Shift 凌晨偶發 5xx:freshness 探針連續 3 輪才升級切 Apify。

## 環境備註

- Windows + PowerShell;preview 用 launch.json 名 `mention-radar-dashboard`(本機 port 5180;
  此設定在 `~/.claude/launch.json`,不在 repo,新電腦需重設一條)。
- gh CLI 在原電腦登入為 `Cassiszuoan`(repo+workflow scope);新電腦需 `gh auth login`。
- AI agent 被安全機制擋,**不能 push 外部 GitHub**(資料外流類)——push 由操作者自己跑。
