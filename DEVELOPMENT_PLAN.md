# ruokassi — Development plan (post‑M4)

_Date: 2026‑09‑05 · Author: Claude, with a Fable 5.1 code review · Status: proposed_

## Where we are

M0–M4 are built and live: receipt ingestion, the standing weekly basket, the recipe/meal planner, LLM‑assisted weekly suggestions (Supabase Edge Function + heuristic fallback + feedback loop), the i18n layer (FI/EN, SV stubbed), and a pile of fixes. The app is a single‑file static PWA on GitHub Pages backed by Supabase, used by two people.

A Fable 5.1 review of the whole codebase (client, ingest job, edge function, schema) found the code is in reasonable shape for what it is, but has crossed the size where its founding shortcuts now produce real bugs. Full findings are in the appendix; the headline is five things worth fixing before piling on more features.

## Sequencing

You asked to tackle **#2 smarter suggestions → #3 notifications → #1 visual polish**, with **#4 auto‑submit** last. That order stands. The one change I recommend: a short **hardening pass (M5)** first, because the review surfaced a bug that can silently stop your data pipeline (and it got *more* likely the moment we shipped the "add anything to the basket" feature), plus two cheap safety fixes that make everything after them easier to build on. It's roughly half a day of the critical items, and it de‑risks the feature work.

So the proposed milestones:

| Milestone | Theme | Your # | Why here |
|---|---|---|---|
| **M5** | Hardening & foundations | — | Stops a silent pipeline break; makes feature work safe |
| **M6** | Smarter suggestions | #2 | Your first pick; builds on the LLM engine just finished |
| **M7** | Receipt → notifications loop | #3 | Closes the original "event‑driven" vision |
| **M8** | Visual polish | #1 | Deferred until features done — now they are |
| **M9** | S‑kaupat auto‑submit | #4 | Highest effort/risk; last, as a separate track |

If you'd rather skip M5 and go straight to #2, that's your call — but I'd at least pull **C1** forward (details below), because it can quietly break receipt ingestion.

---

## M5 — Hardening & foundations _(recommended first; ~0.5–1 day for the criticals)_

Goal: fix the correctness/security issues that can bite, and put the foundations (schema in repo, error surfacing) in place so the feature milestones aren't built on sand.

**Must‑do (the criticals):**
- **C1 — ingest can crash and get stuck.** `supa.py` creates products with `resolution=merge-duplicates` but no `?on_conflict=name`, so a receipt naming a product the app already created (exactly what "Fairy"/add‑as‑new now produces) raises a 409, aborts the run, and the email is retried forever. Fix: `products?on_conflict=name`, and wrap the per‑message work in try/except that records the failure and continues. **Time‑sensitive.**
- **C2 — `cap()` doesn't escape HTML.** It's used in ~a dozen `innerHTML` templates for product/recipe names and the live search term, so typed text like `<img onerror=…>` executes (self‑XSS, but the session token + anon key are worth protecting). Fix: `cap = s => esc(capitalize(s))`; validate LLM `effort`/`season` against known values before storing/rendering.
- **C3 — write results are never checked.** Almost every `sb.from(...).insert/update/delete` ignores its `{error}`, so RLS/constraint failures show a success toast. Fix: make `q()` throw on error after the retry, add one `toastErr`, wrap handlers in try/catch. (This will also immediately reveal whether the `products` upsert needs an UPDATE policy, not just INSERT.)
- **C4 — `monday()` timezone bug.** Local‑time arithmetic then `toISOString()` can yield Sunday's date early Monday in Helsinki → two `meal_plans` rows for one week. Fix: build the date from local components; add `unique(week_start)`; upsert‑then‑select.
- **C5 — schema only lives in the live DB.** Everything after M0 (all planner tables, extra `recipes` columns, all RLS policies) is unversioned. Fix: `supabase db diff`/`pg_dump --schema-only` → commit `0002_planning_schema.sql`; run the Supabase **security advisor** and fix anything it flags (a table with RLS off is world‑readable via the anon key).

**High‑leverage should‑dos to fold in:**
- **S5/C1 follow‑up — one product identity rule.** Replace the three different "get‑or‑create product" paths (ingest upper‑case, `addNewProduct` upper, `addFreeItemToBasket` raw) with a single `get_or_create_product(name)` Postgres RPC that trims+normalizes case, used by both client and ingest. Add `unique(upper(name))`.
- **S2 — slot numbering** (`slots.length+1` collides after deletes) → `max(slot)+1` or order by `id`.
- **Commit `supabase/config.toml`** with `verify_jwt = true` so the edge function's auth isn't just "remembered" (S7).

Deliverable: a bundle + a committed migration; no user‑visible change except errors now surface instead of lying.

