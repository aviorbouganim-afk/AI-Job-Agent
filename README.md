# 🎯 AI Job Search Agent & Career CRM

A two-stage, AI-assisted job pipeline for an Industrial Engineering & Management
(IE&M) job search in Israel. It scrapes daily postings, scores them against your CV
across four personas (Data/BI Analyst, Product, Project/PMO, Operations), and tracks
your entire application pipeline in a local Streamlit dashboard.

```
┌─────────────────────┐   writes   ┌──────────────────┐   reads/writes   ┌──────────────────┐
│  job_agent.py       │ ─────────► │ job_tracker.json │ ◄──────────────► │  dashboard.py    │
│  (scheduled scrape  │            │ (single source   │                  │  (Streamlit CRM) │
│   + Claude scoring) │            │  of truth)       │                  │                  │
└─────────────────────┘            └──────────────────┘                  └──────────────────┘
```

The scraper only writes scoring fields + the cached JD snapshot; the dashboard only
writes status/tracking fields. A shared file lock serializes the two so a background
scrape and a live click can't corrupt each other.

---

## 1. Setup

Requires **Python 3.10+**.

```bash
# from the project folder
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate

pip install -r requirements.txt
```

### Required files in the project root

| File | Purpose | Notes |
|------|---------|-------|
| `cv.txt` | Your General CV as plain text | Injected into both prompts at runtime |
| `system_prompt.txt` | The Stage 1 scoring prompt | **Rename `stage1_system_prompt.md` → `system_prompt.txt`** (this is the name `job_agent.py` reads) |
| `stage2_system_prompt.txt` | The Stage 2 tailoring prompt | Read by the dashboard; it must contain the literal `{CV_TEXT}` placeholder |

> Both prompt files must contain the literal token `{CV_TEXT}` — the code replaces
> it with the contents of `cv.txt`. They already do; don't remove it.

---

## 2. Environment variables (set permanently)

Two keys are read from the environment — never hard-code them.

| Variable | Used by | Get it from |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | scoring + tailoring | console.anthropic.com |
| `SERPAPI_KEY` | Google Jobs fetch | serpapi.com |

**Windows (PowerShell, permanent):**
```powershell
setx ANTHROPIC_API_KEY "sk-ant-..."
setx SERPAPI_KEY "your-serpapi-key"
# close and reopen the terminal for setx to take effect
```

**macOS / Linux (add to ~/.zshrc or ~/.bashrc):**
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export SERPAPI_KEY="your-serpapi-key"
```

---

## 3. Running it

### Step 1 — scrape & score (the background job)

```bash
python job_agent.py
```

This fetches from SerpApi (English + Hebrew queries) and Jobspy (English, LinkedIn +
Indeed), deduplicates on a stable `(company + title + location)` hash, filters
off-domain roles (construction / pure finance) via `TITLE_BLOCKLIST`, scores only the
**new/unscored** jobs in batches of 6, and saves atomically with a dated backup in
`backups/`. Re-running is safe and cheap: already-scored jobs are never re-sent to
Claude, so your API bill tracks new jobs only.

### Step 2 — open the dashboard

```bash
streamlit run dashboard.py
```

Opens at `http://localhost:8501` with three tabs:
- **🔥 Morning Firehose** — today's new jobs, grouped HIGH / MEDIUM / LOW; set a status to file each one.
- **📋 Career CRM** — Active Pipeline (Applied / Interviewing, with Days-in-Stage and a Next-Action nudge) and a Watchlist (Saved for Later).
- **✨ AI Consultant** — pick any tracked job and tailor your CV for it. The Stage 2 call is **cached per job**, so re-opening it never re-bills.

---

## 4. Scheduling the daily scrape

**Windows — Task Scheduler:**
Create a Basic Task → Daily → Action "Start a program":
- Program: full path to `python.exe` in your venv
- Arguments: `job_agent.py`
- Start in: the project folder

**macOS / Linux — cron** (`crontab -e`), e.g. 7:00am daily:
```
0 7 * * * cd /path/to/AI_Job_Agent && /path/to/.venv/bin/python job_agent.py >> agent.log 2>&1
```

Leave the dashboard running (or start it when you sit down); it always reads the
latest `job_tracker.json`, and the sidebar shows the last scrape time so you can tell
the cron actually ran.

---

## 5. Tuning notes

- **Date window:** `SERPAPI_DATE_FILTER` (`today | 3days | week | month`) buffers Google's indexing lag. `3days` is the daily-run sweet spot.
- **Batch size:** `BATCH_SIZE = 6` keeps Claude's JSON output under the token cap; a `stop_reason` guard logs loudly if a batch ever truncates anyway.
- **Blocklist:** `TITLE_BLOCKLIST` filters off-domain titles. The run log prints how many were dropped — if it's catching roles you want (watch `"financial analyst"`), edit the set. It's the one debatable entry.
- **Queries:** geography lives in the SerpApi `location`/`gl`/`hl` params and the Jobspy `location` arg, **not** in the query strings. Add roles to `SEARCH_QUERIES` (both engines) or `HEBREW_QUERIES` (SerpApi only).

---

## 6. Recovery

The tracker is the only stateful file. If `job_agent.py` ever reports it as corrupt,
it refuses to overwrite (so it can't destroy your pipeline) — restore the latest file
from `backups/job_tracker_YYYY-MM-DD.json` and re-run.

---

## File map

```
job_agent.py              # scraper + scorer (scheduled)
dashboard.py              # Streamlit Career CRM (interactive)
system_prompt.txt         # Stage 1 scoring prompt (rename from stage1_system_prompt.md)
stage2_system_prompt.txt  # Stage 2 tailoring prompt
cv.txt                    # your CV (plain text)
requirements.txt
job_tracker.json          # created on first run — the single source of truth
backups/                  # dated tracker backups (auto-created)
```
