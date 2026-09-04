#!/usr/bin/env python3
"""
Ruokassi ingestion job.

Reads S-kaupat receipt emails from Gmail (IMAP), parses them deterministically,
upserts orders/items/missing-items into Supabase, and emails the "buy at pickup"
list for any newly-seen out-of-stock items. Runs on a GitHub Actions cron.

Modes
  (default)                 IMAP: fetch receipts newer than --since days, ingest.
  --backfill                IMAP: fetch the full history (no date filter), ingest.
  --backfill-local DIR      Ingest *.eml files from a local directory (no IMAP).
  --dry-run                 Parse + reconcile only. No DB writes, no email.

Environment
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY   (Supabase; service_role bypasses RLS)
  GMAIL_USER, GMAIL_APP_PASSWORD            (IMAP + SMTP; a Google App Password)
  NOTIFY_TO                                 (optional; defaults to GMAIL_USER)

Idempotent: dedupes on the email Message-ID, and re-upserting an order replaces
its items, so re-runs and overlapping windows are safe.
"""
import argparse
import email
import glob
import imaplib
import os
import re
import sys
from datetime import datetime, timedelta
from email import policy

import parser as receipt_parser

IMAP_HOST = 'imap.gmail.com'
GM_QUERY = 'from:s-kaupat.fi subject:kuitti'


def _find_all_mail(M):
    """Locate Gmail's 'All Mail' folder by its \\All special-use flag, which is
    language-independent (a Finnish account names it '[Gmail]/Kaikki viestit').
    Falls back to INBOX."""
    typ, boxes = M.list()
    if typ == 'OK' and boxes:
        for b in boxes:
            line = b.decode(errors='replace')
            if '\\All' in line:
                m = re.search(r'"([^"]+)"\s*$', line)
                if m:
                    return m.group(1)
    return 'INBOX'


def log(*a):
    print(*a, flush=True)


# ---------- IMAP -----------------------------------------------------------
def imap_fetch(since_days=None, backfill=False, limit=None):
    """Yield (message_id, received_at_iso, raw_bytes) for matching receipts."""
    user = os.environ.get('GMAIL_USER'); pw = os.environ.get('GMAIL_APP_PASSWORD')
    if not (user and pw):
        raise SystemExit('GMAIL_USER and GMAIL_APP_PASSWORD must be set')
    M = imaplib.IMAP4_SSL(IMAP_HOST)
    M.login(user, pw)
    try:
        folder = _find_all_mail(M)
        typ, _ = M.select(f'"{folder}"', readonly=True)
        if typ != 'OK':
            typ, _ = M.select('INBOX', readonly=True)
            if typ != 'OK':
                raise SystemExit(f'could not select a mailbox (tried {folder!r} and INBOX)')
        q = GM_QUERY if backfill else f'{GM_QUERY} newer_than:{since_days}d'
        typ, data = M.search(None, 'X-GM-RAW', f'"{q}"')
        if typ != 'OK':
            raise SystemExit(f'IMAP search failed: {typ}')
        ids = data[0].split()
        if limit:
            ids = ids[-limit:]
        log(f'[imap] {len(ids)} matching messages')
        for num in ids:
            typ, md = M.fetch(num, '(RFC822 INTERNALDATE)')
            if typ != 'OK':
                continue
            raw = md[0][1]
            msg = email.message_from_bytes(raw, policy=policy.default)
            mid = (msg['message-id'] or f'imap-{num.decode()}').strip()
            recv = msg['date']
            try:
                recv_iso = email.utils.parsedate_to_datetime(recv).isoformat() if recv else None
            except Exception:
                recv_iso = None
            yield mid, recv_iso, raw
    finally:
        M.logout()


def local_fetch(directory):
    for f in sorted(glob.glob(os.path.join(directory, '*.eml'))):
        raw = open(f, 'rb').read()
        msg = email.message_from_bytes(raw, policy=policy.default)
        mid = (msg['message-id'] or os.path.basename(f)).strip()
        yield mid, None, raw


