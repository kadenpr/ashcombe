# Ashcombe AI News Tracker

A Python script that runs on a schedule, fetches recent news for a list of target companies via Google News RSS, filters and summarises each item using the Anthropic API, and delivers a single HTML digest email.

---

## File structure

```
Ashcombe/
├── tracker.py        # Main entry point
├── fetcher.py        # Google News RSS fetcher
├── summariser.py     # Anthropic relevance classifier + summariser
├── mailer.py         # Jinja2 email renderer + SendGrid/SMTP sender
├── template.html     # Jinja2 HTML email template
├── companies.csv     # Target companies (name, url, owner)
├── state.json        # Persisted last-run timestamp + seen item hashes
├── requirements.txt
├── .env.example      # Copy to .env and fill in secrets
└── README.md
```

---

## Setup

### 1. Python environment

Requires **Python 3.11+**.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Environment variables

Copy `.env.example` to `.env` and fill in your secrets:

```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key (`sk-ant-...`) |
| `SENDGRID_API_KEY` | Preferred | SendGrid API key — omit to fall back to SMTP |
| `RECIPIENT_EMAIL` | Yes | Digest recipient address |
| `SENDER_EMAIL` | Yes | From address (must be verified in SendGrid) |
| `SMTP_HOST` | Optional | SMTP host (default: `smtp.gmail.com`) |
| `SMTP_PORT` | Optional | SMTP port (default: `587`) |
| `SMTP_USER` | Optional | SMTP username (defaults to `SENDER_EMAIL`) |
| `SMTP_PASSWORD` | Optional | SMTP password or app password |
| `LOOKBACK_HOURS` | Optional | Hours to look back on first run (default: `24`) |
| `DRY_RUN` | Optional | Set to `true` to skip email send and print to console |

### 3. Configure companies

Edit `companies.csv` — one row per company:

```csv
name,url,owner
Balfour Beatty,https://www.balfourbeatty.com,Infrastructure
```

- **name** — used as the Google News search query and as the digest heading
- **url** — reference URL (not used in search, kept for context)
- **owner** — internal category label (not used in current logic)

---

## Running

### Normal run

```bash
python tracker.py
```

### Dry run (no email sent, digest printed to console)

```bash
python tracker.py --dry-run
# or
DRY_RUN=true python tracker.py
```

### Debug logging

```bash
python tracker.py --log-level DEBUG
```

### Test individual modules

```bash
# Fetcher smoke test (first 3 companies, last 24 h)
python fetcher.py

# Summariser smoke test (3 synthetic items, requires ANTHROPIC_API_KEY)
python summariser.py

# Mailer smoke test (synthetic digest, writes digest_preview.html)
python mailer.py
```

---

## Scheduling

### macOS / Linux — cron

Run at **07:00 UK time** (adjust for UTC offset — BST is UTC+1, GMT is UTC+0):

```bash
crontab -e
```

Add:
```cron
# BST (summer): 07:00 UK = 06:00 UTC
0 6 * * * cd /path/to/Ashcombe && /path/to/.venv/bin/python tracker.py >> /path/to/Ashcombe/tracker.log 2>&1

# GMT (winter): 07:00 UK = 07:00 UTC
0 7 * * * cd /path/to/Ashcombe && /path/to/.venv/bin/python tracker.py >> /path/to/Ashcombe/tracker.log 2>&1
```

Or use a single rule that covers both by running at both 06:00 and 07:00 UTC with a guard, or configure your cron timezone:

```cron
CRON_TZ=Europe/London
0 7 * * * cd /path/to/Ashcombe && /path/to/.venv/bin/python tracker.py >> /path/to/Ashcombe/tracker.log 2>&1
```

### Windows Task Scheduler

1. Open Task Scheduler → Create Basic Task
2. Trigger: Daily at 07:00
3. Action: Start a program
   - Program: `C:\path\to\.venv\Scripts\python.exe`
   - Arguments: `tracker.py`
   - Start in: `C:\path\to\Ashcombe`
4. Set the time zone to **GMT Standard Time / GMT Daylight Time** in the trigger settings

---

## Behaviour and idempotency

- **Seen hashes** — every fetched item URL is hashed and stored in `state.json` after a successful run. Re-running within the same window will not re-process or re-send those items.
- **No relevant items** — the script exits cleanly with code 0 and no email is sent.
- **No new items** — same: clean exit, no email.
- **State is only updated after a successful send** — if the email send fails, the next run will retry the same items.

---

## Tuning the relevance prompt

The LLM prompt lives as a single constant in `summariser.py`:

```python
RELEVANCE_SYSTEM_PROMPT = """..."""
```

Edit the categories, examples, or suppression rules there without touching any surrounding logic. Change `DEFAULT_MODEL` in the same file to swap models (e.g. `claude-opus-4-6` for higher accuracy).

---

## Costs

With `claude-haiku-4-5-20251001` and prompt caching enabled, classifying 100 items costs roughly **$0.01–0.03** per run. The system prompt is cached after the first call in each run, so only user-turn tokens are billed for subsequent items.
