"""
Tests for refresh_basket — the one piece of the ingest job that DELETES data the
family typed in, so its rules are worth pinning down.

No database and no secrets: a Supa instance is built without __init__ and its four
HTTP helpers are replaced with in-memory fixtures.

    python3 ingest/test_basket.py
"""
import sys
from supa import Supa, RECUR_MIN


class FakeSupa(Supa):
    def __init__(self, orders, items, basket, products, freq_rows):
        self.orders, self.items, self.basket, self.products = orders, items, basket, products
        self.freq_rows = freq_rows
        self.deleted, self.patched, self.posted = [], [], []
        self._product_cache = {}

    def _get(self, path, params):
        if path == 'orders':
            return self.orders[:int(params.get('limit', 99))]
        if path == 'order_items':
            if 'order_id' in params and params['order_id'].startswith('eq.'):
                oid = params['order_id'][3:]
                return [i for i in self.items if i['order_id'] == oid]
            raw = params['order_id'][len('in.('):-1]
            ids = {x.strip().strip('"') for x in raw.split(',')}
            return [{'order_id': i['order_id'], 'product_id': i['product_id']}
                    for i in self.items if i['order_id'] in ids]
        if path == 'standard_basket':
            return [dict(b) for b in self.basket]
        if path == 'products':
            return [{'id': k, 'name': v} for k, v in self.products.items()]
        if path == 'product_frequency':
            return self.freq_rows
        raise AssertionError('unexpected GET ' + path)

    def _delete(self, path, params):
        self.deleted.append(int(params['product_id'][3:]))

    def _patch(self, path, params, row):
        self.patched.append((int(params['product_id'][3:]), row['default_qty']))

    def _post(self, path, rows, prefer=None):
        self.posted.append((rows['product_id'], rows['default_qty'], rows['added_by']))
        return []


def build(**over):
    orders = [{'order_id': 'B', 'order_date': '2026-09-16'}] + \
             [{'order_id': f'A{i}', 'order_date': f'2026-09-{9 - i:02d}'} for i in range(7)]
    # product 1 in every order (staple), product 9 in 3 of them (not yet a staple)
    items = []
    for o in orders:
        items += [{'order_id': o['order_id'], 'product_id': 1, 'qty': 2, 'unit': 'kpl'}]
    for o in orders[:3]:
        items += [{'order_id': o['order_id'], 'product_id': 9, 'qty': 1, 'unit': 'kpl'}]
    for o in orders:
        items += [{'order_id': o['order_id'], 'product_id': 7, 'qty': 1, 'unit': 'kpl'}]  # KERÄILY-alike
    items += [
        {'order_id': 'B', 'product_id': 2, 'qty': 3, 'unit': 'kpl'},    # receipt says 3, median says 5
        {'order_id': 'B', 'product_id': 3, 'qty': 0.48, 'unit': 'kg'},  # in basket, weight-based
        {'order_id': 'B', 'product_id': 4, 'qty': 1, 'unit': 'kpl'},    # manual + bought
    ]
    freq_rows = [
        {'product_id': 1, 'median_qty': 2, 'exclude_from_reorder': False},
        {'product_id': 2, 'median_qty': 5, 'exclude_from_reorder': False},
        {'product_id': 3, 'median_qty': 0.5, 'exclude_from_reorder': False},
        {'product_id': 7, 'median_qty': 1, 'exclude_from_reorder': True},   # a non-product
        {'product_id': 9, 'median_qty': 1, 'exclude_from_reorder': False},
    ]
    basket = [
        {'product_id': 2, 'default_qty': 1, 'added_by': 'auto', 'added_at': '2026-09-03T00:00:00Z'},
        {'product_id': 3, 'default_qty': 5, 'added_by': 'auto', 'added_at': '2026-09-03T00:00:00Z'},
        {'product_id': 4, 'default_qty': 1, 'added_by': 'max@x.fi', 'added_at': '2026-09-05T00:00:00Z'},
        {'product_id': 5, 'default_qty': 1, 'added_by': 'max@x.fi', 'added_at': '2026-09-17T00:00:00Z'},
    ]
    products = {i: f'P{i}' for i in range(1, 10)}
    d = dict(orders=orders, items=items, basket=basket, products=products, freq_rows=freq_rows)
    d.update(over)
    return FakeSupa(**d)


def run():
    fails = []
    def check(cond, msg):
        print(('  ok   ' if cond else '  FAIL ') + msg)
        if not cond:
            fails.append(msg)

    print('refresh_basket:')
    s = build()
    r = s.refresh_basket('B')

    check(r.skipped is None, 'runs for the newest order')
    check(4 in s.deleted, 'removes a manual addition made before the order')
    check(5 not in s.deleted, 'keeps a manual addition made AFTER the order (this week)')
    check(2 not in s.deleted and 3 not in s.deleted, 'never removes auto rows')
    check((2, 5) in s.patched, 'sets qty from the MEDIAN (5), not this receipt (3)')
    check(all(pid != 3 for pid, _ in s.patched), 'leaves a weight-based item alone (kg would round to 0/1)')
    check(any(p[0] == 1 for p in s.posted), f'adopts a staple seen in >= {RECUR_MIN} of 8 orders')
    check(all(p[0] != 9 for p in s.posted), 'does not adopt something seen in only 3 of 8')
    check(all(p[2] == 'auto' for p in s.posted), 'adopted rows are marked auto, not attributed to a user')
    check(all(p[0] != 7 for p in s.posted), 'never adopts a product flagged exclude_from_reorder (KERÄILY, PANTTI)')
    check(any(p[0] == 1 and p[1] == 2 for p in s.posted), 'adopts at the median quantity')

    print('guard:')
    s2 = build()
    r2 = s2.refresh_basket('A3')
    check(r2.skipped is not None, 'refuses to run for anything but the newest order')
    check(not s2.deleted and not s2.patched and not s2.posted, 'a backfill of old receipts writes nothing')

    print('idempotence:')
    s3 = build()
    s3.refresh_basket('B')
    first = (len(s3.deleted), len(s3.patched), len(s3.posted))
    s3.basket = [b for b in s3.basket if b['product_id'] not in s3.deleted]
    for pid, q in s3.patched:
        for b in s3.basket:
            if b['product_id'] == pid:
                b['default_qty'] = q
    s3.basket += [{'product_id': p[0], 'default_qty': p[1], 'added_by': 'auto',
                   'added_at': '2026-09-16T00:00:00Z'} for p in s3.posted]
    s3.deleted, s3.patched, s3.posted = [], [], []
    s3.refresh_basket('B')
    check((s3.deleted, s3.patched, s3.posted) == ([], [], []),
          f'a second run on the same receipt changes nothing (first run did {first})')

    print()
    print(f'{len(fails)} failure(s)' if fails else 'all basket rules hold')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(run())
