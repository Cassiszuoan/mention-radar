# mention·radar

Near-real-time social sentiment monitor for consumer-hardware brand entities
across **Reddit** and **YouTube**. Single-operator, free-tier-first
(GitHub Actions + Supabase + Gemini + Cloudflare Pages, ≈ $0/month).

```
GitHub Actions (cron :07/:27/:47)            Supabase (free tier)
┌──────────────────────────────────┐         ┌──────────────────────┐
│ ingest → sentiment → agg+detect  │ ──────▶ │ mentions / agg / ... │
└──────────────────────────────────┘         │ alerts  (RLS locked) │
  sources: Arctic Shift, reddit RSS,         └─────────┬────────────┘
  YouTube Data API v3 + channel RSS                    │ 60s polling
  daily: retention/compliance · weekly: report   ┌─────▼─────┐
                                                 │ dashboard │  Cloudflare Pages
                                                 └───────────┘
```

* **Design doc**: the approved system design lives with the operator
  (architecture, alert math, retention/ToS rules, cost model). Code comments
  reference its review fixes.
* **Setup**: [docs/SETUP.md](docs/SETUP.md) — Supabase project, API keys,
  GitHub secrets, Cloudflare Pages.
* **Daily ops**: [docs/SOP.md](docs/SOP.md) — add/remove keywords, subreddits,
  channels (no code changes), tune alert thresholds, handle alerts.

## Repository layout

| Path | What |
|---|---|
| `supabase/migrations/` | schema + RLS + SQL functions (aggregation, detector math, retention helpers) |
| `pipeline/` | Python jobs: ingest (Reddit/YouTube/Apify-fallback), sentiment (Gemini), aggregate+detect, retention, reports |
| `.github/workflows/` | `pipeline` (20-min cycle), `retention` (daily), `report` (weekly/monthly) |
| `dashboard/` | Vite + TypeScript + ECharts SPA (dark data-terminal theme) |

## Non-negotiable operating rules

1. **Targets live in the database, never in this repo** (`entities`,
   `keywords`, `sources` via Supabase Studio). Repo seeds are fictional.
2. **Logs carry counts and ids only.** Workflow logs are world-readable;
   mention text, author handles, payloads and keys must never be printed.
3. **YouTube comment text is stored max 30 days** (API policy III.E.4.d);
   reports paraphrase YT quotes instead of embedding them. The daily retention
   job is load-bearing compliance — keep it green.
4. **Reddit ingestion is best-effort by design** (no official API): Arctic
   Shift primary, `search.rss` soft-skip, Apify fallback capped by maxItems.
