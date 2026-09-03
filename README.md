# ruokassi

Weekly food planning & ordering for our family. Turns S-kaupat receipt emails
into a live purchase history in Supabase, and (later) a mobile app that proposes
the next order and meal plan. Cloud-only — nothing runs on a personal machine.

**Milestone 0 (this):** the ingestion pipeline. A scheduled job reads receipts
from Gmail, parses them, writes them to Supabase, and emails the "buy at pickup"
list of out-of-stock items. No UI yet.

## How it works

```
Gmail  --IMAP-->  GitHub Actions (ingest.py, Python)  --REST-->  Supabase (Postgres)
                        |                                              ^
                        +-- SMTP: missing-items email to you           |
                                                             PWA (later) reads here
```

Everything is deterministic; there is no LLM at runtime. The parser reconciles
all 86 historical orders to their receipt totals to the cent.

## Layout

- `ingest/parser.py` — the receipt parser (regex; also extracts `Puuttuvat tuotteet`).
- `ingest/supa.py` — Supabase REST upserts (idempotent per order).
- `ingest/notify.py` — Gmail SMTP notifications.
- `ingest/ingest.py` — entry point (IMAP fetch, backfill, dry-run).
- `supabase/migrations/0001_m0_ingestion_schema.sql` — the DB schema (already applied).
- `.github/workflows/ingest.yml` — the cron job (every 2 h) + manual dispatch.

## Setup (M0)

1. **Google App Password.** On the Gmail account, enable 2-Step Verification,
   then create an App Password (Google Account → Security → App passwords). This
   is used for both IMAP (read) and SMTP (notify). If Google ever stops issuing
   these, switch to an OAuth refresh token — the code stays Python.

2. **Supabase key.** In the Supabase project → Settings → API, copy the
   `service_role` key and the project URL. The service_role key bypasses RLS and
   must **only** live in Actions secrets — never in the app or the repo.

3. **Add repo secrets** (Settings → Secrets and variables → Actions):

   | secret | value |
   |---|---|
   | `SUPABASE_URL` | `https://<project>.supabase.co` |
   | `SUPABASE_SERVICE_ROLE_KEY` | the service_role key |
   | `GMAIL_USER` | the Gmail address |
   | `GMAIL_APP_PASSWORD` | the app password (no spaces) |
   | `NOTIFY_TO` | where to send alerts (optional; defaults to `GMAIL_USER`) |

4. **Backfill + acceptance test.** Actions → *ingest* → *Run workflow* → mode
   `backfill`. It ingests the full history and logs
   `[acceptance] orders reconciled: N/N`. Expect **86/86**. After that the cron
   keeps it current every 2 hours.

### Run locally (optional)

```bash
pip install -r requirements.txt
cd ingest
# parse + reconcile only, no DB, no email:
python ingest.py --backfill --dry-run
# real backfill (needs the env vars from step 3):
SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... GMAIL_USER=... GMAIL_APP_PASSWORD=... \
  python ingest.py --backfill
```

## Notes & known follow-ups

- **Idempotent.** Dedupe is on the email `Message-ID`; re-upserting an order
  replaces its items. Re-runs and overlapping windows are safe.
- **Resilience.** Every email's raw text is stored in `ingested_emails`; if
  S-kaupat changes its template and the parser breaks, re-parse from the DB
  instead of re-fetching Gmail. Parse failures are emailed.
- **Cadence.** Currently every 2 h. A tighter, confirmation-anchored window
  (poll hardest in the hours before the reserved pickup slot) is a planned
  refinement — the confirmation email carries the pickup time.
- **GitHub Actions** disables scheduled workflows after 60 days with no repo
  activity; a periodic commit (or a heartbeat step) avoids this.
- **Not indexed.** `robots.txt` disallows crawlers for the future Pages site;
  the app will also ship a `noindex` meta tag.

Architecture spec (private): see the project's design doc.
