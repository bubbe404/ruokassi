-- 0005_missing_items_resolution.sql
-- M7 / review S10: "Missing from last order" must clear once dealt with.
--
-- resolution lifecycle:
--   open       noted on a receipt, still to deal with (default)
--   handled    a person ticked it off in the app (bought in store / not needed)
--   reordered  closed automatically: a later receipt contained the product
--   expired    closed automatically: a later receipt arrived without it
--
-- The app may change ONLY the resolution column, and only to handled/open.
-- The legacy values from 0001 (bought_at_pickup/skipped/substituted) stay allowed;
-- nothing writes them today.

alter table public.missing_items
  drop constraint if exists missing_items_resolution_check;
alter table public.missing_items
  add constraint missing_items_resolution_check
  check (resolution in ('open', 'handled', 'reordered', 'expired',
                        'bought_at_pickup', 'skipped', 'substituted'));

-- column-level write access for signed-in users; nothing for anon.
-- (INSERT/DELETE stay blocked by RLS: there are no policies for them.)
revoke update on public.missing_items from anon, authenticated;
grant update (resolution) on public.missing_items to authenticated;

drop policy if exists resolve_auth on public.missing_items;
create policy resolve_auth on public.missing_items
  for update to authenticated
  using (true)
  with check (resolution in ('open', 'handled'));

-- Called by the ingest job after a new receipt is stored: every still-open
-- missing item from an EARLIER order is closed, as 'reordered' when the new
-- receipt contains that product and 'expired' otherwise. Items noted on the new
-- receipt itself stay open. Idempotent.
create or replace function public.close_superseded_missing(p_order_id text)
returns integer
language sql
security invoker
set search_path = public
as $$
  with cur as (
    select order_date from orders where order_id = p_order_id
  ), upd as (
    update missing_items m
       set resolution = case
             when m.product_id is not null and exists (
               select 1 from order_items oi
                where oi.order_id = p_order_id and oi.product_id = m.product_id)
             then 'reordered' else 'expired' end
     where m.resolution = 'open'
       and m.order_id <> p_order_id
       and m.order_id in (select o.order_id from orders o, cur
                           where o.order_date <= cur.order_date)
    returning 1
  )
  select count(*)::int from upd;
$$;

revoke execute on function public.close_superseded_missing(text) from public, anon, authenticated;
grant execute on function public.close_superseded_missing(text) to service_role;

-- One-time backfill: close everything superseded by the newest receipt.
-- (Applied 2026-10-02 against order 1421214555 of 2026-09-30: closed 109 of 111.)
select public.close_superseded_missing(
  (select order_id from public.orders order by order_date desc, created_at desc limit 1));
