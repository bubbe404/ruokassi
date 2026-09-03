"""
Email notifications via Gmail SMTP (same account + app password as ingestion).
Phase-1 notification channel: missing-items ("buy at pickup") and parse failures.
"""
import os
import smtplib
from email.message import EmailMessage

GMAIL_USER = os.environ.get('GMAIL_USER', '')
GMAIL_APP_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD', '')
NOTIFY_TO = os.environ.get('NOTIFY_TO', GMAIL_USER)


def send(subject, body):
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and NOTIFY_TO):
        print('[notify] SMTP not configured, skipping email')
        return
    msg = EmailMessage()
    msg['From'] = GMAIL_USER
    msg['To'] = NOTIFY_TO
    msg['Subject'] = subject
    msg.set_content(body)
    with smtplib.SMTP('smtp.gmail.com', 587) as s:
        s.starttls()
        s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        s.send_message(msg)
    print(f'[notify] sent: {subject}')


def missing_items_body(order):
    """order is a parsed receipt dict with a non-empty missing_items list."""
    lines = [f"Order {order['order_id']} ({order['order_date']}) — out of stock,",
             "grab these at pickup:\n"]
    for m in order['missing_items']:
        q = int(m['qty']) if float(m['qty']).is_integer() else m['qty']
        lines.append(f"  • {m['name']}  ×{q}")
    return "\n".join(lines)
