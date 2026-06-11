# 日常操作 SOP(零程式改動)

所有監測目標與參數都在 Supabase 資料庫。編輯介面 = **Supabase Studio → Table Editor**
(以專案擁有者身份登入,天然繞過 RLS)。改完即生效,下一輪 cron(≤20 分鐘)自動拾取。

## 新增一個監測產品(實體)
1. `entities` → Insert row:
   - `slug`:小寫-連字號(如 `ally-x-2`),建立後不要改
   - `name`:顯示名稱
   - `side`:`ours` 或 `competitor`
2. `keywords` → 為該 entity 加 1–5 個關鍵字:
   - `match_type`:`phrase`(子字串,**中文一律用這個**)/ `word`(英文整詞)/ `regex`(進階)
   - 中文關鍵字注意:系統自動以子字串匹配(全形半形/大小寫不敏感)
3. (如需)`sources` 加新的 subreddit / YouTube 頻道(見下)
4. 完成。前 72 小時為 baseline 暖機期,不會觸發警報。

## 新增 subreddit / YouTube 頻道 / 關鍵字搜尋
`sources` → Insert row:
| platform | kind | source_key 範例 |
|---|---|---|
| reddit | subreddit | `SteamDeck`(不含 r/) |
| reddit | search | `"rog ally" OR "ally x"`(跨全 Reddit) |
| youtube | channel | `UCxxxxxxxxxxxxxxxxxxxxxx`(頻道 ID,非 @handle) |
| youtube | search | `rog ally review` |

`config` 留 `{}`(游標自動建立);停用改 `active=false`(不要刪,保留游標)。
> 頻道 ID 查法:頻道頁 → 檢視原始碼搜 `channelId`,或 `https://www.youtube.com/@handle/about` 的分享連結。

## 調整警報靈敏度
- 全域:`app_config` → `alert_defaults`(z、min_count、drop、cooldown_hours…)
- 單一實體:`entities.thresholds` 放覆寫,例:
  ```json
  {"volume": {"min_count": 5}, "sentiment": {"drop": 0.2}}
  ```
  (安靜的小眾產品把 `min_count` 調低;吵雜的大產品調高)

## 警報處理
儀表板 Alerts 頁:`ack`(已看到,處理中)→ `resolved`(已結案)。
證據(evidence)在警報建立當下固化,即使原文之後被清除仍可回看。

## 每週固定動作(5 分鐘)
1. 看週報(儀表板 Reports 頁,週一 09:07 自動產出)。
2. 看 Overview 底部「資料品質」:freshness lag、yt units、降級旗標;
   `retention` job 必須是綠的(它是 YouTube ToS 合規的執行者)。
3. healthchecks.io 若有未讀警告 → 看 GitHub Actions 失敗 log。

## 已知的「正常異常」
- `rss_soft_skips`:GitHub IP 被 Reddit 擋,當輪跳過 — 設計內行為,下輪自動補。
- 偶發 cron 延遲 5–30 分:GitHub 高峰抖動,游標設計保證不漏資料。
- Arctic Shift 凌晨偶發 5xx:freshness 探針連續 3 輪才會升級為切換 Apify。
