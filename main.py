"""
Daily job search bot.

Fetches new listings from Qureos and RemoteOK, scores each one against your
CV using Groq, and emails you a digest of the good matches. Designed to run
once a day via GitHub Actions (see .github/workflows/daily-job-search.yml) —
no server, no subscription, nothing to keep alive.

State (which job IDs we've already seen) lives in seen_jobs.json, which the
GitHub Action commits back to the repo after every run.
"""

import json
import os
import re
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config — everything here comes from environment variables (GitHub Actions
# secrets), so no credentials ever live in this file or the repo.
# ---------------------------------------------------------------------------
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
TO_EMAIL = os.environ.get("TO_EMAIL", GMAIL_ADDRESS)

SEEN_JOBS_FILE = Path(__file__).parent / "seen_jobs.json"
CV_FILE = Path(__file__).parent / "cv_summary.txt"

# Qureos searches to run — each is (search title slug, location slug, human label)
QUREOS_SEARCHES = [
    ("ai-engineer-jobs-in-dubai", "Dubai", "AI Engineer"),
    ("full-stack-developer-jobs-in-dubai", "Dubai", "Full Stack Developer"),
]

# RemoteOK: public JSON API, filtered by tag
REMOTEOK_TAGS = ["ai", "python", "fullstack"]

MIN_SCORE_TO_INCLUDE = 6  # out of 10 — tune this once you see real output


# ---------------------------------------------------------------------------
# Qureos
# ---------------------------------------------------------------------------
def get_qureos_build_id() -> str:
    """Qureos embeds their current Next.js build ID in the page HTML. It
    changes whenever they redeploy, so we pull it fresh every run instead
    of hardcoding it — otherwise this silently breaks after their next
    deploy and you'd never know."""
    resp = requests.get("https://app.qureos.com/jobs", timeout=20)
    resp.raise_for_status()
    match = re.search(r"/_next/data/([^/]+)/", resp.text)
    if not match:
        raise RuntimeError(
            "Couldn't find Qureos's build ID in the page — they may have "
            "changed their site structure. Check main.py's Qureos section."
        )
    return match.group(1)


def fetch_qureos_jobs() -> list[dict]:
    build_id = get_qureos_build_id()
    jobs = []
    for slug, location, label in QUREOS_SEARCHES:
        url = (
            f"https://app.qureos.com/_next/data/{build_id}/en/jobs/search/"
            f"{slug}.json?position=1&pageNumber=1&q=&location={location}&slug={slug}"
        )
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        docs = data["pageProps"]["jobsFromJobPool"]["docs"]
        for job in docs:
            jobs.append(
                {
                    "id": f"qureos:{job['_id']}",
                    "title": job["title"],
                    "company": job.get("company", {}).get("name", "Unknown"),
                    "location": job.get("location", "Unknown"),
                    "link": job.get("applyLink", ""),
                    "description": strip_html(job.get("description", "")),
                    "source": f"Qureos ({label})",
                }
            )
    return jobs


# ---------------------------------------------------------------------------
# RemoteOK
# ---------------------------------------------------------------------------
def fetch_remoteok_jobs() -> list[dict]:
    resp = requests.get(
        "https://remoteok.com/api",
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    jobs = []
    # First item is metadata, not a job — skip it
    for job in data[1:]:
        tags = [t.lower() for t in job.get("tags", [])]
        if not any(t in tags for t in REMOTEOK_TAGS):
            continue
        jobs.append(
            {
                "id": f"remoteok:{job.get('id')}",
                "title": job.get("position", "Unknown"),
                "company": job.get("company", "Unknown"),
                "location": job.get("location", "Remote"),
                "link": job.get("url", ""),
                "description": strip_html(job.get("description", "")),
                "source": "RemoteOK",
            }
        )
    return jobs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:2000]  # keep it short — Groq doesn't need the whole posting


def load_seen_ids() -> set[str]:
    if SEEN_JOBS_FILE.exists():
        return set(json.loads(SEEN_JOBS_FILE.read_text()))
    return set()


def save_seen_ids(ids: set[str]) -> None:
    SEEN_JOBS_FILE.write_text(json.dumps(sorted(ids), indent=2))


def load_cv_summary() -> str:
    if not CV_FILE.exists():
        raise RuntimeError(
            f"Missing {CV_FILE.name} — create it with a short summary of your "
            "skills/experience so the scorer has something to compare against."
        )
    return CV_FILE.read_text()


# ---------------------------------------------------------------------------
# Groq scoring
# ---------------------------------------------------------------------------
def score_job(job: dict, cv_summary: str) -> tuple[int, str]:
    prompt = f"""You are screening a job posting for a candidate. Score how
well this job fits the candidate from 0-10, and give a one-sentence reason.

CANDIDATE SUMMARY:
{cv_summary}

JOB TITLE: {job['title']}
COMPANY: {job['company']}
LOCATION: {job['location']}
DESCRIPTION: {job['description']}

Respond in EXACTLY this format, nothing else:
SCORE: <number 0-10>
REASON: <one sentence>"""

    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        },
        timeout=30,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]

    score_match = re.search(r"SCORE:\s*(\d+)", content)
    reason_match = re.search(r"REASON:\s*(.+)", content)
    score = int(score_match.group(1)) if score_match else 0
    reason = reason_match.group(1).strip() if reason_match else "No reason given"
    return score, reason


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
def send_digest(scored_jobs: list[dict]) -> None:
    if not scored_jobs:
        print("No new matching jobs today — skipping email.")
        return

    lines = [f"{len(scored_jobs)} new job match(es) today:\n"]
    for job in scored_jobs:
        lines.append(
            f"[{job['score']}/10] {job['title']} — {job['company']} "
            f"({job['location']})\nSource: {job['source']}\n"
            f"Why: {job['reason']}\nApply: {job['link']}\n"
        )
    body = "\n".join(lines)

    msg = MIMEText(body)
    msg["Subject"] = f"Job digest: {len(scored_jobs)} new match(es)"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = TO_EMAIL

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.send_message(msg)

    print(f"Sent digest with {len(scored_jobs)} job(s).")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    cv_summary = load_cv_summary()
    seen_ids = load_seen_ids()

    all_jobs = []
    try:
        all_jobs += fetch_qureos_jobs()
    except Exception as e:
        print(f"Qureos fetch failed (continuing anyway): {e}", file=sys.stderr)

    try:
        all_jobs += fetch_remoteok_jobs()
    except Exception as e:
        print(f"RemoteOK fetch failed (continuing anyway): {e}", file=sys.stderr)

    new_jobs = [j for j in all_jobs if j["id"] not in seen_ids]
    print(f"Fetched {len(all_jobs)} total, {len(new_jobs)} new.")

    scored_jobs = []
    for job in new_jobs:
        try:
            score, reason = score_job(job, cv_summary)
        except Exception as e:
            print(f"Scoring failed for {job['title']}: {e}", file=sys.stderr)
            continue
        seen_ids.add(job["id"])
        if score >= MIN_SCORE_TO_INCLUDE:
            job["score"] = score
            job["reason"] = reason
            scored_jobs.append(job)

    scored_jobs.sort(key=lambda j: j["score"], reverse=True)
    send_digest(scored_jobs)
    save_seen_ids(seen_ids)


if __name__ == "__main__":
    main()
