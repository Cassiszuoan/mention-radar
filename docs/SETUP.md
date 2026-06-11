# 一次性建置(操作者手動步驟)

> 全程約 40–60 分鐘。標 🔑 的步驟只有你能做(需要你的帳號)。

## 1. 🔑 Supabase 專案
1. [supabase.com](https://supabase.com) → New project(Free plan,region 選 `ap-northeast-1` 東京)。
2. SQL Editor → 貼上執行 `supabase/migrations/0001_init.sql`。
3. (選用)貼上 `supabase/seed.example.sql` 跑虛構測試資料;**真實目標之後照 SOP 用 Table Editor 輸入,不要寫進 repo**。
4. 記下:Project Settings → API
   - `Project URL` → `SUPABASE_URL`
   - `service_role` key → `SUPABASE_SERVICE_KEY`(**絕不可進前端/repo**)
   - `anon` key → 給儀表板用
5. Authentication → Providers → Email:開啟 **Email OTP**;Users → 手動建立你的 email 帳號(程式登入時 `shouldCreateUser:false`)。
6. (建議)Authentication → SMTP:接 Resend/Brevo 免費層(內建郵件 ~2 封/時且常進垃圾箱,危機當下會鎖住你)。

## 2. 🔑 YouTube Data API v3 金鑰
1. [console.cloud.google.com](https://console.cloud.google.com) → 新專案 → 啟用 **YouTube Data API v3**。
2. 建 API key → **API restrictions = YouTube Data API v3 only**。 → `YT_API_KEY`
3. 不需 OAuth(只讀公開資料)。配額免費 10,000 units/日,本系統用 3–8%。

## 3. 🔑 Gemini 金鑰
1. [aistudio.google.com](https://aistudio.google.com) → Get API key(免費層)→ `GEMINI_API_KEY`
2. 到 AI Studio 確認 `gemini-2.5-flash-lite` 的實際 RPM/RPD,若與預設(12 RPM)差異大,
   改 `app_config.gemini`(Table Editor)。
3. ⚠️ 免費層輸入會被 Google 用於改進產品 — 本系統只送公開社群留言,合規;
   勿在 prompt/config 混入內部資料。

## 4. 🔑 GitHub repo + Secrets
1. 在 `mention-radar/` 目錄:
   ```
   gh repo create mention-radar --public --source=. --remote=origin --push
   ```
   (公開 repo = Actions 分鐘無上限。repo 內無任何真實監測目標。)
2. Repo → Settings → Secrets and variables → Actions,新增:
   | Secret | 值 |
   |---|---|
   | `SUPABASE_URL` | 步驟 1 |
   | `SUPABASE_SERVICE_KEY` | 步驟 1 |
   | `YT_API_KEY` | 步驟 2 |
   | `GEMINI_API_KEY` | 步驟 3 |
   | `CONTACT_EMAIL` | 你的聯絡信箱(進 Arctic Shift User-Agent) |
   | `HEALTHCHECK_URL` | 步驟 6(選用) |
   | `APIFY_TOKEN` | 步驟 7(選用) |
3. Actions 頁啟用 workflows;手動 `Run workflow` 跑一次 `pipeline` 驗證全綠。

## 5. 🔑 Cloudflare Pages(儀表板)
1. `dashboard/.env.example` → 在 Pages 專案設定環境變數
   `VITE_SUPABASE_URL` / `VITE_SUPABASE_ANON_KEY`(anon key,非 service key)。
2. [dash.cloudflare.com](https://dash.cloudflare.com) → Workers & Pages → Pages →
   Connect to Git → 選 repo:
   - Build command: `npm --prefix dashboard install && npm --prefix dashboard run build`
   - Build output: `dashboard/dist`
3. 開啟 `*.pages.dev` 網址 → email OTP 登入。

## 6. 監控(選用,強烈建議)
[healthchecks.io](https://healthchecks.io) 免費帳號 → 建 check(period 30 分、grace 60 分)
→ ping URL 填入 `HEALTHCHECK_URL` secret。管線停止超過 ~90 分鐘你會收到 email。

## 7. Apify 備援(選用)
[apify.com](https://apify.com) 免費帳號($5 credit/月)→ token 填 `APIFY_TOKEN`。
⚠️ 首次啟用前,到 actor 頁(automation-lab/reddit-scraper)核對 input 欄位名稱
與 `pipeline/ingest_apify.py` 內的 payload 一致(actor schema 可能演進)。

## 8. 驗收清單(對應設計文件「驗證方式」)
- [ ] `pipeline` workflow 連跑兩輪綠燈;Supabase `pipeline_runs` 有 stats(yt_units < 1,000)
- [ ] mentions 有增量、無重複(`select platform, external_id, count(*) ... having count(*)>1` 應為空)
- [ ] 抽 30 則 `mention_entities` 人工對照情緒標籤(含繁中樣本)
- [ ] SQL 灌測試負面列 → 兩型警報觸發 → 儀表板橫幅 60 秒內出現 → ack → cooldown 不重開
- [ ] 無痕視窗(未登入)直接打 REST API 應回 0 列;登入後可讀、僅能改 alerts.status
- [ ] 手動跑 `retention` workflow → Storage `archives/` 出現歸檔、`pipeline_runs` 記 db_bytes
