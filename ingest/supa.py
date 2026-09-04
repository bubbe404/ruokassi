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
