-- 0006_store_buys_and_dismissals.sql
-- M, 2026-10-02: refine "Puuttui" and "Loppumassa, ei korissa" in the basket.
--
-- 1. A missing item gets two outcomes in the app instead of one "handled":
--      ✓ bought in store  -> resolution 'bought_at_pickup'
--      ✕ not needed       -> resolution 'skipped'
--    (both values exist since 0001). 'handled' stays valid for rows ticked
--    before this change.
-- 2. A store buy counts as a purchase in product_frequency, dated to the order
--    it was missing from, so due-to-reorder timing treats it as bought.
--    Catch: the "missing" section of a receipt uses the long shop name
--    ("Oululainen Hapankorppu 370g") while the item lines use the short one
--    ("HAPANKORPPU"), so no missing item has ever pointed at a product with
--    purchase history (0 of 111). guess_receipt_products() proposes matches and
--    link_missing_product() records the person's pick once, as an alias, so the
--    ingest job maps that long name correctly from then on.
-- 3. suggestion_dismissals: ✕ on a "due, not in basket" row hides it for the
--    current planning week (Monday-based, like meal_plans.week_start).

alter policy resolve_auth on public.missing_items
  with check (resolution in ('open', 'handled', 'bought_at_pickup', 'skipped'));

create or replace view public.product_frequency with (security_invoker = true) as
 WITH items AS (
         SELECT COALESCE(p.merged_into, p.id) AS canonical_id,
            oi.order_id,
            o.order_date,
            oi.qty
           FROM order_items oi
             JOIN orders o ON o.order_id = oi.order_id
             JOIN products p ON p.id = oi.product_id
          WHERE oi.is_fee = false AND oi.product_id IS NOT NULL
        UNION ALL
         -- bought in the store after the online order came without it
         SELECT COALESCE(p.merged_into, p.id) AS canonical_id,
            m.order_id,
            o.order_date,
            COALESCE(m.qty, 1::numeric) AS qty
           FROM missing_items m
             JOIN orders o ON o.order_id = m.order_id
             JOIN products p ON p.id = m.product_id
          WHERE m.resolution = 'bought_at_pickup'
        ), per_order AS (
         SELECT items.canonical_id,
            items.order_date,
            sum(items.qty) AS qty
           FROM items
          GROUP BY items.canonical_id, items.order_date
        ), gaps AS (
         SELECT per_order.canonical_id,
            per_order.order_date,
            per_order.order_date - lag(per_order.order_date) OVER (PARTITION BY per_order.canonical_id ORDER BY per_order.order_date) AS gap_days
           FROM per_order
        ), agg AS (
         SELECT g.canonical_id,
            count(DISTINCT g.order_date) AS orders_with_item,
            percentile_cont(0.5::double precision) WITHIN GROUP (ORDER BY (g.gap_days::double precision)) AS median_days_between,
            max(g.order_date) AS last_ordered
           FROM gaps g
          GROUP BY g.canonical_id
        ), recent AS (
         SELECT per_order.canonical_id,
            per_order.qty,
            row_number() OVER (PARTITION BY per_order.canonical_id ORDER BY per_order.order_date DESC) AS rn
           FROM per_order
        ), qtys AS (
         SELECT recent.canonical_id,
            percentile_cont(0.5::double precision) WITHIN GROUP (ORDER BY (recent.qty::double precision)) AS median_qty
           FROM recent
          WHERE recent.rn <= 12
          GROUP BY recent.canonical_id
        ), totals AS (
         SELECT count(*)::numeric AS n_orders
           FROM orders
        )
 SELECT pc.id AS product_id,
    pc.name AS product,
    pc.is_staple,
    pc.exclude_from_reorder,
    a.orders_with_item,
    round(a.orders_with_item::numeric / NULLIF(t.n_orders, 0::numeric), 3) AS pct_of_orders,
    a.median_days_between,
    q.median_qty,
    a.last_ordered,
    CURRENT_DATE - a.last_ordered AS days_since_last,
    round((CURRENT_DATE - a.last_ordered)::numeric / NULLIF(a.median_days_between::numeric, 0::numeric), 2) AS due_ratio
   FROM agg a
     JOIN products pc ON pc.id = a.canonical_id
     JOIN qtys q ON q.canonical_id = a.canonical_id
     CROSS JOIN totals t
  ORDER BY a.orders_with_item DESC;

create table if not exists public.suggestion_dismissals (
  product_id   bigint not null references public.products(id) on delete cascade,
  week_start   date   not null,
  dismissed_by text,
  created_at   timestamptz not null default now(),
  primary key (product_id, week_start)
);
alter table public.suggestion_dismissals enable row level security;
create policy dismiss_read on public.suggestion_dismissals for select to authenticated using (true);
create policy dismiss_add  on public.suggestion_dismissals for insert to authenticated with check (true);
create policy dismiss_undo on public.suggestion_dismissals for delete to authenticated using (true);

-- Best guesses: receipt products (ones with order lines) whose every word occurs
-- in the long name. Longest name first, then most often bought.
create or replace function public.guess_receipt_products(p_name text)
returns table (id bigint, name text, times_bought bigint)
language sql stable security invoker set search_path = public as $$
  select p.id, p.name, count(*) as times_bought
    from products p join order_items oi on oi.product_id = p.id and not oi.is_fee
   where p.merged_into is null
     and not exists (
       select 1 from unnest(regexp_split_to_array(upper(p.name), '[\s,]+')) w
        where length(w) > 0 and position(w in upper(p_name)) = 0)
   group by p.id, p.name
   order by length(p.name) desc, count(*) desc
   limit 3;
$$;
grant execute on function public.guess_receipt_products(text) to authenticated;

-- Point every missing item with this long name at the chosen product, and
-- remember the long name as an alias for future receipts.
create or replace function public.link_missing_product(p_missing_id bigint, p_product_id bigint)
returns void
language plpgsql security definer set search_path = public as $$
declare v_raw text;
begin
  if auth.uid() is null then raise exception 'not signed in'; end if;
  select product_name_raw into v_raw from missing_items where id = p_missing_id;
  if v_raw is null then raise exception 'no such missing item'; end if;
  if not exists (select 1 from products where id = p_product_id) then
    raise exception 'no such product';
  end if;
  update missing_items set product_id = p_product_id where product_name_raw = v_raw;
  insert into product_aliases (raw_name, product_id, source)
       values (v_raw, p_product_id, 'manual')
  on conflict (raw_name) do update set product_id = excluded.product_id, source = 'manual';
end $$;
revoke execute on function public.link_missing_product(bigint, bigint) from public, anon;
grant execute on function public.link_missing_product(bigint, bigint) to authenticated;
