-- 0007_missing_added_to_basket.sql
-- M, 2026-10-02: a "+" on a Puuttui row puts the item in next week's basket
-- (standard_basket) and closes the missing item as 'added_to_basket'.
-- Not a purchase: product_frequency is unaffected; the next receipt that
-- contains it counts as usual.

alter table public.missing_items
  drop constraint if exists missing_items_resolution_check;
alter table public.missing_items
  add constraint missing_items_resolution_check
  check (resolution in ('open', 'handled', 'reordered', 'expired',
                        'bought_at_pickup', 'skipped', 'substituted', 'added_to_basket'));

alter policy resolve_auth on public.missing_items
  with check (resolution in ('open', 'handled', 'bought_at_pickup', 'skipped', 'added_to_basket'));
