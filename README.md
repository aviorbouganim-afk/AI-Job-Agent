# 🎯 AI Job Search Agent & Career CRM

A two-stage, AI-assisted job pipeline for an Industrial Engineering & Management
(IE&M) job search in Israel. It scrapes daily postings from LinkedIn and Indeed,
runs them through a multi-layer seniority filter, scores them against your CV
across four personas (Data/BI Analyst, Product, Project/PMO, Operations), and
tracks your entire application pipeline in a local Streamlit dashboard.

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
| `system_prompt.txt` | Stage 1 scoring prompt | Must contain the literal `{CV_TEXT}` placeholder |
| `stage2_system_prompt.txt` | Stage 2 CV-tailoring prompt | Must contain the literal `{CV_TEXT}` placeholder |

---

## 2. Environment variables (set permanently)

Only one key is strictly required; the SerpApi key is optional (disabled by default).

| Variable | Used by | Get it from |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | scoring + tailoring | console.anthropic.com |
| `SERPAPI_KEY` | Google Jobs (disabled for IL) | serpapi.com |

**Windows (PowerShell, permanent):**
```powershell
setx ANTHROPIC_API_KEY "sk-ant-..."
# close and reopen the terminal for setx to take effect
```

**macOS / Linux (add to ~/.zshrc or ~/.bashrc):**
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

---

## 3. Running it

### Step 1 — scrape & score

Always redirect output to a file so the log survives if the terminal closes:

```bash
python job_agent.py > agent_log.txt 2>&1
```

What it does:
- Fetches from **LinkedIn + Indeed** via jobspy across 25 English queries and 9 Hebrew queries.
- Deduplicates on a stable `(company + title + location)` MD5 hash.
- Runs three pre-scoring filters in sequence:
  1. **Domain blocklist** — drops construction, civil engineering, pure finance/banking.
  2. **Seniority gate** — drops Senior/Lead/Principal titles, Mid-Senior LinkedIn tags, and roles requiring > 3 years experience (detected via regex in the JD, English + Hebrew).
  3. **Relevance gate** — drops roles whose title has no persona-relevant keyword (Sales, Marketing, HR, etc.).
- Scores only **new/unscored** jobs via the Claude API, capped at `MAX_SCORE_PER_RUN = 60` per run to protect API budget. Already-scored jobs are never re-sent.
- Saves atomically with a dated backup in `backups/`.

### Step 2 — open the dashboard

```bash
streamlit run dashboard.py
```

Opens at `http://localhost:8501` with three tabs:
- **🔥 Morning Firehose** — new jobs sorted by posting date, grouped HIGH / MEDIUM (LOW-match jobs are hidden but still tracked). Set a status to move each one into your pipeline or watchlist.
- **📋 Career CRM** — Active Pipeline (Applied / Interviewing, with Days-in-Stage and a Next-Action nudge) and a Watchlist (Saved for Later).
- **✨ AI Consultant** — pick any tracked job and get a CV-tailoring briefing (ATS audit, bullet rewrites, gap assessment). The Stage 2 call is **cached per job**, so re-opening it never re-bills.

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

The dashboard sidebar shows the last scrape time so you can tell the job actually ran.

---

## 5. Tuning notes

- **Sources:** jobspy covers LinkedIn and Indeed-IL. Google Jobs (SerpApi) has no coverage in Israel — verified empirically. Glassdoor is not supported by jobspy for Israel. Both are disabled by default (`SERPAPI_ENABLED = False`, `GLASSDOOR_ENABLED = False`); flip to `True` only if searching remote/US roles.
- **Queries:** 25 English + 9 Hebrew queries. Junior variants are included for Data/BI and Product (top two personas). Cross-functional umbrella terms (`Solutions Engineer`, `Systems Analyst`, `Implementation Specialist`, etc.) catch hybrid roles. Geography is handled by jobspy's `location`/`country_indeed` args — not the query strings.
- **Seniority gate:** `MAX_YEARS_EXPERIENCE = 3` (drop if JD requires strictly more). Flip `SENIORITY_FILTER_ENABLED = False` for full visibility during debug runs.
- **Relevance gate:** `PRE_SCORE_FILTER_ENABLED = False` for full visibility during debug runs.
- **Cost cap:** `MAX_SCORE_PER_RUN = 60`. On a typical daily run with 10–30 new jobs this is never hit; it only guards against first-run floods after query expansion.
- **Batch size:** `BATCH_SIZE = 6` keeps Claude's JSON output under the token cap.
- **Blocklist:** `TITLE_BLOCKLIST` filters off-domain titles. The log prints how many were dropped — if it's catching roles you want, edit the set.

---

## 6. Recovery

The tracker is the only stateful file. If `job_agent.py` ever reports it as corrupt,
it refuses to overwrite (so it can't destroy your pipeline) — restore the latest file
from `backups/job_tracker_YYYY-MM-DD.json` and re-run.

---

## File map

```
job_agent.py              # scraper + filter + scorer (scheduled)
dashboard.py              # Streamlit Career CRM (interactive)
system_prompt.txt         # Stage 1 scoring prompt
stage2_system_prompt.txt  # Stage 2 CV-tailoring prompt
cv.txt                    # your CV (plain text)
requirements.txt
job_tracker.json          # created on first run — the single source of truth
backups/                  # dated tracker backups (auto-created)
agent_log.txt             # created when you redirect stdout (recommended)
```
