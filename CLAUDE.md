# CLAUDE.md — mention-radar

AI agent:這是專案層級指引,Claude Code 開啟此 repo 時會自動載入。

## 開工前先讀

1. **[docs/HANDOFF.md](docs/HANDOFF.md)** ← **第一份必讀**。目前狀態、待辦、環境備註、不可重複踩的研究結論。
2. [docs/DESIGN.md](docs/DESIGN.md) — 核准設計規格(架構、警報數學、ToS/保留規則、成本)。理解「為什麼」。
3. [docs/SETUP.md](docs/SETUP.md) / [docs/SOP.md](docs/SOP.md) — 部署步驟 / 日常操作。

**一句話**:系統 code-complete、已通過審查、**尚未部署**。接手 = 照 SETUP.md 接外部服務並調參,**不是重寫程式**。

## 這個專案是什麼

ROG(ASUS 電競)KOL 經理用的獨立社群情緒監測:近即時監測 Reddit + YouTube 上我方/競品產品情緒,
出警報 + 週/月報。單人 + AI 維運,全月成本目標 US$5–20。Python 管線(GitHub Actions)+ Supabase
免費層 + Gemini 免費層 + Cloudflare Pages 暗色儀表板。

## 改任何程式都要守的鐵則

1. **監測目標存 DB 不存 repo**(`entities`/`keywords`/`sources` 走 Supabase Studio;repo 種子是虛構的)。
2. **日誌只印計數與 ID**——絕不印 mention 內文/作者/payload/金鑰(`pipeline/db.py` 的 `sanitized_run`
   已統一處理;新增 log 時自己也要守)。
3. **YouTube 留言原文 ≤30 天**(ToS);報表 YT 引文一律 LLM 改寫不逐字;retention job 是合規執行者。
4. **Reddit 無官方 API**:Arctic Shift 主、`search.rss` soft-skip、Apify 封頂備援。**別再嘗試
   `reddit.com/*.json`(對腳本硬 403)。**
5. `app_config` 是 runtime 旋鈕(model id / RPM / 閾值 / 上限)——能調 config 就別改程式、別 hardcode。

## 動手前的慣例

- 改 cron 頻率前先問操作者的 GitHub 方案(private Free 只有 2,000 分/月,20 分輪詢會超;見 HANDOFF §待辦 2)。
- 改 schema 要同步 migration + 受影響的 view/RLS;改偵測數學要回看 DESIGN.md §5。
- 提交前:Python `py_compile`、儀表板 `npm --prefix dashboard run build`(含 `tsc`)。
- **不能 push 外部 GitHub**(安全機制擋,資料外流類)——push 由操作者自己跑。
