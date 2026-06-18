// Cloudflare Worker: serves the dashboard SPA (static assets via env.ASSETS) and
// a small /api/discover endpoint for ad-hoc LIVE search of Reddit (Arctic Shift)
// and YouTube. API keys stay server-side here; the endpoint is gated by the
// caller's Supabase JWT so it can't be used as an open quota-draining proxy.
//
// Reddit note: Arctic Shift full-text `query` REQUIRES a subreddit scope, so
// discovery searches within the subreddit(s) the caller supplies (no global
// Reddit search exists on the free tier). YouTube search IS global but needs
// YT_API_KEY set as a Worker secret (optional — Reddit works without it).

const ARCTIC = "https://arctic-shift.photon-reddit.com/api";

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

async function verifyUser(request, env) {
  const auth = request.headers.get("Authorization") || "";
  if (!auth.startsWith("Bearer ") || !env.SUPABASE_URL || !env.SUPABASE_ANON_KEY) return false;
  try {
    const r = await fetch(`${env.SUPABASE_URL}/auth/v1/user`, {
      headers: { apikey: env.SUPABASE_ANON_KEY, Authorization: auth },
    });
    return r.ok;
  } catch {
    return false;
  }
}

async function searchReddit(q, subreddits, limit) {
  const out = [];
  await Promise.all(subreddits.flatMap((sub) =>
    ["posts", "comments"].map(async (kind) => {
      try {
        const u = new URL(`${ARCTIC}/${kind}/search`);
        u.searchParams.set("subreddit", sub);
        u.searchParams.set("query", q);
        u.searchParams.set("sort", "desc");
        u.searchParams.set("limit", String(limit));
        const r = await fetch(u, { headers: { "User-Agent": "mention-radar-discover/1.0" } });
        if (!r.ok) return;
        const data = (await r.json()).data || [];
        for (const it of data) {
          out.push({
            platform: "reddit",
            kind: kind === "posts" ? "post" : "comment",
            subreddit: sub,
            title: it.title || null,
            body: it.selftext || it.body || "",
            url: "https://www.reddit.com" + (it.permalink || `/r/${sub}/`),
            created: Number(it.created_utc) || 0,
            score: it.score ?? null,
          });
        }
      } catch { /* skip this source */ }
    })));
  return out;
}

async function searchYouTube(q, limit, env) {
  if (!env.YT_API_KEY) return { items: [], note: "YouTube 未啟用:Worker 尚未設定 YT_API_KEY secret" };
  try {
    const u = new URL("https://www.googleapis.com/youtube/v3/search");
    u.searchParams.set("part", "snippet");
    u.searchParams.set("q", q);
    u.searchParams.set("type", "video");
    u.searchParams.set("order", "date");
    u.searchParams.set("maxResults", String(Math.min(limit, 15)));
    u.searchParams.set("key", env.YT_API_KEY);
    const r = await fetch(u);
    if (!r.ok) return { items: [], note: "YouTube 查詢失敗 HTTP " + r.status };
    const data = await r.json();
    const items = (data.items || []).map((v) => ({
      platform: "youtube",
      kind: "video",
      title: v.snippet?.title || "",
      body: v.snippet?.description || "",
      url: "https://www.youtube.com/watch?v=" + (v.id?.videoId || ""),
      channel: v.snippet?.channelTitle || "",
      created: v.snippet?.publishedAt || null,
    }));
    return { items };
  } catch (e) {
    return { items: [], note: "YouTube 例外:" + (e?.message || "unknown") };
  }
}

async function discover(request, url, env) {
  if (!(await verifyUser(request, env))) return json({ error: "未授權(請重新登入)" }, 401);
  const q = (url.searchParams.get("q") || "").trim();
  if (!q) return json({ error: "缺少搜尋字 q" }, 400);
  const platform = url.searchParams.get("platform") || "both";
  const limit = Math.min(Math.max(Number(url.searchParams.get("limit")) || 10, 1), 25);
  const subs = (url.searchParams.get("subreddits") || "")
    .split(",").map((s) => s.trim().replace(/^r\//i, "")).filter(Boolean).slice(0, 8);

  const result = { reddit: [], youtube: [], notes: [] };
  const tasks = [];
  if (platform !== "youtube") {
    if (subs.length) {
      tasks.push(searchReddit(q, subs, limit).then((r) => { result.reddit = r; }));
    } else {
      result.notes.push("Reddit:未指定 subreddit(Arctic Shift 全文搜尋需指定版面)");
    }
  }
  if (platform !== "reddit") {
    tasks.push(searchYouTube(q, limit, env).then((y) => {
      result.youtube = y.items;
      if (y.note) result.notes.push(y.note);
    }));
  }
  await Promise.all(tasks);
  result.reddit.sort((a, b) => (b.created || 0) - (a.created || 0));
  return json(result, 200);
}

// On-demand sentiment for discover results. Mirrors the pipeline: Gemini
// 2.5-flash-lite with structured JSON output. Ephemeral (nothing is stored).
async function scoreSentiment(texts, env) {
  if (!env.GEMINI_API_KEY) {
    return { scores: null, note: "情緒分析未啟用:Worker 尚未設定 GEMINI_API_KEY secret" };
  }
  const numbered = texts.map((t, i) => `${i}: ${String(t || "").replace(/\s+/g, " ").slice(0, 500)}`).join("\n");
  const prompt = "You classify sentiment of social-media product mentions. For each "
    + "numbered item, judge sentiment toward the main product/brand discussed. "
    + "Return one object per item with its index i, a label (pos/neu/neg) and a "
    + "score from -1.0 (very negative) to 1.0 (very positive).\n\nItems:\n" + numbered;
  try {
    const r = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key=${env.GEMINI_API_KEY}`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          contents: [{ parts: [{ text: prompt }] }],
          generationConfig: {
            temperature: 0,
            responseMimeType: "application/json",
            responseSchema: {
              type: "ARRAY",
              items: {
                type: "OBJECT",
                properties: {
                  i: { type: "INTEGER" },
                  label: { type: "STRING", enum: ["pos", "neu", "neg"] },
                  score: { type: "NUMBER" },
                },
                required: ["i", "label", "score"],
              },
            },
          },
        }),
      },
    );
    if (!r.ok) return { scores: null, note: "Gemini HTTP " + r.status };
    const data = await r.json();
    const txt = data.candidates?.[0]?.content?.parts?.[0]?.text || "[]";
    return { scores: JSON.parse(txt) };
  } catch (e) {
    return { scores: null, note: "Gemini 例外:" + (e?.message || "unknown") };
  }
}

async function sentiment(request, env) {
  if (!(await verifyUser(request, env))) return json({ error: "未授權(請重新登入)" }, 401);
  let body;
  try { body = await request.json(); } catch { return json({ error: "bad json" }, 400); }
  const texts = Array.isArray(body?.texts) ? body.texts.slice(0, 25) : [];
  if (!texts.length) return json({ error: "no texts" }, 400);
  return json(await scoreSentiment(texts, env), 200);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/api/discover") return discover(request, url, env);
    if (url.pathname === "/api/sentiment" && request.method === "POST") return sentiment(request, env);
    return env.ASSETS.fetch(request);  // everything else → static SPA
  },
};