# ---------- orchestration --------------------------------------------------
def run(args):
    dry = args.dry_run
    supa = None
    if not dry:
        from supa import Supa
        supa = Supa()

    if args.backfill_local:
        source = local_fetch(args.backfill_local)
        send_email = False
    else:
        source = imap_fetch(since_days=args.since, backfill=args.backfill, limit=args.limit)
        send_email = not dry

    n_ok = n_bad = n_skip = n_fail = 0
    new_missing = []   # orders (parsed dicts) newly ingested that have missing items
    failures = []

    for mid, recv_iso, raw in source:
        if supa and supa.email_done(mid):
            n_skip += 1
            continue
        try:
            p = receipt_parser.parse(raw)
        except ValueError as e:
            # not an itemised receipt, or a template we couldn't read
            n_fail += 1
            failures.append((mid, str(e)))
            if supa:
                supa.record_email(mid, None, recv_iso, None, None,
                                  raw.decode('utf-8', 'replace'), False, str(e))
            continue

        reconciled = receipt_parser.reconciles(p)
        if reconciled:
            n_ok += 1
        else:
            n_bad += 1
            log(f'[warn] {p["order_id"]} does not reconcile: '
                f'total={p["order_total_eur"]} net-basket={p["sum_net_eur"]-p["order_discount_eur"]}')

        if not dry:
            # Isolate DB writes per message: one failing order must not abort the
            # whole run (and leave every later receipt unprocessed). On failure we
            # log it and leave the email un-acked (parsed_ok=False) so the next run
            # retries it — upsert_order is idempotent, so a retry is safe.
            try:
                supa.upsert_order(p, source_message_id=mid)
                supa.record_email(mid, p['subject'], recv_iso, p['receipt_type'],
                                  p['order_id'], raw.decode('utf-8', 'replace'), True)
                if p['missing_items']:
                    new_missing.append(p)
            except Exception as e:
                n_fail += 1
                failures.append((mid, f'db write failed: {e}'))
                log(f'[error] {p.get("order_id", "?")} db write failed: {e}')
                try:
                    supa.record_email(mid, p.get('subject'), recv_iso, p.get('receipt_type'),
                                      p.get('order_id'), raw.decode('utf-8', 'replace'),
                                      False, f'db write failed: {e}')
                except Exception:
                    pass
                continue

    log(f'[done] ingested_ok={n_ok} not_reconciled={n_bad} '
        f'skipped_dupes={n_skip} parse_failures={n_fail}')

    # notifications
    if send_email:
        from notify import send, missing_items_body
        for p in new_missing:
            send(f'Ruokassi: {len(p["missing_items"])} item(s) missing — buy at pickup',
                 missing_items_body(p))
        if failures:
            body = 'These S-kaupat emails could not be parsed (stored raw for re-parse):\n\n' + \
                   '\n'.join(f'  {mid}: {err}' for mid, err in failures)
            send(f'Ruokassi: {len(failures)} receipt(s) failed to parse', body)

    # backfill acceptance summary
    if args.backfill or args.backfill_local:
        log(f'[acceptance] orders reconciled: {n_ok}/{n_ok + n_bad}')
    return 1 if n_bad else 0


def main():
    ap = argparse.ArgumentParser(description='Ruokassi receipt ingestion')
    ap.add_argument('--since', type=int, default=14, help='IMAP: days back (daily run)')
    ap.add_argument('--backfill', action='store_true', help='IMAP: full history')
    ap.add_argument('--backfill-local', metavar='DIR', help='ingest *.eml from a folder')
    ap.add_argument('--dry-run', action='store_true', help='parse + reconcile only')
    ap.add_argument('--limit', type=int, help='cap number of messages (testing)')
    sys.exit(run(ap.parse_args()))


if __name__ == '__main__':
    main()
