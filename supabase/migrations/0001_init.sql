-- mention-radar: initial schema
-- Conventions:
--   * All writes go through service_role (GitHub Actions secrets). RLS is deny-by-default.
--   * anon has ZERO grants/policies. authenticated (single operator) is read-only,
--     except alerts.status (column-level grant).
--   * Dashboard reads go through security_invoker views only.

-- ---------------------------------------------------------------------------
-- Config tables (targets are DB-driven; the public repo never contains them)
-- ---------------------------------------------------------------------------

create table entities (
  id          bigint generated always as identity primary key,
  slug        text unique not null,
  name        text not null,
  side        text not null check (side in ('ours','competitor')),
  active      boolean not null default true,
  thresholds  jsonb not null default '{}',
  created_at  timestamptz not null default now()
);

create table keywords (
  id         bigint generated always as identity primary key,
  entity_id  bigint not null references entities(id) on delete cascade,
  keyword    text not null,
  -- 'word' (\b boundaries) only works for whitespace-delimited languages.
  -- CJK keywords are always matched as plain substrings regardless of match_type (pipeline/match.py).
  match_type text not null default 'phrase' check (match_type in ('phrase','word','regex')),
  lang       text,
  active     boolean not null default true
);

create table sources (
  id         bigint generated always as identity primary key,
  platform   text not null check (platform in ('reddit','youtube')),
  kind       text not null check (kind in ('subreddit','channel','search')),
  source_key text not null,
  config     jsonb not null default '{}',  -- cursors, freshness, active video state
  active     boolean not null default true,
  unique (platform, kind, source_key)
);

create table app_config (
  key   text primary key,
  value jsonb not null
);

-- ---------------------------------------------------------------------------
-- Data tables
-- ---------------------------------------------------------------------------

create table mentions (
  id            bigint generated always as identity primary key,
  platform      text not null check (platform in ('reddit','youtube')),
  source_id     bigint references sources(id) on delete set null,
  external_id   text not null,
  kind          text not null check (kind in ('post','comment','video')),
  parent_external_id text,
  url           text,
  author_hash   bytea,            -- sha256(platform + author); raw handle is never stored
  title         text,
  body          text,             -- retention-managed (YouTube 30d / Reddit 90d)
  lang          text,
  published_at  timestamptz not null,
  fetched_at    timestamptz not null default now(),
  metrics       jsonb not null default '{}',
  body_purged_at timestamptz,
  unique (platform, external_id)
);
create index mentions_published_idx on mentions (published_at);
create index mentions_platform_fetched_idx on mentions (platform, fetched_at);
-- Deliberately NO GIN full-text index on body (free-tier disk budget).

create table mention_entities (
  mention_id  bigint not null references mentions(id) on delete cascade,
  entity_id   bigint not null references entities(id) on delete cascade,
  relevant    boolean,            -- null = pending; false rows are KEPT (prevents re-analysis loops)
  sentiment   numeric(4,3) check (sentiment between -1 and 1),
  label       text check (label in ('pos','neu','neg')),
  confidence  numeric(3,2),
  aspects     jsonb,
  model       text,
  analyzed_at timestamptz,
  primary key (mention_id, entity_id)
);
create index mention_entities_pending_idx on mention_entities (analyzed_at) where analyzed_at is null;
create index mention_entities_entity_idx on mention_entities (entity_id);

create table agg_hourly (
  entity_id   bigint not null references entities(id) on delete cascade,
  platform    text not null,
  source_id   bigint not null default 0,  -- coalesce(mentions.source_id, 0); no FK on purpose
  bucket      timestamptz not null,
  mention_n   int not null default 0,     -- candidate pairs not judged irrelevant
  analyzed_n  int not null default 0,     -- relevant AND analyzed
  pos_n int not null default 0,
  neu_n int not null default 0,
  neg_n int not null default 0,
  sent_sum    numeric not null default 0, -- avg = sent_sum / nullif(analyzed_n,0)
  primary key (entity_id, platform, source_id, bucket)
);

