-- 0003_household_prefs.sql
-- M6 (smarter suggestions): a single free-text household food-preferences note
-- (likes / dislikes / avoid) fed into the weekly LLM suggestion prompt. One row
-- for the whole household (2-user app), pinned at id = 1.
create table if not exists public.household_prefs (
  id smallint primary key default 1,
  food_prefs text,
  updated_at timestamptz not null default now(),
  constraint household_prefs_singleton check (id = 1)
);

alter table public.household_prefs enable row level security;
drop policy if exists rw on public.household_prefs;
create policy rw on public.household_prefs for all to authenticated using (true) with check (true);

insert into public.household_prefs (id, food_prefs) values (1, null)
  on conflict (id) do nothing;
