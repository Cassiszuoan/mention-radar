// Supabase access layer. anon key + RLS; operator signs in with email OTP.
// All reads hit pre-aggregated views/tables; raw scans never leave the DB.
import { createClient, type Session } from "@supabase/supabase-js";

export const sb = createClient(
  import.meta.env.VITE_SUPABASE_URL,
  import.meta.env.VITE_SUPABASE_ANON_KEY,
);

export type Entity = {
  id: number; slug: string; name: string; side: "ours" | "competitor";
};
export type AggRow = {
  entity_id: number; platform: string; source_id: number; bucket: string;
  mention_n: number; analyzed_n: number; pos_n: number; neu_n: number;
  neg_n: number; sent_sum: number; slug: string; name: string; side: string;
};
export type Alert = {
  id: number; entity_id: number; type: string; severity: "watch" | "high";
  triggered_at: string; window_start: string; window_end: string | null;
  observed: number | null; baseline: number | null; zscore: number | null;
  status: string; evidence: { items?: EvidenceItem[] } | null;
  slug: string; name: string;
};
export type EvidenceItem = {
  url?: string; platform?: string; published_at?: string;
  sentiment?: number; label?: string; summary?: string;
};
export type MentionRow = {
  entity_id: number; slug: string; name: string; mention_id: number;
  platform: string; source_id: number | null; kind: string; url: string | null;
  title: string | null; body: string | null; body_purged_at: string | null;
  lang: string | null; published_at: string; metrics: Record<string, number>;
  engagement: number | null;
  sentiment: number | null; label: string | null; aspects: { name: string; score: number }[] | null;
};

// Laptop-wakes-from-sleep path: a stale JWT must not render as "no alerts".
async function withRetry<T>(fn: () => Promise<{ data: T | null; error: unknown }>): Promise<T> {
  let res = await fn();
  if (res.error) {
    await sb.auth.refreshSession();
    res = await fn();
    if (res.error) throw res.error;
  }
  return (res.data ?? []) as T;
}

export const api = {
  session: (): Promise<Session | null> =>
    sb.auth.getSession().then((r) => r.data.session),

  entities: () =>
    withRetry<Entity[]>(() =>
      sb.from("entities").select("id, slug, name, side").eq("active", true) as never),

  aggHourly: (sinceIso: string) =>
    withRetry<AggRow[]>(() =>
      sb.from("v_agg_hourly").select("*").gte("bucket", sinceIso)
        .order("bucket", { ascending: true }).limit(20000) as never),

  aggDaily: (sinceIso: string) =>
    withRetry<AggRow[]>(() =>
      sb.from("v_agg_daily").select("*").gte("bucket", sinceIso)
        .order("bucket", { ascending: true }).limit(20000) as never),

  alerts: (openOnly: boolean) =>
    withRetry<Alert[]>(() => {
      let q = sb.from("v_alerts").select("*").order("triggered_at", { ascending: false }).limit(100);
      if (openOnly) q = q.eq("status", "open");
      return q as never;
    }),

  // Route through 401-recovery and surface failures: .select() makes a
  // 0-rows-updated (RLS/grant denial) detectable instead of a silent no-op.
  ackAlert: (id: number, status: "ack" | "resolved") =>
    withRetry<{ id: number }[]>(() =>
      sb.from("alerts").update({ status }).eq("id", id).select("id") as never),

  mentions: (entityId: number, sinceIso: string, order: "latest" | "negative" | "engaged",
             platform?: string, sourceId?: number) =>
    withRetry<MentionRow[]>(() => {
      let q = sb.from("v_mentions").select("*")
        .eq("entity_id", entityId).gte("published_at", sinceIso).limit(60);
      if (platform) q = q.eq("platform", platform);
      if (sourceId != null) q = q.eq("source_id", sourceId);
      if (order === "negative") q = q.order("sentiment", { ascending: true, nullsFirst: false });
      else if (order === "engaged") q = q.order("engagement", { ascending: false, nullsFirst: false });
      else q = q.order("published_at", { ascending: false });
      return q as never;
    }),

  sources: () =>
    withRetry<{ id: number; platform: string; kind: string; source_key: string }[]>(
      () => sb.from("sources").select("id, platform, kind, source_key") as never),

  quality: () =>
    withRetry<{ job: string; started_at: string; status: string; stats: Record<string, unknown> }[]>(
      () => sb.from("v_data_quality").select("*") as never),

  reports: async () => {
    const out: { period: string; name: string }[] = [];
    for (const period of ["weekly", "monthly"]) {
      const { data } = await sb.storage.from("reports").list(period, {
        limit: 50, sortBy: { column: "name", order: "desc" },
      });
      for (const f of data ?? []) {
        if (f.name.endsWith(".html")) out.push({ period, name: f.name });
      }
    }
    return out;
  },

  reportUrl: async (period: string, name: string) => {
    const { data, error } = await sb.storage.from("reports")
      .createSignedUrl(`${period}/${name}`, 3600);
    if (error) throw error;
    return data.signedUrl;
  },
};