create table agg_daily (like agg_hourly including all);
-- LIKE ... INCLUDING ALL copies columns/PK/checks but NOT foreign keys; add it
-- back so deleting an entity cascades (agg_daily is permanent — orphans would
-- accumulate against the 500MB budget and be invisible behind v_agg_daily).
alter table agg_daily
  add constraint agg_daily_entity_fk foreign key (entity_id)
  references entities(id) on delete cascade;

create table alerts (
  id            bigint generated always as identity primary key,
  entity_id     bigint not null references entities(id) on delete cascade,
  type          text not null check (type in ('sentiment_drop','volume_spike')),
  severity      text not null check (severity in ('watch','high')),
  triggered_at  timestamptz not null default now(),
  window_start  timestamptz not null,
  window_end    timestamptz,
  observed      numeric,
  baseline      numeric,
  zscore        numeric,
  -- evidence is denormalized at creation time: per item {url, platform, published_at,
  -- sentiment, label, aspects, summary (LLM paraphrase, never verbatim YT text), archive_path}
  evidence      jsonb,
  status        text not null default 'open' check (status in ('open','ack','resolved')),
  notified_at   timestamptz,      -- extension point for future Slack/Email notifier
  unique (entity_id, type, window_start)  -- detector idempotency: re-evaluations upsert, never duplicate
);
create index alerts_open_idx on alerts (status) where status = 'open';

create table pipeline_runs (
  id          bigint generated always as identity primary key,
  job         text not null,
  started_at  timestamptz not null default now(),
  finished_at timestamptz,
  status      text,
  stats       jsonb not null default '{}'
);
create index pipeline_runs_job_idx on pipeline_runs (job, started_at desc);

-- ---------------------------------------------------------------------------
-- App config defaults (tunable at runtime without redeploy)
-- ---------------------------------------------------------------------------

insert into app_config (key, value) values
  ('alert_defaults', '{
     "volume":    {"z": 3.0, "high_z": 5.0, "min_count": 10, "high_min_count": 20,
                   "baseline_hours": 168, "eval_trailing_buckets": 3},
     "sentiment": {"drop": 0.25, "high_drop": 0.40, "min_mentions": 15,
                   "min_analyzed_ratio": 0.8, "baseline_days": 7, "window_hours": 24},
     "cooldown_hours": 12,
     "new_entity_warmup_hours": 72
   }'),
  ('gemini', '{
     "model": "gemini-2.5-flash-lite",
     "fallback_model": "gemini-3.1-flash-lite",
     "rpm": 12, "rpd": 1000,
     "batch_size": 20, "max_output_tokens": 4096
   }'),
  ('ingest', '{
     "daily_write_cap": 5000,
     "reddit_post_margin_sec": 10800,
     "reddit_comment_margin_sec": 5400,
     "freshness_fail_hours": 6, "freshness_fail_cycles": 3,
     "yt_daily_unit_budget": 5000,
     "yt_active_video_days": 14, "yt_every_cycle_hours": 72
   }'),
  ('apify', '{
     "enabled_as_fallback": true,
     "actor": "automation-lab~reddit-scraper",
     "max_items_per_run": 500, "max_runs_per_day": 1,
     "monthly_spend_alert_usd": 4
   }');

-- ---------------------------------------------------------------------------
-- Aggregation: idempotent trailing recompute (NOT incremental).
-- Late data has three paths in (source indexing lag, sentiment backlog, metric
-- refresh); recomputing a trailing window self-heals all of them.
-- ---------------------------------------------------------------------------

create or replace function fn_recompute_agg(p_hours int default 72, p_days int default 7)
returns void
language plpgsql
security definer set search_path = public set timezone = 'UTC'
as $$
declare
  h_from timestamptz := date_trunc('hour', now()) - make_interval(hours => p_hours);
  d_from timestamptz := date_trunc('day',  now()) - make_interval(days  => p_days);
