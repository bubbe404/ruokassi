// suggest-week — LLM-assisted weekly dinner suggestions for ruokassi (M4.2).
// Reads the recipe library + planning history server-side (service role), asks
// an LLM for a rule-respecting week, and returns library picks + optional novel
// recipe ideas. On any failure (missing key, API error, bad JSON) it returns
// { fallback: true } so the client falls back to its local heuristic.
import "jsr:@supabase/functions-js/edge-runtime.d.ts";

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};
const SB_URL = Deno.env.get("SUPABASE_URL")!;
const SRK = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const AK = Deno.env.get("ANTHROPIC_API_KEY") || "";
// Current models (2026); override/reorder via SUGGEST_MODEL="id1,id2". Tries each, skipping 404s.
const MODELS = (Deno.env.get("SUGGEST_MODEL") || "claude-haiku-4-5-20251001,claude-sonnet-5,claude-opus-5")
  .split(",").map((s) => s.trim()).filter(Boolean);

function json(o: unknown, status = 200) {
  return new Response(JSON.stringify(o), { status, headers: { ...cors, "content-type": "application/json" } });
}
async function rest(path: string) {
  const r = await fetch(`${SB_URL}/rest/v1/${path}`, { headers: { apikey: SRK, authorization: `Bearer ${SRK}` } });
  if (!r.ok) throw new Error(`rest ${path}: ${r.status}`);
  return await r.json();
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
function stripFences(s: string): string {
  const m = s.match(/\{[\s\S]*\}/);
  return m ? m[0] : s;
}
function buildPrompt(p: { dinners: number; need: number; keepIds: number[]; lib: any[]; today: string }): string {
  return `You are planning weekly family dinners for a Finnish family (2 adults + 2 young children). Today is ${p.today}.

House rules:
- at least 50% of the week's dinners vegetarian
- about 1 fish dish per week
- at most 1 tomato-based dish
- at most 1 legume dish (soy preferred)
- at least 1 freezer-friendly / batch dish
- no bread-centric mains
Favour recipes not cooked recently. Respect the season/date (no clearly out-of-season holiday dishes).

Recipe library (id: name [tags] season last-planned):
${p.lib.map((r) => `- ${r.id}: ${r.name} [${r.tags.join(", ") || "no tags"}]${r.season ? " season=" + r.season : ""} last=${r.last_planned || "never"}`).join("\n")}

The week needs ${p.dinners} dinners total.${p.keepIds.length ? ` Already chosen (keep these and count them toward the rules): ids ${p.keepIds.join(", ")}.` : ""} Propose ${p.need} more dinner(s) so the full week satisfies the rules.

Respond with ONLY minified JSON, no prose, exactly this shape:
{"picks":[library recipe ids to add, length ${p.need}],"novel":[{"name":"","is_vegetarian":true,"has_fish":false,"has_legume":false,"has_tomato_sauce":false,"freezer_ok":false,"is_bread_centric":false,"effort":"normal","season":null,"ingredients":["määrä + aine"]}],"note":"one short sentence in Finnish"}
Prefer filling "picks" from the library. Optionally add up to 3 novel recipe ideas in "novel" that fit the rules/season and add variety (ingredient lines in Finnish: amount + item). If the library cannot satisfy the rules, lean on novel and return fewer picks. The whole response must be valid JSON.`;
}
async function anthropicOnce(model: string, prompt: string) {
  const r = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: { "x-api-key": AK, "anthropic-version": "2023-06-01", "content-type": "application/json" },
    body: JSON.stringify({ model, max_tokens: 1200, messages: [{ role: "user", content: prompt }] }),
  });
  const bodyText = await r.text();
  if (r.status === 404) { const e: any = new Error("model_not_found " + model); e.notFound = true; throw e; }
  if (!r.ok) throw new Error("anthropic " + r.status + " " + bodyText.slice(0, 300));
  const d = JSON.parse(bodyText);
  const text = (d.content || []).map((c: any) => c.text || "").join("").trim();
  return { ai: JSON.parse(stripFences(text)), model };
}
async function callAnthropic(prompt: string) {
  let lastErr: any;
  for (const m of MODELS) {
    try { const res = await anthropicOnce(m, prompt); console.log("suggest-week used model=" + m); return res; }
    catch (e: any) { if (e && e.notFound) { console.log("suggest-week model 404 " + m); lastErr = e; continue; } throw e; }
  }
  throw lastErr || new Error("no model available");
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  try {
    console.log(`suggest-week start hasKey=${!!AK} keyLen=${AK.length} models=${MODELS.join("/")}`);
    if (!AK) { console.log("suggest-week no_key"); return json({ error: "no_key", fallback: true }); }
    const body = await req.json().catch(() => ({}));
    const dinners = Number(body.dinners) || 5;
    const need = Math.max(1, Number(body.need) || dinners);
    const planId = body.plan_id;
    const keepIds: number[] = Array.isArray(body.keep_recipe_ids) ? body.keep_recipe_ids : [];

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
    const { ai, model: usedModel } = await callAnthropic(buildPrompt({ dinners, need, keepIds, lib, today }));
    const libIds = new Set(lib.map((l: any) => l.id));
    const picks = (ai.picks || []).map(Number).filter((id: number) => libIds.has(id)).slice(0, need);
    const novel = (Array.isArray(ai.novel) ? ai.novel : []).slice(0, 3);
    console.log(`suggest-week ok picks=${picks.length} novel=${novel.length} model=${usedModel}`);
    return json({ picks, novel, note: typeof ai.note === "string" ? ai.note : "", model: usedModel });
  } catch (e) {
    console.log("suggest-week fail: " + String(e));
    return json({ error: String(e), fallback: true });
  }
});
