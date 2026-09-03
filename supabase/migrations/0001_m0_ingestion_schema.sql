-- Ruokassi M0: ingestion schema (orders, items, products, missing items) + reorder view + RLS
-- Applied to the Supabase project on 2026-09-03.

create table products (
  id            bigint generated always as identity primary key,
  name          text not null unique,
  is_staple     boolean not null default false,
  exclude_from_reorder boolean not null default false,
  default_qty   numeric,
  notes         text,
  merged_into   bigint references products(id),
  created_at    timestamptz not null default now()
);

create table product_aliases (
  raw_name    text primary key,
  product_id  bigint not null references products(id),
  source      text not null default 'auto' check (source in ('auto','merge','manual')),
  created_at  timestamptz not null default now()
);
create index on product_aliases (product_id);

create table ingested_emails (
  message_id  text primary key,
  subject     text,
  received_at timestamptz,
  receipt_type text,
  order_id    text,
  raw_text    text,
  parsed_ok   boolean not null default false,
  error       text,
  parsed_at   timestamptz not null default now()
);

create table orders (
  order_id          text primary key,
  order_date        date,
  ordered_at        timestamptz,
  pickup_at         timestamptz,
  receipt_type      text,
  order_total_eur   numeric,
  num_food_items    integer,
  sum_net_eur       numeric,
  order_discount_eur numeric,
  is_july_atypical  boolean not null default false,
  source_message_id text,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);
create index on orders (order_date);

create table order_items (
  id              bigint generated always as identity primary key,
  order_id        text not null references orders(order_id) on delete cascade,
  line_no         integer,
  item_date       date,
  product_name_raw text not null,
  product_id      bigint references products(id),
  qty             numeric,
  unit            text,
  unit_price_eur  numeric,
  gross_eur       numeric,
  discount_eur    numeric,
  net_eur         numeric,
  is_fee          boolean not null default false
);
create index on order_items (order_id);
create index on order_items (product_id);
create index on order_items (item_date);

create table missing_items (
  id              bigint generated always as identity primary key,
  order_id        text not null references orders(order_id) on delete cascade,
  product_name_raw text not null,
  product_id      bigint references products(id),
  qty             numeric,
  resolution      text not null default 'open' check (resolution in ('open','bought_at_pickup','skipped','substituted')),
  noted_at        timestamptz not null default now()
);
create index on missing_items (order_id);

-- Reorder engine: per canonical product, cadence + due ratio
create view product_frequency with (security_invoker = true) as
with items as (
  select coalesce(p.merged_into, p.id) as canonical_id,
         oi.order_id, o.order_date, oi.qty
  from order_items oi
  join orders o on o.order_id = oi.order_id
  join products p on p.id = oi.product_id
  where oi.is_fee = false and oi.product_id is not null
),
per_order as (
  select canonical_id, order_date, sum(qty) as qty
  from items group by canonical_id, order_date
),
gaps as (
  select canonical_id, order_date,
         (order_date - lag(order_date) over (partition by canonical_id order by order_date)) as gap_days
  from per_order
),
agg as (
  select g.canonical_id,
         count(distinct g.order_date)                              as orders_with_item,
         percentile_cont(0.5) within group (order by g.gap_days)   as median_days_between,
         max(g.order_date)                                          as last_ordered
  from gaps g group by g.canonical_id
),
qtys as (
  select canonical_id, percentile_cont(0.5) within group (order by qty) as median_qty
  from per_order group by canonical_id
),
totals as ( select count(*)::numeric as n_orders from orders )
select pc.id                                    as product_id,
       pc.name                                  as product,
       pc.is_staple,
       pc.exclude_from_reorder,
       a.orders_with_item,
       round(a.orders_with_item::numeric / nullif(t.n_orders,0), 3) as pct_of_orders,
       a.median_days_between,
       q.median_qty,
       a.last_ordered,
       (current_date - a.last_ordered)          as days_since_last,
       round((current_date - a.last_ordered)::numeric / nullif(a.median_days_between::numeric,0), 2) as due_ratio
from agg a
join products pc on pc.id = a.canonical_id
join qtys q on q.canonical_id = a.canonical_id
cross join totals t
order by a.orders_with_item desc;

alter table products        enable row level security;
alter table product_aliases enable row level security;
alter table ingested_emails enable row level security;
alter table orders          enable row level security;
alter table order_items     enable row level security;
alter table missing_items   enable row level security;

create policy read_auth on products        for select to authenticated using (true);
create policy read_auth on product_aliases for select to authenticated using (true);
create policy read_auth on ingested_emails for select to authenticated using (true);
create policy read_auth on orders          for select to authenticated using (true);
create policy read_auth on order_items     for select to authenticated using (true);
create policy read_auth on missing_items   for select to authenticated using (true);
