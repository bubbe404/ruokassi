// suggest-week — LLM-assisted weekly dinner suggestions for ruokassi (M4.2 / M6).
// Reads the recipe library + planning history server-side (service role), asks
// an LLM for a rule-respecting week, and returns library picks + optional novel
// recipe ideas + a one-line Finnish rationale. On any failure (missing key, API
// error, no valid tool call) it returns { fallback: true } so the client falls
// back to its local heuristic.
//
// M6 hardening (Fable review S7/S8/S9):
//  S8 — forced tool-use JSON schema (no more fence-stripping / parse failures),
//       picks deduped against keepIds and each other, novel enums validated,
//       higher max_tokens, one retry on 429/529.
//  S7 — plan_id coerced with Number(), explicit signed-in-user check via
//       GET /auth/v1/user, CORS pinned to the Pages origin, no key length logged.
//  S9 — the plan note / lunches flag / household food-prefs are fed into the
//       prompt; the model's rationale is returned as `note`.
import "jsr:@supabase/functions-js/edge-runtime.d.ts";

// S7: pin CORS to the app origin (override with ALLOW_ORIGIN if the Pages URL changes).
const ORIGIN = Deno.env.get("ALLOW_ORIGIN") || "https://bubbe404.github.io";
const cors = {
  "Access-Control-Allow-Origin": ORIGIN,
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Vary": "Origin",
};
const SB_URL = Deno.env.get("SUPABASE_URL")!;
const SRK = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const AK = Deno.env.get("ANTHROPIC_API_KEY") || "";
// Current models (2026); override/reorder via SUGGEST_MODEL="id1,id2". Tries each, skipping 404s.
const MODELS = (Deno.env.get("SUGGEST_MODEL") || "claude-haiku-4-5-20251001,claude-sonnet-5,claude-opus-5")
  .split(",").map((s) => s.trim()).filter(Boolean);

const EFFORTS = ["quick", "normal", "long"];
const SEASONS = ["easter", "vappu", "midsummer", "christmas"];

function json(o: unknown, status = 200) {
  return new Response(JSON.stringify(o), { status, headers: { ...cors, "content-type": "application/json" } });
}
async function rest(path: string) {
  const r = await fetch(`${SB_URL}/rest/v1/${path}`, { headers: { apikey: SRK, authorization: `Bearer ${SRK}` } });
  if (!r.ok) throw new Error(`rest ${path}: ${r.status}`);
  return await r.json();
}
// S7: confirm the caller is a signed-in user, not merely a holder of the anon key
// (verify_jwt=true accepts the anon JWT too). Returns the user id, or throws.
async function requireUser(authHeader: string | null): Promise<string> {
  if (!authHeader) throw new Error("no_auth");
  const r = await fetch(`${SB_URL}/auth/v1/user`, { headers: { apikey: SRK, authorization: authHeader } });
  if (!r.ok) throw new Error("not_a_user " + r.status);
  const u = await r.json();
  if (!u || !u.id) throw new Error("not_a_user");
  return u.id;
}
function tagList(r: any): string[] {
  const t: string[] = [];
  if (r.is_vegetarian) t.push("vegetarian");
  if (r.has_fish) t.push("fish");
  if (r.has_legume) t.push("legume");
  if (r.has_tomato_sauce) t.push("tomato");
  if (r.freezer_ok) t.push("freezer");
  if (r.is_bread_centric) t.push("bread");
  if (r.effort && r.effort !== "normal") t.push(r.effort);
  return t;
}
function buildPrompt(p: {
  dinners: number; need: number; keepIds: number[]; lib: any[]; today: string;
  note: string; lunches: boolean; prefs: string;
}): string {
  const ctx: string[] = [];
  if (p.note) ctx.push(`This week's note (respect it — guests, travel, eating out, requests): ${p.note}`);
  ctx.push(p.lunches ? "The family also wants lunches this week — lean a little toward batch/leftover-friendly dinners." : "Dinners only this week.");
  if (p.prefs) ctx.push(`Standing household food preferences (likes / dislikes / avoid): ${p.prefs}`);
  return `You are planning weekly family dinners for a Finnish family (2 adults + 2 young children). Today is ${p.today}.

House rules:
- at least 50% of the week's dinners vegetarian
- about 1 fish dish per week
- at most 1 tomato-based dish
- at most 1 legume dish (soy preferred)
- at least 1 freezer-friendly / batch dish
- no bread-centric mains
Favour recipes not cooked recently. Respect the season/date (no clearly out-of-season holiday dishes).

Context for this week:
${ctx.map((c) => "- " + c).join("\n")}

Recipe library (id: name [tags] season last-planned):
${p.lib.map((r) => `- ${r.id}: ${r.name} [${r.tags.join(", ") || "no tags"}]${r.season ? " season=" + r.season : ""} last=${r.last_planned || "never"}`).join("\n")}

The week needs ${p.dinners} dinners total.${p.keepIds.length ? ` Already chosen (keep these and count them toward the rules): ids ${p.keepIds.join(", ")}.` : ""} Propose ${p.need} more dinner(s) so the full week satisfies the rules.

Prefer filling "picks" from the library (do not repeat an id already chosen, and no duplicates). Optionally add up to 3 novel recipe ideas that fit the rules/season and add variety (ingredient lines in Finnish: amount + item). If the library cannot satisfy the rules, lean on novel and return fewer picks. Call the submit_week tool with your plan.`;
}