---

## M6 — Smarter suggestions _(#2)_

Goal: make the weekly suggestion genuinely better and more controllable, and harden the LLM path.

**Features:**
- **Reshuffle / regenerate button** — re‑run the suggestion for the empty slots (or clear the suggested ones and redo) without hand‑deleting.
- **Feed real context to the LLM (S9 — cheapest big win).** The plan's free‑text `note` ("guests, away, eating out") and the `lunches` flag already exist and the function has service‑role read access — pass them into the prompt. Surface the model's returned `note` (currently discarded) as a one‑line "why this week" rationale.
- **Saved preferences.** A small free‑text "preferences" field (likes/dislikes/avoid) persisted per‑household and fed to the prompt.
- **Seasonal produce weighting.** A Finnish seasonal‑produce calendar nudging both the heuristic and the LLM prompt toward in‑season veg (distinct from the existing holiday `season` tag). Fix `currentHoliday()`'s Easter window (N3) while here.

**LLM robustness (from the review):**
- **S8** — switch the edge function from "respond with ONLY JSON" + regex fence‑stripping to **tool‑use with a forced JSON schema** (removes the truncation/parse‑failure class that silently drops you to the heuristic). Dedupe `picks` against `keepIds` and against each other. Validate `novel[].effort/season` enums. Raise `max_tokens`. Retry once on 429/529 (not just 404).
- **S7** — validate `plan_id` with `Number()`; add an in‑function `GET /auth/v1/user` check; pin CORS to the Pages origin; drop `keyLen` from logs.

Deliverable: edge‑function redeploy (I can do that live) + a client bundle.

---

## M7 — Receipt → notifications loop _(#3)_

Goal: close the event‑driven loop — when a receipt lands, flag what's missing and nudge planning. **No outbound email** (Gmail is read‑only by your standing rule); use Web Push and/or in‑app.

**Scope:**
- On ingest, when a new receipt has `missing_items`, mark that as the signal. Options for delivery: (a) **Web Push** (needs a service worker + a VAPID key + a tiny push‑send step in the ingest job or an edge function), or (b) **in‑app**: a badge/banner on next open. Recommend starting with in‑app (no infra) and adding Web Push if you want it to reach the phone.
- **S10** — make the missing‑items section respect `missing_items.resolution`: show only `open`, with tap‑to‑resolve, so it stops being permanent noise after pickup.
- "Time to plan next week" nudge tied to the delivery‑slot timing.

Note: this milestone naturally introduces the **service worker**, which also fixes the caching pain (deploys showing up immediately instead of needing a hard‑refresh) — so M7 and M8's cache fix share that groundwork.

---

## M8 — Visual polish _(#1)_

Goal: the design pass deferred "until everything is done." Everything functional now is.

**Scope:**
- Spacing, hierarchy, typography, mobile feel; consistent styling across the four tabs; empty states; the recipe form; the suggestion/novel panels.
- **Service worker** with a sensible cache strategy (network‑first for the HTML) so every deploy is immediate — retires the manual `build 2026‑09‑05‑j` stamp / hard‑refresh dance. Stamp the git short‑SHA automatically in a Pages step (N12).
- **Split the single file** into `docs/app.js`, `docs/styles.css`, `docs/i18n/*` as `<script type="module">` — no bundler, GitHub Pages serves them as‑is. Kills the TDZ fragility and the `t()`/`toast` shadowing class of bug (N1), and lets an editor + ESLint catch them. Retire the regex‑replacement i18n build in favour of per‑language files + a key‑coverage check script (N2). Hide/label SV until translated.
- i18n niceties: locale‑aware `eur`/`fmtDate`/`trimQty` (currently hard‑wired to `fi‑FI`).

---

## M9 — S‑kaupat auto‑submit _(#4, separate track)_

Goal: place the order on s‑kaupat.fi from the basket. No API → **browser automation**. Highest effort and the most fragile (breaks when the site changes) and it acts on a real store account, so it's last and gated on appetite. Scope when we get there: a driven browser flow that logs in, adds the basket items, and stops at the review step for you to confirm — never auto‑confirming a real purchase.

---

## Added 2026‑09‑08 — M's requests

Five items from M, with suggested milestone placement:

1. **Suggest 7 recipe alternatives, not 3.** The edge function caps novel ideas at 3 (`novel.slice(0,3)` in `cleanNovel`, and "up to 3" in the prompt). Raise to 7 — bump the slice, update the prompt wording, and confirm `max_tokens` (now 2000) is enough for 7 ideas with steps. Client `renderNovel` already loops over whatever it's given. → **M6** (extends the suggestions work just shipped).

