"""
Deterministic S-kaupat receipt parser.

Input: a raw RFC822 email (bytes) — an S-kaupat receipt whose subject contains
"kuitti". Output: a structured dict with the order, its line items, and the
out-of-stock "Puuttuvat tuotteet" list.

This is a faithful port of the original ingest_receipts.py regex parser, which
reconciles all 86 historical orders to their receipt totals to the cent. The
only additions here are missing-items extraction and returning data instead of
writing CSVs. Do not "improve" the item loop without re-running the
reconciliation test (see ingest.py --dry-run).
"""
import email
import re
from email import policy
from datetime import datetime

from bs4 import BeautifulSoup

FEE_KEYS = ("PAKKAUSMATERIAALIMAKSU", "KERAILYMAKSU", "KERÄILYMAKSU",
            "TOIMITUSMAKSU", "PALVELUMAKSU")

item_re   = re.compile(r'^(.+?)\s{2,}(\d+,\d{2})$')
qty_re    = re.compile(r'^([\d,]+)\s*(KPL|KG)\s+([\d,]+)\s*€/(KPL|KG)$')
norm_re   = re.compile(r'^NORM\.\s+([\d,]+)$')
disc_re   = re.compile(r'^ALENNUS\s+-([\d,]+)$')
basket_re = re.compile(r'^(VERKKOKAUPPA-ALENNUS|TILAUS-ALENNUS|S-ETU\S*|.*KUPONKI.*)\b.*\s-(\d+,\d{2})$')
header_re = re.compile(r'^(KAMPANJA|TARJOUS|PAKETTI)\s{2,}\d+,\d{2}$')
qtyline_re = re.compile(r'^\d+([.,]\d+)?$')

MISSING_HEADER = "Puuttuvat tuotteet"
SUBS_HEADER    = "Korvatut tuotteet"
RECEIPT_STOPS  = {SUBS_HEADER, "Kuitti ostoksistasi", "Tässä kuittisi", "S-KAUPAT"}


def num(s):
    return float(s.replace(' ', '').replace(',', '.'))


def html_text(raw_bytes):
    """Return (subject, message_id, lines[]) from a raw RFC822 email."""
    msg = email.message_from_bytes(raw_bytes, policy=policy.default)
    subject = msg['subject'] or ''
    message_id = (msg['message-id'] or '').strip()
    htmls = [p.get_content() for p in msg.walk() if p.get_content_type() == 'text/html']
    if not htmls:
        return subject, message_id, None
    txt = BeautifulSoup(htmls[0], 'html.parser').get_text("\n", strip=True)
    lines = [l.strip() for l in txt.split("\n") if l.strip()]
    return subject, message_id, lines


def _receipt_type(subject):
    s = subject.lower()
    if 'muutoksia' in s:
        return 'changed'
    if 'maksettu' in s:
        return 'paid'
    if 'ostoksistasi' in s:
        return 'receipt'
    return 'other'


def _parse_missing(lines):
    """Items under 'Puuttuvat tuotteet' (out of stock -> buy at pickup)."""
    try:
        i = lines.index(MISSING_HEADER) + 1
    except ValueError:
        return []
    if i < len(lines) and lines[i] in ('Määrä', 'Maara'):
        i += 1
    out = []
    while i < len(lines):
        name = lines[i]
        if name in RECEIPT_STOPS or name.startswith('TILAUSNRO') or set(name) == {'-'}:
            break
        if i + 1 < len(lines) and qtyline_re.match(lines[i + 1]):
            out.append({'name': name, 'qty': num(lines[i + 1])})
            i += 2
        else:
            break
    return out


def parse(raw_bytes):
    """Parse one receipt email. Returns a dict, or raises ValueError if it is
    not a parseable itemised receipt."""
    subject, message_id, lines = html_text(raw_bytes)
    if lines is None:
        raise ValueError('no HTML body')

    onro_m = re.search(r'TILAUSNRO:\s*(\d+)', "\n".join(lines))
    if not onro_m:
        raise ValueError('no TILAUSNRO (not an itemised receipt)')
    order_id = onro_m.group(1)

    rd = re.search(r'(\d{2}\.\d{2}\.\d{4})\s+\d{2}:\d{2}', "\n".join(lines))
    order_date = datetime.strptime(rd.group(1), '%d.%m.%Y').date().isoformat() if rd else None

    tot = re.search(r'YHTEENSÄ\s+([\d ,]+)\n', "\n".join(lines) + "\n")
    order_total = num(tot.group(1)) if tot else None

    receipt_type = _receipt_type(subject)
    missing = _parse_missing(lines)

    seps = [i for i, l in enumerate(lines) if set(l) == {'-'}]
    if len(seps) < 2:
        raise ValueError('no item block (dashed separators)')
    block = lines[seps[0] + 1:seps[-1]]

    items = []
    i = 0
    sum_net = 0.0
    n_food = 0
    obasket = 0.0
    while i < len(block):
        bm = basket_re.match(block[i])
        if bm:
            obasket += num(bm.group(2)); i += 1; continue
        if norm_re.match(block[i]) or disc_re.match(block[i]) or header_re.match(block[i]):
            i += 1; continue
        m = item_re.match(block[i])
        if not m:
            i += 1; continue
        name = m.group(1).strip(); disp = num(m.group(2))
        qty = 1.0; unit = 'kpl'; uprice = disp; j = i + 1
        if j < len(block):
            q = qty_re.match(block[j])
            if q:
                qty = num(q.group(1)); unit = q.group(2).lower(); uprice = num(q.group(3)); j += 1
        norm = None; disc = 0.0
        while j < len(block):
            nm = norm_re.match(block[j]); dm = disc_re.match(block[j])
            if nm:
                norm = num(nm.group(1)); j += 1
            elif dm:
                disc += num(dm.group(1)); j += 1
            else:
                break
        if norm is not None:
            net = disp; discount = round(norm - disp, 2)
        else:
            net = round(disp - disc, 2); discount = round(disc, 2)
        gross = round(net + discount, 2)
        is_fee = any(k in name for k in FEE_KEYS)
        items.append({
            'line_no': len(items) + 1, 'product_name_raw': name, 'qty': qty,
            'unit': unit, 'unit_price_eur': uprice, 'gross_eur': gross,
            'discount_eur': discount, 'net_eur': net, 'is_fee': is_fee,
        })
        sum_net += net
        if not is_fee:
            n_food += 1
        i = j

    return {
        'message_id': message_id,
        'subject': subject,
        'order_id': order_id,
        'order_date': order_date,
        'receipt_type': receipt_type,
        'order_total_eur': order_total,
        'num_food_items': n_food,
        'sum_net_eur': round(sum_net, 2),
        'order_discount_eur': round(obasket, 2),
        'items': items,
        'missing_items': missing,
    }


def reconciles(parsed, tol=0.05):
    """True if sum_net - basket_discount == order_total within tolerance."""
    if parsed['order_total_eur'] is None:
        return True
    diff = round(parsed['order_total_eur'] - (parsed['sum_net_eur'] - parsed['order_discount_eur']), 2)
    return abs(diff) <= tol
