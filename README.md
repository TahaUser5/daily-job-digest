# Daily Job Search Bot

Fetches new listings from Qureos and RemoteOK every day, scores them against
your CV using Groq's LLM API, and emails you a digest of the good matches.
Runs entirely on GitHub Actions — free, no server, nothing to keep alive.

## How it works

1. A GitHub Actions cron job runs `main.py` once a day.
2. The script fetches jobs from Qureos (AI Engineer + Full Stack Developer
   searches, Dubai) and RemoteOK (tagged `ai`, `python`, `fullstack`).
3. New listings (not seen in a previous run) get scored 0-10 against
   `cv_summary.txt` using Groq.
4. Anything scoring 6+ goes into an email digest sent to you.
5. The list of seen job IDs is committed back to the repo so tomorrow's run
   only looks at what's actually new.

## Setup

### 1. Fill in your CV summary

Edit `cv_summary.txt` with a real summary of your background and what
you're looking for — this is what the scorer compares jobs against.

### 2. Get a Gmail App Password

Regular Gmail passwords don't work for SMTP. Generate an app-specific one:

- Go to your Google Account → Security → 2-Step Verification (must be on)
- Search settings for "App passwords"
- Create one for "Mail", copy the 16-character code

### 3. Add repo secrets

In your GitHub repo: **Settings → Secrets and variables → Actions → New
repository secret**. Add these four:

| Secret name          | Value                                      |
| --------------------- | ------------------------------------------- |
| `GROQ_API_KEY`         | Your Groq API key (console.groq.com)        |
| `GMAIL_ADDRESS`        | The Gmail address sending the digest         |
| `GMAIL_APP_PASSWORD`   | The 16-character app password from step 2    |
| `TO_EMAIL`             | Where you want the digest sent (can be same as GMAIL_ADDRESS) |

### 4. Test it manually

Go to the **Actions** tab in your repo → **Daily Job Search** →
**Run workflow**. This triggers it immediately instead of waiting for the
cron schedule, so you can confirm everything works before trusting it to
run unattended.

### 5. Let it run

Once the manual test sends you a digest successfully, it'll now run daily
on its own at 08:00 UTC (edit the cron line in
`.github/workflows/daily-job-search.yml` to change the time).

## Adding more sources

Each source is just a `fetch_*_jobs()` function in `main.py` that returns a
list of dicts with `id`, `title`, `company`, `location`, `link`,
`description`, and `source` keys. Add a new one and append its results in
`main()` — the dedup, scoring, and email logic all work automatically for
anything in that shape.
