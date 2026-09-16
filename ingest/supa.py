"""
Thin Supabase REST client for the ingestion job.

Writes go through the service_role key (bypasses RLS) — it lives only in the
environment (GitHub Actions secret) and is never logged. All operations are
idempotent per order so re-runs are safe.
"""
import os
import requests

URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
KEY = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')


# How the standing basket learns from a receipt: a product bought in at least
# RECUR_MIN of the last RECUR_WINDOW orders is treated as a staple worth carrying.
# 6/8 = "most weeks". 4/8 was tried first and would have adopted 15 items in one
# go, nearly doubling the basket; the due-to-reorder list already surfaces the
# half-the-time items when they are actually due, so the basket does not need them.
RECUR_WINDOW = 8
RECUR_MIN = 6


class BasketRefresh:
    """What refresh_basket did, so the caller can log it."""

    def __init__(self):
        self.removed = []     # (name, added_by, added_at)
        self.requantified = []  # (name, old_qty, new_qty)
        self.added = []       # (name, qty, seen_in_n_orders)
        self.skipped = None   # reason, when the refresh did not run


class Supa:
    def __init__(self, url=URL, key=KEY):
        if not url or not key:
            raise SystemExit('SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set')
        self.base = f'{url}/rest/v1'
        self.s = requests.Session()
        self.s.headers.update({
            'apikey': key,
            'Authorization': f'Bearer {key}',
            'Content-Type': 'application/json',
        })
        self._product_cache = {}  # raw_name -> product_id

    # -- low level ---------------------------------------------------------
    def _get(self, path, params):
        r = self.s.get(f'{self.base}/{path}', params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def _post(self, path, rows, prefer='return=representation'):
        r = self.s.post(f'{self.base}/{path}', json=rows,
                        headers={'Prefer': prefer}, timeout=60)
        r.raise_for_status()
        return r.json() if r.text else []

    def _delete(self, path, params):
        r = self.s.delete(f'{self.base}/{path}', params=params, timeout=30)
        r.raise_for_status()

    def _patch(self, path, params, row):
        r = self.s.patch(f'{self.base}/{path}', params=params, json=row,
                         headers={'Prefer': 'return=minimal'}, timeout=30)
        r.raise_for_status()

    # -- products (identity-first, learn-as-you-go) ------------------------
    def product_id(self, raw_name):
        if raw_name in self._product_cache:
            return self._product_cache[raw_name]
        found = self._get('product_aliases',
                          {'raw_name': f'eq.{raw_name}', 'select': 'product_id'})
        if found:
            pid = found[0]['product_id']
        else:
            # on_conflict=name makes this an UPSERT: a pre-existing name returns
            # the existing row instead of raising a 409 unique-violation (which
            # would abort the whole run). Reached whenever a product exists with
            # no alias yet — e.g. one the app created via "add as new" / a free
            # basket item, then later seen on a receipt.
            prod = self._post('products?on_conflict=name', {'name': raw_name},
                              prefer='return=representation,resolution=merge-duplicates')
            if isinstance(prod, list):
                prod = prod[0] if prod else None
            if not prod:  # representation empty for some reason: fetch by name
                rows = self._get('products', {'name': f'eq.{raw_name}', 'select': 'id'})
                prod = rows[0] if rows else None
            if not prod:
                raise RuntimeError(f'could not get-or-create product {raw_name!r}')
            pid = prod['id']
            # bind the alias (ignore if it already exists)
            try:
                self._post('product_aliases',
                           {'raw_name': raw_name, 'product_id': pid, 'source': 'auto'},
                           prefer='resolution=ignore-duplicates,return=minimal')
            except requests.HTTPError:
                pass
        self._product_cache[raw_name] = pid
        return pid

    # -- dedupe ------------------------------------------------------------
    def email_done(self, message_id):
        if not message_id:
            return False
        rows = self._get('ingested_emails',
                         {'message_id': f'eq.{message_id}', 'parsed_ok': 'eq.true',
                          'select': 'message_id'})
        return bool(rows)

    def record_email(self, message_id, subject, received_at, receipt_type,
                     order_id, raw_text, parsed_ok, error=None):
        self._post('ingested_emails', {
            'message_id': message_id, 'subject': subject, 'received_at': received_at,
            'receipt_type': receipt_type, 'order_id': order_id, 'raw_text': raw_text,
            'parsed_ok': parsed_ok, 'error': error,
        }, prefer='resolution=merge-duplicates,return=minimal')

    # -- the order (idempotent replace) ------------------------------------
    def upsert_order(self, p, source_message_id=None):
        self._post('orders', {
            'order_id': p['order_id'], 'order_date': p['order_date'],
            'receipt_type': p['receipt_type'], 'order_total_eur': p['order_total_eur'],
            'num_food_items': p['num_food_items'], 'sum_net_eur': p['sum_net_eur'],
            'order_discount_eur': p['order_discount_eur'],
            'source_message_id': source_message_id,
        }, prefer='resolution=merge-duplicates,return=minimal')

        self._delete('order_items', {'order_id': f'eq.{p["order_id"]}'})
        rows = []
        for it in p['items']:
            pid = None if it['is_fee'] else self.product_id(it['product_name_raw'])
            rows.append({
                'order_id': p['order_id'], 'line_no': it['line_no'],
                'item_date': p['order_date'], 'product_name_raw': it['product_name_raw'],
                'product_id': pid, 'qty': it['qty'], 'unit': it['unit'],
                'unit_price_eur': it['unit_price_eur'], 'gross_eur': it['gross_eur'],
                'discount_eur': it['discount_eur'], 'net_eur': it['net_eur'],
                'is_fee': it['is_fee'],
            })
        if rows:
            self._post('order_items', rows, prefer='return=minimal')

        self._delete('missing_items', {'order_id': f'eq.{p["order_id"]}'})
        mrows = [{
            'order_id': p['order_id'], 'product_name_raw': m['name'],
            'product_id': self.product_id(m['name']), 'qty': m['qty'],
            'resolution': 'open',
        } for m in p['missing_items']]
        if mrows:
            self._post('missing_items', mrows, prefer='return=minimal')

    # -- the standing basket, after a receipt -------------------------------
    def refresh_basket(self, order_id):
        """Align the standing basket with what was actually bought, and drop the
        previous week's one-off manual additions.

        Only ever runs for the newest order in the database: a backfill of older
        receipts must not rewrite the basket with stale quantities. Re-running on
        the same receipt is a no-op, so a retried job is harmless.
        """
        out = BasketRefresh()

        orders = self._get('orders', {'select': 'order_id,order_date',
                                      'order': 'order_date.desc', 'limit': RECUR_WINDOW})
        if not orders:
            out.skipped = 'no orders'
            return out
        if orders[0]['order_id'] != str(order_id):
            out.skipped = f'{order_id} is not the newest order ({orders[0]["order_id"]})'
            return out
        order_date = orders[0]['order_date']

        # what this receipt contained, and how each product is sold (fees are not products)
        units = {}
        for ln in self._get('order_items', {'select': 'product_id,unit',
                                            'order_id': f'eq.{order_id}', 'is_fee': 'eq.false'}):
            pid = ln.get('product_id')
            if pid is not None:
                units[pid] = (ln.get('unit') or '').lower()

        # Quantities come from the median of recent orders, not this one receipt: a week
        # where something was out of stock or substituted must not permanently drop a
        # standing amount. This is the same median the app already shows in the due list.
        median, excluded = {}, set()
        for row in self._get('product_frequency',
                             {'select': 'product_id,median_qty,exclude_from_reorder'}):
            pid = row['product_id']
            if row.get('exclude_from_reorder'):
                excluded.add(pid)
            if row.get('median_qty') is not None:
                median[pid] = float(row['median_qty'])

        basket = self._get('standard_basket', {'select': 'product_id,default_qty,added_by,added_at'})
        names = self._product_names([r['product_id'] for r in basket] + list(units))

        # 1. drop manual additions made before this order — they were one-offs for the week
        #    just ordered for. Anything genuinely recurring returns via the due list.
        keep = []
        for r in basket:
            by = (r.get('added_by') or 'auto')
            added = (r.get('added_at') or '')[:10]
            if by != 'auto' and added and added < order_date:
                self._delete('standard_basket', {'product_id': f'eq.{r["product_id"]}'})
                out.removed.append((names.get(r['product_id'], r['product_id']), by, added))
            else:
                keep.append(r)

        # 2. correct the quantity of anything on this receipt — pieces only. Weight-based
        #    lines are kilos, so a median of 0.48 would overwrite a deliberate setting with
        #    1; the basket hint promises those stay as they were set.
        in_basket = set()
        for r in keep:
            pid = r['product_id']
            in_basket.add(pid)
            if pid not in units or units[pid] != 'kpl' or pid not in median:
                continue
            new_qty = max(1, int(round(median[pid])))
            if new_qty != float(r.get('default_qty') or 0):
                self._patch('standard_basket', {'product_id': f'eq.{pid}'}, {'default_qty': new_qty})
                out.requantified.append((names.get(pid, pid), r.get('default_qty'), new_qty))

        # 3. adopt anything on this receipt that has become a staple. exclude_from_reorder
        #    keeps non-products out — KERÄILY and PANTTI are ordinary rows in `products`
        #    and would otherwise qualify on frequency alone.
        freq = self._order_counts([o['order_id'] for o in orders])
        for pid in units:
            if pid in in_basket or pid in excluded or freq.get(pid, 0) < RECUR_MIN:
                continue
            qty = max(1, int(round(median[pid]))) if units[pid] == 'kpl' and pid in median else 1
            self._post('standard_basket',
                       {'product_id': pid, 'default_qty': qty, 'added_by': 'auto'},
                       prefer='resolution=merge-duplicates,return=minimal')
            out.added.append((names.get(pid, pid), qty, freq.get(pid, 0)))
        return out

    def _product_names(self, ids):
        ids = sorted({i for i in ids if i is not None})
        if not ids:
            return {}
        out = {}
        for i in range(0, len(ids), 200):
            chunk = ids[i:i + 200]
            for row in self._get('products', {'select': 'id,name',
                                              'id': f'in.({",".join(str(x) for x in chunk)})'}):
                out[row['id']] = row['name']
        return out

    def _order_counts(self, order_ids):
        """How many of these orders contained each product."""
        if not order_ids:
            return {}
        quoted = ','.join(f'"{o}"' for o in order_ids)
        rows = self._get('order_items', {'select': 'order_id,product_id',
                                         'order_id': f'in.({quoted})', 'is_fee': 'eq.false'})
        seen = {}
        for r in rows:
            pid = r.get('product_id')
            if pid is None:
                continue
            seen.setdefault(pid, set()).add(r['order_id'])
        return {pid: len(v) for pid, v in seen.items()}