// S8: a forced tool schema — the model returns structured input, so there is no
// free-text JSON to strip fences from or fail to parse.
const TOOL = {
  name: "submit_week",
  description: "Return the proposed dinners for the week.",
  input_schema: {
    type: "object",
    properties: {
      picks: { type: "array", items: { type: "integer" }, description: "Library recipe ids to add." },
      novel: {
        type: "array",
        items: {
          type: "object",
          properties: {
            name: { type: "string" },
            is_vegetarian: { type: "boolean" },
            has_fish: { type: "boolean" },
            has_legume: { type: "boolean" },
            has_tomato_sauce: { type: "boolean" },
            freezer_ok: { type: "boolean" },
            is_bread_centric: { type: "boolean" },
            effort: { type: "string", enum: EFFORTS },
            season: { type: ["string", "null"], enum: [...SEASONS, null] },
            ingredients: { type: "array", items: { type: "string" } },
          },
          required: ["name", "is_vegetarian", "ingredients"],
        },
      },
      note: { type: "string", description: "One short sentence in Finnish explaining the week's choices." },
    },
    required: ["picks"],
  },
};

async function anthropicOnce(model: string, prompt: string) {
  const doFetch = () => fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: { "x-api-key": AK, "anthropic-version": "2023-06-01", "content-type": "application/json" },
    body: JSON.stringify({
      model, max_tokens: 2000,
      tools: [TOOL], tool_choice: { type: "tool", name: "submit_week" },
      messages: [{ role: "user", content: prompt }],
    }),
  });
  let r = await doFetch();
  // S8: one retry on transient overload / rate limit.
  if (r.status === 429 || r.status === 529) {
    await new Promise((res) => setTimeout(res, 1200));
    r = await doFetch();
  }
  const bodyText = await r.text();
  if (r.status === 404) { const e: any = new Error("model_not_found " + model); e.notFound = true; throw e; }
  if (!r.ok) throw new Error("anthropic " + r.status + " " + bodyText.slice(0, 300));
  const d = JSON.parse(bodyText);
  const tu = (d.content || []).find((c: any) => c.type === "tool_use" && c.name === "submit_week");
  if (!tu || !tu.input) throw new Error("no_tool_use");
  return { ai: tu.input, model };
}
async function callAnthropic(prompt: string) {
  let lastErr: any;
  for (const m of MODELS) {
    try { const res = await anthropicOnce(m, prompt); console.log("suggest-week used model=" + m); return res; }
    catch (e: any) { if (e && e.notFound) { console.log("suggest-week model 404 " + m); lastErr = e; continue; } throw e; }
  }
  throw lastErr || new Error("no model available");
}
function cleanNovel(arr: any): any[] {
  if (!Array.isArray(arr)) return [];
  return arr.slice(0, 3).map((n: any) => ({
    name: String(n?.name || "").slice(0, 120),
    is_vegetarian: !!n?.is_vegetarian,
    has_fish: !!n?.has_fish,
    has_legume: !!n?.has_legume,
    has_tomato_sauce: !!n?.has_tomato_sauce,
    freezer_ok: !!n?.freezer_ok,
    is_bread_centric: !!n?.is_bread_centric,
    effort: EFFORTS.includes(n?.effort) ? n.effort : "normal",
    season: SEASONS.includes(n?.season) ? n.season : null,
    ingredients: Array.isArray(n?.ingredients) ? n.ingredients.map((x: any) => String(x)).filter(Boolean).slice(0, 40) : [],
  })).filter((n: any) => n.name);
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  try {
    console.log(`suggest-week start hasKey=${!!AK} models=${MODELS.join("/")}`);
    if (!AK) { console.log("suggest-week no_key"); return json({ error: "no_key", fallback: true }); }

    // S7: require a real signed-in user (defence in depth over verify_jwt).
    try { await requireUser(req.headers.get("authorization")); }
    catch (e) { console.log("suggest-week unauthorized: " + String(e)); return json({ error: "unauthorized" }, 401); }

    const body = await req.json().catch(() => ({}));
    const dinners = Number(body.dinners) || 5;
    const need = Math.max(1, Number(body.need) || dinners);
    const planId = Number(body.plan_id) || null;   // S7: coerce, don't interpolate raw
    const keepIds: number[] = (Array.isArray(body.keep_recipe_ids) ? body.keep_recipe_ids : [])
      .map(Number).filter((n: number) => Number.isFinite(n));
    const note = String(body.note || "").slice(0, 500);
    const lunches = !!body.lunches;
    const prefs = String(body.prefs || "").slice(0, 500);

    const recipes = await rest(
      `recipes?select=id,name,is_vegetarian,has_fish,has_legume,has_tomato_sauce,freezer_ok,is_bread_centric,effort,season,disliked&disliked=eq.false&order=name`,
    );
    const hist = await rest(
      `meal_plan_slots?select=recipe_id,meal_plans(week_start)${planId ? `&plan_id=neq.${planId}` : ""}`,
    );
    const last: Record<number, string> = {};
    for (const h of hist) {
      const w = h.meal_plans?.week_start;
      if (w && (!last[h.recipe_id] || w > last[h.recipe_id])) last[h.recipe_id] = w;
    }
    const lib = recipes.map((r: any) => ({ id: r.id, name: r.name, tags: tagList(r), season: r.season, last_planned: last[r.id] || null }));
    if (!lib.length) return json({ error: "empty_library", fallback: true });

    const today = new Date().toISOString().slice(0, 10);
    const { ai, model: usedModel } = await callAnthropic(buildPrompt({ dinners, need, keepIds, lib, today, note, lunches, prefs }));

    // S8: valid library ids only; drop anything already chosen; dedupe; cap to need.
    const libIds = new Set(lib.map((l: any) => l.id));
    const keepSet = new Set(keepIds);
    const seen = new Set<number>();
    const picks: number[] = [];
    for (const raw of (Array.isArray(ai.picks) ? ai.picks : [])) {
      const id = Number(raw);
      if (!libIds.has(id) || keepSet.has(id) || seen.has(id)) continue;
      seen.add(id); picks.push(id);
      if (picks.length >= need) break;
    }
    const novel = cleanNovel(ai.novel);
    console.log(`suggest-week ok picks=${picks.length} novel=${novel.length} model=${usedModel}`);
    return json({ picks, novel, note: typeof ai.note === "string" ? ai.note.slice(0, 300) : "", model: usedModel });
  } catch (e) {
    console.log("suggest-week fail: " + String(e));
    return json({ error: String(e), fallback: true });
  }
});