begin
  delete from agg_hourly where bucket >= h_from;
  insert into agg_hourly (entity_id, platform, source_id, bucket,
                          mention_n, analyzed_n, pos_n, neu_n, neg_n, sent_sum)
  select me.entity_id, m.platform, coalesce(m.source_id, 0),
         date_trunc('hour', m.published_at),
         count(*) filter (where me.relevant is distinct from false),
         count(*) filter (where me.relevant and me.analyzed_at is not null),
         count(*) filter (where me.relevant and me.label = 'pos'),
         count(*) filter (where me.relevant and me.label = 'neu'),
         count(*) filter (where me.relevant and me.label = 'neg'),
         coalesce(sum(me.sentiment) filter (where me.relevant), 0)
  from mention_entities me
  join mentions m on m.id = me.mention_id
  where m.published_at >= h_from
  group by 1, 2, 3, 4;

  delete from agg_daily where bucket >= d_from;
  insert into agg_daily (entity_id, platform, source_id, bucket,
                         mention_n, analyzed_n, pos_n, neu_n, neg_n, sent_sum)
  select me.entity_id, m.platform, coalesce(m.source_id, 0),
         date_trunc('day', m.published_at),
         count(*) filter (where me.relevant is distinct from false),
         count(*) filter (where me.relevant and me.analyzed_at is not null),
         count(*) filter (where me.relevant and me.label = 'pos'),
         count(*) filter (where me.relevant and me.label = 'neu'),
         count(*) filter (where me.relevant and me.label = 'neg'),
         coalesce(sum(me.sentiment) filter (where me.relevant), 0)
  from mention_entities me
  join mentions m on m.id = me.mention_id
  where m.published_at >= d_from
  group by 1, 2, 3, 4;
end;
$$;

-- ---------------------------------------------------------------------------
-- Detector helpers. Math lives next to the data; Python applies thresholds,
-- cooldown and upserts alerts.
-- ---------------------------------------------------------------------------

-- Volume: evaluate the N most recent COMPLETE hour buckets per active entity.
-- Baseline = preceding p_baseline_hours, ZERO-FILLED via generate_series
-- (agg_hourly has no rows for quiet hours; without zero-fill mu is inflated
-- and sigma collapsed, which kills the math).
create or replace function fn_volume_check(p_buckets int default 3, p_baseline_hours int default 168)
returns table (
  entity_id    bigint,
  window_start timestamptz,
  current_n    bigint,
  mu           numeric,
  sigma        numeric,
  z            numeric,
  data_age_hours numeric
)
language sql
stable
security definer set search_path = public set timezone = 'UTC'
as $$
with counts as (
  select a.entity_id, a.bucket, sum(a.mention_n)::bigint as n
  from agg_hourly a
  group by 1, 2
),
eval_buckets as (
  select e.id as entity_id,
         date_trunc('hour', now()) - make_interval(hours => i.i) as bucket,
         e.created_at
  from entities e
  cross join generate_series(1, p_buckets) as i(i)
  where e.active
),
first_data as (
  select entity_id, min(bucket) as first_bucket from agg_hourly group by 1
),
cur as (
  select b.entity_id, b.bucket, coalesce(c.n, 0) as current_n,
         -- warmup proxy = data coverage, not entity-row age (entities are seeded
         -- in Phase 0 before ingest starts, so created_at would falsely clear warmup)
         greatest(b.created_at, coalesce(f.first_bucket, now())) as coverage_start
  from eval_buckets b
  left join counts c on c.entity_id = b.entity_id and c.bucket = b.bucket
  left join first_data f on f.entity_id = b.entity_id
),
base as (
  select b.entity_id, b.bucket,
         avg(coalesce(c.n, 0))                as mu,
         coalesce(stddev_pop(coalesce(c.n,0)), 0) as sigma
  from eval_buckets b
  cross join lateral generate_series(
      b.bucket - make_interval(hours => p_baseline_hours),
      b.bucket - interval '1 hour',
      interval '1 hour') as g(h)
  left join counts c on c.entity_id = b.entity_id and c.bucket = g.h
  group by 1, 2
)
select cur.entity_id,
       cur.bucket as window_start,
       cur.current_n,
       round(base.mu, 3),
       round(base.sigma, 3),
       round((cur.current_n - base.mu) / greatest(base.sigma, 1), 3) as z,
       extract(epoch from (now() - cur.coverage_start)) / 3600.0 as data_age_hours
