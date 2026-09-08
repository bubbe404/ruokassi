-- 0004_recipe_method_time.sql
-- M6.2 (recipe method & time): a brief idea of what to DO with a recipe, not
-- just its ingredients. time_min = total minutes, method = short free-text
-- cooking method (stored in the user's language, like names/ingredients),
-- steps = a few short steps, newline-separated. All optional / nullable.
alter table public.recipes add column if not exists time_min integer;
alter table public.recipes add column if not exists method text;
alter table public.recipes add column if not exists steps text;