2. **Trim the basket hint copy.** `basket.hint` ends with "Painon mukaan myytäviä ei voi laskea automaattisesti, joten ne alkavat pieninä — aseta kerran ja ne pysyvät." Per a code read on 2026‑09‑08 this is still *technically* accurate (quantities are stored as whole pieces in `standard_basket.default_qty` and reused; there is no automatic kg→piece conversion), but the sentence is long and confusing. Shorten the hint and drop this clause. Confirm with M whether behaviour changed before deciding it's outright false. → **M8** (copy/polish) or a quick standalone fix.

3. **Group the basket into store sub-categories.** Section the weekly basket under S‑kaupat's own headings, in this order: HEDELMÄT JA VIHANNEKSET · HILLOT JA SÄILYKKEET · JUOMAT · KAHVIT, TEET JA MEHUT · KODINHOITO JA TALOUSTARVIKKEET · KUIVATUOTTEET JA LEIVONTA · LEIVÄT JA LEIVONNAISET · LIHA JA KASVIPROTEIINIT · MAITO, MUNAT JA RASVAT · PAKASTEET · PASTAT, RIISIT JA NUUDELIT · SNACKSIT · MUUT. Needs a `category` on `products` (nullable, default → MUUT), a way to assign it (manual edit now; ideally auto-classified from S‑kaupat's own category on the receipt during ingest, or a one-time LLM/keyword pass), and grouped rendering in `renderBasket` + the copy-to-clipboard output. The copy order should follow this list so the pasted list matches store aisles. → **M7/M8** (data model + basket UI; the ingest-side auto-classification is the bigger piece).

4. **Filter the basket by who added it (user(s) / auto).** `standard_basket.added_by` already records the adder (`ME`), and auto/suggested items are distinguishable. Add a filter control (chips: everyone / me / partner / auto) over `renderBasket`. Small, mostly client-side. → **M8**, or bundle with #3 since both touch `renderBasket`.

5. **Expand a recipe from the basket to see its steps.** A "+" on basket rows that came from a recipe, opening the recipe's short steps (the `steps` field added in M6.2). Requires tracking which basket items trace to which recipe/plan (the menu→basket flow in `loadMenuSection`); today the standing basket is product-centric and doesn't keep the recipe link per item, so this needs that provenance wired through. → **M6/M7** (depends on the menu→basket provenance).

---

## Cross‑cutting foundations (introduce during M5–M8, not a separate milestone)

- **Schema in repo** from now on; migrations directory is the source of truth (C5).
- **Three cheap tests** (review §4): anonymized `.eml` fixtures + pytest for `parse()`/`reconciles()`; a node test for `normKey`/`parseQty`/`parseUnit`/`monday` once they're modules; the i18n key‑coverage script.
- **Move multi‑statement writes into Postgres functions** (replace recipe ingredients, replace order items, get‑or‑create product) for transactions + one identity rule (S4/S5).

---

## Appendix — full review triage

**CRITICAL:** C1 ingest 409 crash · C2 `cap()` no escape / self‑XSS · C3 unchecked write results · C4 `monday()` TZ bug · C5 schema not in repo.

**SHOULD‑FIX:** S1 `loadMenuSection` race + depends on `basket` · S2 slot numbering collides · S3 menu qty loses unit · S4 recipe‑ingredients delete+reinsert orphans optionals, non‑transactional · S5 two product‑identity rules · S6 `q()` retry weaker than claimed · S7 edge fn trusts `plan_id`, JWT gate not in repo · S8 LLM output under‑validated · S9 note/lunches never sent to LLM · S10 `missing_items.resolution` ignored · S11 language switch refetches everything · S12 README stale · S13 ingest robustness (fetch header before body, unstable fallback id, pinned deps, rename `parser.py`, alert on template change).

**NICE‑TO‑HAVE:** N1 `toast` shadows `t` + add ESLint · N2 i18n one‑line dict / SV empty‑but‑selectable / hard‑wired locale · N3 Easter window wrong · N4 `normKey`/`parseUnit` unit lists differ · N5 two sources of truth for ingredient→product · N6 `is_pantry` unsettable · N7 dash "20+" is a limit not a count · N8 stepper unthrottled PATCH · N9 order drill‑down sets `loaded` before fetch · N10 `product_frequency` recomputed 3× per boot · N11 `products.merged_into` not honored by client · N12 no SRI on CDN, duplicate CSS, hand‑stamped build · N13 `onAuthStateChange`→`loadAll` sync footgun; sign‑out leaves `loaded` true · N14 suggestions filter permanently excludes frequent non‑basket items.

**Architecture:** split the file (no bundler); add the three cheap tests; move multi‑statement writes into Postgres functions; treat migrations as source of truth; add `config.toml`; retire the regex i18n build.
