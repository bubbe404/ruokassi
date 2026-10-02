"""
Tests for missing-item handling in upsert_order: re-ingesting a receipt must not
reopen items already resolved in the app. No database, no secrets.

    python3 ingest/test_missing.py
"""
from supa import Supa


class FakeSupa(Supa):
    def __init__(self, existing):
        self.existing = existing          # rows currently in missing_items
        self.posted = []
        self._product_cache = {'MAITO': 1, 'LEIPÄ': 2, 'KAHVI': 3}

    def _get(self, path, params):
        if path == 'missing_items':
            return [dict(r) for r in self.existing]
        raise AssertionError('unexpected GET ' + path)

    def _post(self, path, rows, prefer=''):
        self.posted.append((path, rows))
        return []

    def _delete(self, path, params):
        if path == 'missing_items':
            self.existing = []


def order(missing):
    return {'order_id': '42', 'order_date': '2026-10-01', 'receipt_type': 'final',
            'order_total_eur': 0, 'num_food_items': 0, 'sum_net_eur': 0,
            'order_discount_eur': 0, 'items': [],
            'missing_items': [{'name': n, 'qty': 1} for n in missing]}


def main():
    s = FakeSupa([{'product_name_raw': 'MAITO', 'resolution': 'handled'}])
    s.upsert_order(order(['MAITO', 'LEIPÄ']))
    rows = [r for path, rr in s.posted if path == 'missing_items' for r in rr]
    got = {r['product_name_raw']: r['resolution'] for r in rows}
    assert got == {'MAITO': 'handled', 'LEIPÄ': 'open'}, got
    print('ok   re-ingest keeps "handled", new item opens')

    s = FakeSupa([])
    s.upsert_order(order(['KAHVI']))
    rows = [r for path, rr in s.posted if path == 'missing_items' for r in rr]
    assert [r['resolution'] for r in rows] == ['open'], rows
    print('ok   first ingest opens every missing item')
    print('\nmissing-item rules hold')


if __name__ == '__main__':
    main()
