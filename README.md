# 🎯 AI Job Search Agent & Career CRM

An automated job pipeline for an Industrial Engineering & Management (IE&M) job
search in Israel. It scrapes daily postings from LinkedIn and Indeed, runs them
through a multi-layer seniority filter, and tracks the full application pipeline
in a local Streamlit dashboard.

```
┌─────────────────────┐   writes   ┌──────────────────┐   reads/writes   ┌──────────────────┐
│  job_agent.py       │ ─────────► │ job_tracker.json │ ◄──────────────► │  dashboard.py    │
│  (scheduled scrape  │            │ (single source   │                  │  (Streamlit CRM) │
│   + filter)         │            │  of truth)       │                  │                  │
└─────────────────────┘            └──────────────────┘                  └──────────────────┘
```

The scraper writes job data + snapshot; the dashboard writes status/tracking fields only.
A shared file lock serializes the two so a background scrape and a live click can't
corrupt each other.

---

## 1. Setup

Requires **Python 3.10+**.

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate

pip install -r requirements.txt
```

### Required files in the project root

| File | Purpose |
|------|---------|
| `cv.txt` | Your General CV as plain text — used by Stage 2 CV tailoring |
| `system_prompt.txt` | Stage 1 scoring prompt (used when `SCORING_ENABLED=True`) |
| `stage2_system_prompt.txt` | Stage 2 CV-tailoring prompt |

---

## 2. Environment variables

| Variable | Used by | Required? |
|----------|---------|-----------|
| `ANTHROPIC_API_KEY` | Stage 2 CV tailoring | Yes |
| `SERPAPI_KEY` | Google Jobs (disabled for IL) | No |

**Windows (PowerShell):**
```powershell
setx ANTHROPIC_API_KEY "sk-ant-..."
```

**macOS / Linux:**
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

---

## 3. Running it

### Step 1 — scrape & filter

Always redirect output to a file so the log survives if the terminal closes:

```bash
python job_agent.py > agent_log.txt 2>&1
```

What it does:
- Fetches from **LinkedIn + Indeed** via jobspy across 25 English + 9 Hebrew queries.
- Deduplicates on a stable `(company + title + location)` MD5 hash.
- Runs three pre-scoring filters in sequence:
  1. **Domain blocklist** — drops construction, civil engineering, pure finance/banking.
  2. **Seniority gate** — drops Senior/Lead/Principal titles, Mid-Senior LinkedIn tags, and roles requiring > 3 years experience (regex, English + Hebrew).
  3. **Relevance gate** — drops roles whose title has no persona-relevant keyword.
- Classifies each job by persona (Data/BI, Product, Project/PMO, Operations) using keyword matching — no API call needed.
- Saves atomically with a dated backup in `backups/`.

### Step 2 — open the dashboard

```bash
streamlit run dashboard.py
```

Opens at `http://localhost:8501` with three tabs:
- **🔥 Morning Firehose** — new jobs sorted by posting date. Filter by job type or last run. Stale jobs (4+ days in New) can be bulk-archived.
- **📋 Career CRM** — Active Pipeline (Applied / Interviewing) and Watchlist (Saved for Later).
- **✨ AI Consultant** — pick any tracked job and get a CV-tailoring briefing (ATS audit, bullet rewrites, gap assessment). Cached per job so re-opening never re-bills.

---

## 4. Scheduling the daily scrape

**Windows — Task Scheduler:**
- Program: full path to `python.exe` in your venv
- Arguments: `job_agent.py`
- Start in: the project folder

**macOS / Linux — cron** (`crontab -e`):
```
0 7 * * * cd /path/to/AI_Job_Agent && /path/to/.venv/bin/python job_agent.py >> agent.log 2>&1
```

---

## 5. Tuning notes

- **Scoring:** `SCORING_ENABLED = False` by default. Jobs are classified by keyword matching instead of Claude API. Flip to `True` to re-enable Claude scoring (HIGH/MEDIUM/LOW tiers, alignments, gaps). When enabled, scoring is capped at `MAX_SCORE_PER_RUN = 60` per run.
- **Sources:** LinkedIn + Indeed only. Google Jobs (SerpApi) has no Israel coverage — disabled. Glassdoor not supported by jobspy for Israel — disabled.
- **Queries:** 25 English + 9 Hebrew. Junior variants included for Data/BI and Product. Cross-functional umbrella terms catch hybrid roles.
- **Seniority gate:** `MAX_YEARS_EXPERIENCE = 3`. Flip `SENIORITY_FILTER_ENABLED = False` for full visibility during debug.
- **Relevance gate:** `PRE_SCORE_FILTER_ENABLED = False` for full visibility during debug.
- **Stale jobs:** Jobs sitting in "New" for 4+ days are flagged in the Firehose with a bulk-archive button.

---

## 6. Recovery

If `job_agent.py` reports the tracker as corrupt, restore from `backups/job_tracker_YYYY-MM-DD.json` and re-run.

---

## File map

```
job_agent.py              # scraper + filter (scheduled)
dashboard.py              # Streamlit Career CRM (interactive)
system_prompt.txt         # Stage 1 scoring prompt (used when SCORING_ENABLED=True)
stage2_system_prompt.txt  # Stage 2 CV-tailoring prompt
cv.txt                    # your CV (plain text)
requirements.txt
job_tracker.json          # created on first run — single source of truth
backups/                  # dated tracker backups (auto-created)
agent_log.txt             # created when you redirect stdout (recommended)
```
