// refresh-orders — the app's "Refresh orders" button (M7).
// Starts the `ingest` GitHub Actions job on demand (IMAP → parse → Supabase)
// instead of waiting for the 2-hourly cron, and reports its progress.
//
// The page cannot call GitHub itself: that would put a token in a public page.
// This function holds GH_DISPATCH_TOKEN — a fine-grained token limited to
// bubbe404/ruokassi with Actions read/write — and only signed-in users reach it.
//
// POST { action: "start" }               → { state: "dispatched"|"running", since }
// POST { action: "status", since: iso }  → { run: {status, conclusion, created_at, url} | null }
//
// Guard: a new run is not dispatched while one is queued/running, or within
// COOLDOWN_S of the previous on-demand run; the caller just follows that run.
import "jsr:@supabase/functions-js/edge-runtime.d.ts";

const ORIGIN = Deno.env.get("ALLOW_ORIGIN") || "https://bubbe404.github.io";
const cors = {
  "Access-Control-Allow-Origin": ORIGIN,
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Vary": "Origin",
};
const SB_URL = Deno.env.get("SUPABASE_URL")!;
const SRK = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const GH = Deno.env.get("GH_DISPATCH_TOKEN") || "";
const REPO = Deno.env.get("GH_REPO") || "bubbe404/ruokassi";
const WORKFLOW = "ingest.yml";
const COOLDOWN_S = 120;

function json(o: unknown, status = 200) {
  return new Response(JSON.stringify(o), { status, headers: { ...cors, "content-type": "application/json" } });
}

// verify_jwt=true also admits the bare anon key; require a real signed-in user.
async function requireUser(authHeader: string | null): Promise<void> {
  if (!authHeader) throw new Error("no_auth");
  const r = await fetch(`${SB_URL}/auth/v1/user`, { headers: { apikey: SRK, authorization: authHeader } });
  if (!r.ok) throw new Error("not_a_user " + r.status);
  const u = await r.json();
  if (!u || !u.id) throw new Error("not_a_user");
}

async function gh(path: string, init: RequestInit = {}) {
  const r = await fetch(`https://api.github.com/repos/${REPO}${path}`, {
    ...init,
    headers: {
      accept: "application/vnd.github+json",
      authorization: `Bearer ${GH}`,
      "x-github-api-version": "2022-11-28",
      "user-agent": "ruokassi-refresh-orders",
      ...(init.body ? { "content-type": "application/json" } : {}),
    },
  });
  if (!r.ok && r.status !== 204) {
    const t = await r.text();
    throw new Error(`github ${r.status} ${t.slice(0, 200)}`);
  }
  return r.status === 204 ? null : await r.json();
}

type Run = { status: string; conclusion: string | null; created_at: string; html_url: string; event: string };

async function recentRuns(): Promise<Run[]> {
  const d = await gh(`/actions/workflows/${WORKFLOW}/runs?per_page=10`);
  return (d?.workflow_runs || []) as Run[];
}
const busy = (r: Run) => r.status !== "completed";
const view = (r: Run) => ({ status: r.status, conclusion: r.conclusion, created_at: r.created_at, url: r.html_url });

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  try { await requireUser(req.headers.get("authorization")); }
  catch (e) { console.log("refresh-orders unauthorized: " + String(e)); return json({ error: "unauthorized" }, 401); }
  if (!GH) return json({ error: "not_configured" }, 503);

  const body = await req.json().catch(() => ({}));
  try {
    if (body.action === "status") {
      const since = Date.parse(String(body.since || "")) || 0;
      // the run this button started (or joined): created no earlier than `since`
      const run = (await recentRuns()).find((r) => Date.parse(r.created_at) >= since - 5000);
      return json({ run: run ? view(run) : null });
    }

    if (body.action === "start") {
      const runs = await recentRuns();
      const running = runs.find(busy);
      if (running) return json({ state: "running", since: running.created_at, run: view(running) });
      const lastManual = runs.find((r) => r.event === "workflow_dispatch");
      if (lastManual && Date.now() - Date.parse(lastManual.created_at) < COOLDOWN_S * 1000) {
        return json({ state: "running", since: lastManual.created_at, run: view(lastManual) });
      }
      const since = new Date().toISOString();
      await gh(`/actions/workflows/${WORKFLOW}/dispatches`, {
        method: "POST",
        body: JSON.stringify({ ref: "main", inputs: { mode: "daily" } }),
      });
      console.log("refresh-orders dispatched");
      return json({ state: "dispatched", since });
    }

    return json({ error: "bad_action" }, 400);
  } catch (e) {
    console.log("refresh-orders error: " + String(e));
    return json({ error: "github_failed" }, 502);
  }
});
