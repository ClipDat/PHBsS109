# Crypto/News Watcher → iPhone notifications

Checks news RSS feeds every 15 minutes for keyword combos (Trump+crypto,
Trump+Iran, Fed rate news, oil supply news) and pushes a notification to
your phone the moment something matches. Runs entirely free on GitHub
Actions — your laptop doesn't need to be on.

**Important:** this catches news *as it's published*, not before. Nothing
can predict an announcement before it happens. Treat alerts as "go check
this now," not as confirmed fact — always verify before trading on one.

## Setup (10 minutes, all free)

### 1. Get the ntfy app on your iPhone
- Install **ntfy** from the App Store (it's free, made by a small open-source team)
- Pick a private "topic" name — this is like a password, so make it long and
  random, e.g. `wishorder-news-x7k2p9`
- In the app, tap "+" and subscribe to that exact topic name

### 2. Create a GitHub repo
- Go to github.com → New repository
- **Make it Public.** (Public repos get unlimited free Actions minutes;
  private repos only get 2,000 min/month, which running every 15 min will
  eat through fast. Nothing sensitive lives in this repo — the topic name
  is stored as a secret, not in the code.)
- Upload all 5 files from this project, keeping the folder structure
  (`.github/workflows/news-watch.yml` must stay in that exact path)

### 3. Add your ntfy topic as a secret
- In your new repo: Settings → Secrets and variables → Actions → New repository secret
- Name: `NTFY_TOPIC`
- Value: the topic name you picked in step 1

### 4. Enable Actions
- Go to the "Actions" tab in your repo → enable workflows if prompted
- Click into "News Watch" → "Run workflow" to test it immediately
- After that it runs automatically every 15 minutes

## Tuning it

All the logic you'd actually want to tweak lives at the top of `script.py`:

- **`FEEDS`** — add or remove RSS feed URLs
- **`KEYWORD_GROUPS`** — each group fires when 2+ of its keywords appear in
  a headline/summary. Add your own groups, e.g.:
  ```python
  "elon_crypto": ["musk", "dogecoin", "tesla", "crypto"],
  ```

Push a change to `script.py` any time — no redeploy needed, the next
scheduled run just picks it up.

## Known limitations
- Free ntfy.sh is a shared public server — don't put anything truly
  sensitive in the topic name or messages (it's obscure, not encrypted-private)
- Keyword matching means it will occasionally over/under-trigger — this is
  intentionally simple to keep it free and dependency-light. If you want
  smarter relevance filtering later, that's a natural next upgrade.