from cur
join base on base.entity_id = cur.entity_id and base.bucket = cur.bucket;
$$;

-- Sentiment: trailing 24h weighted average vs the preceding p_baseline_days.
create or replace function fn_sentiment_check(p_window_hours int default 24, p_baseline_days int default 7)
returns table (
  entity_id    bigint,
  window_start timestamptz,
  current_avg  numeric,
  baseline_avg numeric,
  analyzed_n   bigint,
  mention_n    bigint,
  analyzed_ratio numeric,
  data_age_hours numeric
)
language sql
stable
security definer set search_path = public set timezone = 'UTC'
as $$
with w as (
  select date_trunc('hour', now()) - make_interval(hours => p_window_hours) as w_start,
         date_trunc('hour', now()) as w_end
),
cur as (
  select a.entity_id,
         sum(a.sent_sum)    as sent_sum,
         sum(a.analyzed_n)::bigint  as analyzed_n,
         sum(a.mention_n)::bigint   as mention_n
  from agg_hourly a, w
  where a.bucket >= w.w_start and a.bucket < w.w_end
  group by 1
),
base as (
  select a.entity_id,
         sum(a.sent_sum)   as sent_sum,
         sum(a.analyzed_n) as analyzed_n
  from agg_hourly a, w
  where a.bucket >= w.w_start - make_interval(days => p_baseline_days)
    and a.bucket <  w.w_start
  group by 1
),
first_data as (
  select entity_id, min(bucket) as first_bucket from agg_hourly group by 1
)
select e.id,
       w.w_start,
       round(cur.sent_sum  / nullif(cur.analyzed_n, 0), 3)  as current_avg,
       round(base.sent_sum / nullif(base.analyzed_n, 0), 3) as baseline_avg,
       coalesce(cur.analyzed_n, 0),
       coalesce(cur.mention_n, 0),
       round(coalesce(cur.analyzed_n, 0)::numeric / nullif(cur.mention_n, 0), 3),
       -- warmup proxy = data coverage, not entity-row age
       extract(epoch from (now() - greatest(e.created_at, coalesce(fd.first_bucket, now())))) / 3600.0
from entities e
cross join w
left join cur  on cur.entity_id  = e.id
left join base on base.entity_id = e.id
left join first_data fd on fd.entity_id = e.id
where e.active;
$$;

-- Disk watchdog for the retention job (DELETE never shrinks pg_database_size;
-- alert at 350MB, well before the 500MB read-only line).
create or replace function fn_db_size()
returns bigint language sql stable
security definer set search_path = public set timezone = 'UTC'
as $$ select pg_database_size(current_database()); $$;

-- Keep detector/aggregation functions out of client reach.
revoke execute on function fn_recompute_agg(int, int) from public, anon, authenticated;
revoke execute on function fn_volume_check(int, int) from public, anon, authenticated;
revoke execute on function fn_sentiment_check(int, int) from public, anon, authenticated;
revoke execute on function fn_db_size() from public, anon, authenticated;

-- ---------------------------------------------------------------------------
-- Dashboard views (security_invoker: caller's RLS applies, never definer's)
-- ---------------------------------------------------------------------------

create view v_agg_hourly with (security_invoker = true) as
  select a.*, e.slug, e.name, e.side
  from agg_hourly a join entities e on e.id = a.entity_id;

create view v_agg_daily with (security_invoker = true) as
  select a.*, e.slug, e.name, e.side
  from agg_daily a join entities e on e.id = a.entity_id;

create view v_alerts with (security_invoker = true) as
  select al.*, e.slug, e.name, e.side
  from alerts al join entities e on e.id = al.entity_id;

create view v_mentions with (security_invoker = true) as
  select me.entity_id, e.slug, e.name, e.side,
         m.id as mention_id, m.platform, m.source_id, m.kind, m.url, m.title,
         m.body, m.body_purged_at, m.lang, m.published_at, m.metrics,
         -- cross-platform engagement (reddit score / youtube likes) for the
         -- "最高互動" mention-stream sort
         greatest(coalesce((m.metrics->>'score')::int, 0),
                  coalesce((m.metrics->>'likes')::int, 0)) as engagement,
         me.sentiment, me.label, me.confidence, me.aspects
  from mention_entities me
  join mentions m on m.id = me.mention_id
  join entities e on e.id = me.entity_id
  where me.relevant is not false;

create view v_data_quality with (security_invoker = true) as
  select distinct on (job) job, started_at, finished_at, status, stats
  from pipeline_runs
  order by job, started_at desc;

-- ---------------------------------------------------------------------------
-- RLS: deny-by-default. anon = nothing. authenticated = read-only + ack alerts.
-- ---------------------------------------------------------------------------

alter table entities         enable row level security;
alter table keywords         enable row level security;
alter table sources          enable row level security;
alter table app_config       enable row level security;
alter table mentions         enable row level security;
alter table mention_entities enable row level security;
alter table agg_hourly       enable row level security;
alter table agg_daily        enable row level security;
alter table alerts           enable row level security;
alter table pipeline_runs    enable row level security;

-- Supabase grants broad table privileges to anon/authenticated by default;
-- strip them so RLS is not the only line of defense.
revoke all on all tables in schema public from anon;
-- Full reset then explicit SELECT: removes TRUNCATE/REFERENCES/TRIGGER (TRUNCATE
-- isn't governed by RLS) and makes the SELECT that auth_select policies depend on
-- explicit, instead of silently inherited from Supabase default privileges.
revoke all on all tables in schema public from authenticated;
grant select on all tables in schema public to authenticated;

-- Operator (authenticated) read policies. Sensitive config stays read-VISIBLE
-- (operator needs to see thresholds in the dashboard) but not writable here;
-- edits happen in Supabase Studio as project owner.
create policy auth_select on entities         for select to authenticated using (true);
create policy auth_select on keywords         for select to authenticated using (true);
create policy auth_select on sources          for select to authenticated using (true);
create policy auth_select on app_config       for select to authenticated using (true);
create policy auth_select on mentions         for select to authenticated using (true);
create policy auth_select on mention_entities for select to authenticated using (true);
create policy auth_select on agg_hourly       for select to authenticated using (true);
create policy auth_select on agg_daily        for select to authenticated using (true);
create policy auth_select on alerts           for select to authenticated using (true);
create policy auth_select on pipeline_runs    for select to authenticated using (true);

-- ack/resolve: RLS is row-level only; the column restriction is the GRANT.
grant update (status) on alerts to authenticated;
create policy auth_ack on alerts for update to authenticated using (true) with check (true);

-- ---------------------------------------------------------------------------
-- Storage buckets: Reddit body archives (pipeline-only) + generated reports
-- (operator-readable from the dashboard).
-- ---------------------------------------------------------------------------

insert into storage.buckets (id, name, public) values
  ('archives', 'archives', false),
  ('reports',  'reports',  false)
on conflict (id) do nothing;

create policy reports_read on storage.objects
  for select to authenticated using (bucket_id = 'reports');
