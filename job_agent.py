"""
job_agent.py — Background scraper & scoring orchestrator for the Career CRM.

Runs on a schedule (cron / Task Scheduler). Responsibilities:
  1. Load the persistent job_tracker.json safely (filelock + graceful empties).
  2. Fetch raw jobs from SerpApi (Google Jobs) and Jobspy (LinkedIn/Indeed).
  3. Fingerprint each job → stable MD5 job_key on (company + title + location).
  4. Merge into the tracker WITHOUT clobbering _tracking / _snapshot state.
  5. Batch only the UNSCORED jobs (chunks of 12), inject cv.txt into the system
     prompt, and score them with the Claude API.
  6. Persist atomically (temp file + os.replace) with a dated backup.

This script ONLY writes scoring fields and _snapshot. It is forbidden from
mutating _tracking — that region belongs to the Streamlit dashboard. The
field-level separation is what makes concurrent access safe.

Environment variables required:
    SERPAPI_KEY        — SerpApi key
    ANTHROPIC_API_KEY  — Anthropic API key (read automatically by the SDK)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import re
from datetime import date
from pathlib import Path

import anthropic
from filelock import FileLock
from serpapi import GoogleSearch
from jobspy import scrape_jobs

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

TRACKER_PATH = Path("job_tracker.json")
LOCK_PATH = Path("job_tracker.json.lock")
BACKUP_DIR = Path("backups")
CV_PATH = Path("cv.txt")
SYSTEM_PROMPT_PATH = Path("system_prompt.txt")

# Current Sonnet. (You wrote "Claude 3.5 Sonnet" — that model is outdated.)
CLAUDE_MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4000
BATCH_SIZE = 6  # 6 keeps the JSON output comfortably under MAX_TOKENS; larger
                # batches risk truncating the output array mid-response.
JD_TRUNCATE_CHARS = 6000  # cap each JD so one giant posting can't crowd a batch

# Statuses whose jobs should NOT be re-scored or re-fed to the API.
TERMINAL_STATUSES = {"Archived"}

# ── SerpApi / Google Jobs tuning ────────────────────────────────────────────
# Recency is filtered via a natural-language phrase appended to the query, NOT
# via `chips`/`uds`. SerpApi's `chips` param is deprecated by Google and only
# ever accepted opaque tokens (not "date_posted:3days"), which is why every
# SerpApi call returned zero — fixed by removing chips entirely.
SERPAPI_DATE_FILTER = "3days"          # today | 3days | week | month | ""(off)
SERPAPI_RECENCY_PHRASE = {
    "today": "since yesterday",        # Google Jobs has no "today"; closest is yesterday
    "3days": "in the last 3 days",
    "week":  "in the last week",
    "month": "in the last month",
    "":      "",                       # no recency filter
}

# Search ORIGIN for Google Jobs. City-level is recommended (country-level resolves
# poorly). Israel is small enough that Tel Aviv as origin covers the tech market;
# if this string ever fails to resolve, verify it via serpapi.com/locations-api.
SERPAPI_LOCATION = "Tel Aviv, Israel"

# Quota guard: if SerpApi returns empty/error this many times in a row, stop
# calling it for the rest of the run. Prevents a future breakage from silently
# burning the whole monthly quota (24 dead searches/day → exhausted in days).
SERPAPI_ABORT_AFTER_EMPTY = 3

# Role only — geography is handled by the location/gl/hl params on the SerpApi
# call (and the location arg on Jobspy), NOT the query string. Baking "Israel"
# into q narrows Google's matching, so it's removed. Sent to BOTH engines.
SEARCH_QUERIES = [
    # ── Data / BI Analyst ──
    "Data Analyst",
    "BI Analyst",
    "Business Intelligence Analyst",
    "Business Analyst",
    # ── Product ──
    "Product Manager",
    "Associate Product Manager",
    "Product Operations",
    "Product Analyst",
    # ── Project / PMO ──
    "Project Manager",
    "Technical Project Manager",
    "PMO",
    "Program Manager",
    # ── Operations / Technical Ops ──
    "Operations Analyst",
    "Business Operations",
    "Revenue Operations",
    "Supply Chain Analyst",
]

# Hebrew terms go to SerpApi/Google Jobs ONLY. Israeli high-tech listings on
# LinkedIn/Indeed are mostly English, so Hebrew through Jobspy returns sparse or
# odd matches; Google Jobs indexes Hebrew career-site postings well, especially
# now that the query is localized.
HEBREW_QUERIES = [
    "אנליסט נתונים",          # data analyst
    "אנליסט BI",              # BI analyst
    "אנליסט עסקי",            # business analyst
    "מנהל מוצר",              # product manager
    "אנליסט תפעול",           # operations analyst
    "מנהל פרויקטים",          # project manager (construction-prone → filtered)
    "תעשייה וניהול",          # industrial engineering & management
    "סטודנט תעשייה וניהול",   # IE&M student
]

# Python-side negative constraint. Query-level exclusion ("-construction") is
# unreliable across SerpApi + LinkedIn + Indeed, so we filter deterministically
# here instead. Title-only matching keeps it conservative — a fintech "Business
# Analyst" survives; a "Credit Analyst" doesn't. Tune as noise reveals itself.
TITLE_BLOCKLIST = {
    # construction / civil
    "construction", "civil eng", "structural", "site engineer", "site manager",
    "hvac", "surveyor", "architect", "בנייה", "בניין", "אזרחית", "קונסטרוקציה",
    # pure finance / banking  (NOTE: "financial analyst" is the debatable one —
    # remove it if you find you're dropping hybrid roles you'd actually want.)
    "investment bank", "credit analyst", "financial analyst", "underwrit",
    "mortgage", "teller", "actuary", "equity research", "loan officer",
    "בנקאי", "אשראי", "משכנתא", "אקטואר",
}

TODAY = date.today().isoformat()
LOCK = FileLock(str(LOCK_PATH))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("job_agent")

# Circuit-breaker state for the SerpApi quota guard (see SERPAPI_ABORT_AFTER_EMPTY).
_serpapi_empty_streak = 0
_serpapi_disabled = False


# ─────────────────────────────────────────────────────────────────────────────
# 1. TRACKER LOAD  — graceful on missing / empty / corrupt
# ─────────────────────────────────────────────────────────────────────────────

def load_tracker() -> dict:
    """
    Return the tracker dict {job_key: job_obj}. Tolerates a missing file
    (first run) and an empty/corrupt file (returns {} and logs loudly rather
    than crashing the daily run).
    """
    if not TRACKER_PATH.exists():
        log.info("No tracker found — starting a fresh one.")
        return {}
    try:
        text = TRACKER_PATH.read_text(encoding="utf-8").strip()
        if not text:
            log.warning("Tracker file is empty — treating as fresh.")
            return {}
        data = json.loads(text)
        if not isinstance(data, dict):
            log.error("Tracker is not a JSON object — refusing to overwrite. "
                      "Inspect %s manually.", TRACKER_PATH)
            raise SystemExit(1)
        return data
    except json.JSONDecodeError as e:
        # Do NOT silently reset — a corrupt tracker is recoverable from backups,
        # but an auto-overwrite would destroy the pipeline. Fail loud.
        log.error("Tracker is corrupt (%s). Restore from backups/ before rerun.", e)
        raise SystemExit(1)


# ─────────────────────────────────────────────────────────────────────────────
# 2. FETCH  — SerpApi (Google Jobs) + Jobspy (LinkedIn/Indeed)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_serpapi_jobs(query: str, recency: bool = True) -> list[dict]:
    """
    Google Jobs via SerpApi. Returns [] on any failure — one bad query must never
    sink the whole run.

    Recency is applied by appending a natural-language phrase to the query (the
    supported method), NOT via `chips` — that param is deprecated and only ever
    accepted opaque tokens, so it silently returned zero results. `recency` is
    disabled for Hebrew queries (the English phrase would pollute them).

    A module-level circuit breaker stops calling SerpApi after
    SERPAPI_ABORT_AFTER_EMPTY consecutive empty/error responses, so a future
    breakage can't burn the whole monthly quota in one run.
    """
    global _serpapi_empty_streak, _serpapi_disabled

    if _serpapi_disabled:
        return []
    api_key = os.environ.get("SERPAPI_KEY")
    if not api_key:
        log.error("SERPAPI_KEY not set — skipping SerpApi.")
        return []

    # Append the recency phrase to q (e.g. "Data Analyst in the last 3 days").
    q = query
    if recency:
        phrase = SERPAPI_RECENCY_PHRASE.get(SERPAPI_DATE_FILTER, "")
        if phrase:
            q = f"{query} {phrase}"

    def _register_empty() -> None:
        """Advance the circuit breaker; disable SerpApi if the streak trips."""
        global _serpapi_empty_streak, _serpapi_disabled
        _serpapi_empty_streak += 1
        if _serpapi_empty_streak >= SERPAPI_ABORT_AFTER_EMPTY:
            _serpapi_disabled = True
            log.error("SerpApi returned nothing %d times in a row — disabling it "
                      "for the rest of this run to protect quota.",
                      SERPAPI_ABORT_AFTER_EMPTY)

    try:
        search = GoogleSearch({
            "engine": "google_jobs",
            "q": q,
            "api_key": api_key,
            # City-level origin + country/language. NO chips (deprecated/broken).
            "location": SERPAPI_LOCATION,
            "gl": "il",
            "hl": "en",
        })
        results = search.get_dict()
        if "error" in results:
            log.warning("SerpApi error for '%s': %s", q, results["error"])
            _register_empty()
            return []
        jobs = results.get("jobs_results", [])
        if jobs:
            _serpapi_empty_streak = 0    # healthy response resets the breaker
        else:
            _register_empty()
        return jobs
    except Exception as e:  # network, quota, parse — isolate the failure
        log.warning("SerpApi fetch failed for '%s': %s", q, e)
        _register_empty()
        return []


def fetch_jobspy_jobs(query: str) -> list[dict]:
    """LinkedIn + Indeed via Jobspy. Returns [] on failure."""
    try:
        df = scrape_jobs(
            site_name=["linkedin", "indeed"],
            search_term=query,
            location="Israel",
            results_wanted=20,
            hours_old=24,
        )
        if df is None or df.empty:
            return []
        # NaN → "" so downstream .get()/string ops never choke on floats.
        return df.fillna("").to_dict("records")
    except Exception as e:
        log.warning("Jobspy fetch failed for '%s': %s", query, e)
        return []


def _first_apply_link(raw: dict) -> str:
    """SerpApi nests apply links; Jobspy gives a flat job_url. Handle both."""
    opts = raw.get("apply_options")
    if isinstance(opts, list) and opts and isinstance(opts[0], dict):
        return opts[0].get("link", "")
    return raw.get("job_url", "") or raw.get("url", "")


def normalize_job(raw: dict, source: str) -> dict:
    """Flatten any source's shape into our common schema. Defensive everywhere:
    sources disagree on field names and occasionally return non-strings."""
    desc = raw.get("description") or raw.get("job_description") or ""
    desc = str(desc)[:JD_TRUNCATE_CHARS]
    return {
        "title": str(raw.get("title") or raw.get("job_title") or "").strip(),
        "company": str(raw.get("company_name") or raw.get("company") or "").strip(),
        "location": str(raw.get("location") or "").strip(),
        "salary": str(raw.get("salary") or "Not listed").strip() or "Not listed",
        "description": desc,
        "url": _first_apply_link(raw),
        "source": source,
        "date_posted": str(raw.get("date_posted") or TODAY),
    }


def is_relevant(job: dict) -> bool:
    """Title-based off-domain filter. Returns False for construction/civil and
    pure finance/banking roles that pollute these queries. Title-only by design:
    conservative enough that adjacent tech roles survive."""
    title = job.get("title", "").lower()
    return not any(term in title for term in TITLE_BLOCKLIST)


# ─────────────────────────────────────────────────────────────────────────────
# 3. STABLE PRIMARY KEY  — MD5 of (company + title + location)
# ─────────────────────────────────────────────────────────────────────────────

def job_key(job: dict) -> str:
    """
    Stable fingerprint. Lower-cased + stripped so trivial source differences
    ('Acme ' vs 'acme') don't fork one job into two records. This key — NOT the
    scrape order — is what keeps a 'Saved'/'Applied' status attached to a job
    across days. Changing this function invalidates the whole tracker, so don't.
    """
    basis = f"{job['company'].lower().strip()}" \
            f"{job['title'].lower().strip()}" \
            f"{job['location'].lower().strip()}"
    return hashlib.md5(basis.encode("utf-8")).hexdigest()[:12]


# ─────────────────────────────────────────────────────────────────────────────
# 4. MERGE  — refresh volatile metadata, PRESERVE all _tracking state
# ─────────────────────────────────────────────────────────────────────────────

def merge_into_tracker(tracker: dict, scraped: list[dict]) -> dict:
    """
    For each scraped job:
      • EXISTS  → refresh volatile fields (title/salary/url/last_seen) only.
                  _tracking and _snapshot are left exactly as they were. A job
                  you marked 'Applied' must never revert to 'New' on re-scrape.
      • NEW     → initialize _tracking (status 'New' + history) and cache the
                  full JD under _snapshot so Stage 2 works even after the URL
                  goes dead weeks later.
    """
    new_count = 0
    for job in scraped:
        key = job_key(job)

        if key in tracker:
            existing = tracker[key]
            # Volatile metadata only — safe to refresh.
            existing["title"] = job["title"]
            existing["company"] = job["company"]
            existing["location"] = job["location"]
            existing["salary"] = job["salary"]
            existing["url"] = job["url"]
            existing["source"] = job["source"]
            existing.setdefault("_tracking", {})["last_seen"] = TODAY
            # Intentionally DO NOT touch: application_status, status_history,
            # user_notes, stage2_ready, _snapshot, or any scoring fields.
        else:
            job["job_key"] = key
            job["_tracking"] = {
                "application_status": "New",
                "first_seen": TODAY,
                "last_seen": TODAY,
                "status_history": [{"status": "New", "date": TODAY}],
                "user_notes": "",
                "stage2_ready": False,
            }
            # Cache the JD once, at first sight — this is the offline source
            # for Stage 2 tailoring long after the live posting expires.
            job["_snapshot"] = {"full_description": job.get("description", "")}
            tracker[key] = job
            new_count += 1

    log.info("Merge complete: %d new, %d already tracked.",
             new_count, len(scraped) - new_count)
    return tracker


# ─────────────────────────────────────────────────────────────────────────────
# 5. SCORING  — batch only unscored jobs, inject CV, call Claude
# ─────────────────────────────────────────────────────────────────────────────

def job_entries(tracker: dict) -> list[dict]:
    """Real job objects only. The tracker root also holds non-job metadata keys
    like '_meta' (last_scrape_completed); those share the dict but must never be
    treated as jobs. Any key starting with '_' is metadata — job_keys are hex.
    This mirrors get_jobs() in dashboard.py; both iterators must agree."""
    return [v for k, v in tracker.items() if not k.startswith("_")]


def needs_scoring(entry: dict) -> bool:
    """A job needs scoring if it has no overall_match yet AND isn't terminal
    (we never burn tokens re-scoring Archived jobs)."""
    status = entry.get("_tracking", {}).get("application_status", "New")
    if status in TERMINAL_STATUSES:
        return False
    return "overall_match" not in entry


def build_system_prompt() -> str:
    """Load the Stage 1 instructions and inject the live CV text. The prompt
    must contain the literal placeholder {CV_TEXT}."""
    prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    cv_text = CV_PATH.read_text(encoding="utf-8")
    if "{CV_TEXT}" not in prompt:
        log.warning("system_prompt.txt has no {CV_TEXT} placeholder — "
                    "CV will not be injected. Add it.")
    return prompt.replace("{CV_TEXT}", cv_text)


def build_user_message(batch: list[dict]) -> str:
    """Render a batch into the delimited format the Stage 1 prompt expects.
    The job_key travels in the delimiter so Claude echoes it back verbatim."""
    parts = [f"Evaluate these jobs (scrape date {TODAY}):\n"]
    for entry in batch:
        parts.append(
            f"====JOB_KEY:{entry['job_key']}====\n"
            f"Title:    {entry.get('title','')}\n"
            f"Company:  {entry.get('company','')}\n"
            f"Location: {entry.get('location','')}\n"
            f"Source:   {entry.get('source','')}\n"
            f"URL:      {entry.get('url','')}\n\n"
            f"{entry.get('_snapshot', {}).get('full_description', entry.get('description',''))}\n"
            f"================\n"
        )
    return "\n".join(parts)


def parse_claude_json(raw_text: str) -> list[dict]:
    """Claude is instructed to return a bare JSON array. Parse it; if the model
    wrapped it in prose or fences anyway, extract the array as a fallback."""
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", raw_text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    log.error("Could not parse Claude output as JSON. First 300 chars:\n%s",
              raw_text[:300])
    return []


def score_batch(client: anthropic.Anthropic, system_prompt: str,
                batch: list[dict]) -> list[dict]:
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": build_user_message(batch)}],
    )
    # The API reports truncation explicitly via stop_reason. If the JSON array
    # was cut off because the output hit MAX_TOKENS, say so in plain language —
    # otherwise it only surfaces downstream as a vague "couldn't parse" error.
    if resp.stop_reason == "max_tokens":
        log.warning("Output hit max_tokens (%d) — JSON likely truncated for a "
                    "%d-job batch. Lower BATCH_SIZE or raise MAX_TOKENS.",
                    MAX_TOKENS, len(batch))
    text = resp.content[0].text
    return parse_claude_json(text)


# Fields Claude is allowed to write back. _tracking/_snapshot are excluded by
# design — even if a buggy model emits them, we never merge them here.
SCORING_FIELDS = (
    "overall_match", "persona_scores", "top_alignments",
    "key_gaps", "posting_quality", "action", "salary",
)


def apply_scores(tracker: dict, scored: list[dict]) -> int:
    """Merge Claude's scoring fields back onto the right record by job_key.
    Whitelisted fields only — this is the second line of defense protecting
    _tracking from accidental mutation."""
    applied = 0
    for s in scored:
        key = s.get("job_key")
        if not key or key not in tracker:
            log.warning("Scored job has unknown job_key '%s' — skipped.", key)
            continue
        for field in SCORING_FIELDS:
            if field in s:
                tracker[key][field] = s[field]
        tracker[key]["_last_scored"] = TODAY
        applied += 1
    return applied


def score_all_unscored(tracker: dict) -> dict:
    pending = [e for e in job_entries(tracker) if needs_scoring(e)]
    if not pending:
        log.info("Nothing to score — all jobs already evaluated.")
        return tracker

    log.info("Scoring %d unscored jobs in batches of %d.", len(pending), BATCH_SIZE)
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    system_prompt = build_system_prompt()

    for i in range(0, len(pending), BATCH_SIZE):
        batch = pending[i:i + BATCH_SIZE]
        n = i // BATCH_SIZE + 1
        try:
            scored = score_batch(client, system_prompt, batch)
            applied = apply_scores(tracker, scored)
            log.info("  Batch %d: scored %d / %d.", n, applied, len(batch))
        except Exception as e:
            # One failed batch shouldn't lose the others already scored.
            log.error("  Batch %d failed (%s) — left unscored for next run.", n, e)
    return tracker


# ─────────────────────────────────────────────────────────────────────────────
# 6. PERSIST  — dated backup + atomic write
# ─────────────────────────────────────────────────────────────────────────────

def save_tracker(tracker: dict) -> None:
    """
    Back up first, then write atomically. The temp-file + os.replace dance means
    a crash mid-write leaves the OLD file intact rather than a half-written,
    unparseable one. os.replace is atomic only within the same filesystem, so
    the temp file is created in the tracker's own directory.
    """
    BACKUP_DIR.mkdir(exist_ok=True)
    if TRACKER_PATH.exists():
        shutil.copy(TRACKER_PATH, BACKUP_DIR / f"job_tracker_{TODAY}.json")

    target_dir = TRACKER_PATH.resolve().parent
    with tempfile.NamedTemporaryFile(
        "w", dir=target_dir, delete=False, encoding="utf-8", suffix=".tmp"
    ) as tmp:
        json.dump(tracker, tmp, indent=2, ensure_ascii=False)
        tmp_path = tmp.name
    os.replace(tmp_path, TRACKER_PATH)  # atomic swap
    log.info("Tracker saved: %d total jobs.", len(tracker))


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def run() -> None:
    log.info("=== Daily job agent run: %s ===", TODAY)

    # The whole read-modify-write is wrapped in the SAME lock the dashboard
    # uses, so a 7am cron run and a live button click can't interleave.
    with LOCK:
        tracker = load_tracker()

        # Fetch from every source/query; isolate failures per call.
        scraped: list[dict] = []
        # English queries → both engines.
        for q in SEARCH_QUERIES:
            scraped += [normalize_job(j, "google_jobs") for j in fetch_serpapi_jobs(q)]
            scraped += [normalize_job(j, "jobspy") for j in fetch_jobspy_jobs(q)]
        # Hebrew queries → SerpApi/Google Jobs only (Jobspy returns little here).
        # recency=False: the English date phrase would pollute a Hebrew query.
        for q in HEBREW_QUERIES:
            scraped += [normalize_job(j, "google_jobs")
                        for j in fetch_serpapi_jobs(q, recency=False)]
        log.info("Fetched %d raw postings across %d EN + %d HE queries.",
                 len(scraped), len(SEARCH_QUERIES), len(HEBREW_QUERIES))

        # Drop blanks that can't form a stable key.
        scraped = [j for j in scraped if j["company"] and j["title"]]

        # Off-domain filter (construction / pure finance). Log the drop count so
        # an over-aggressive blocklist is visible rather than silent.
        before = len(scraped)
        scraped = [j for j in scraped if is_relevant(j)]
        log.info("Filtered %d off-domain jobs (blocklist); %d remain.",
                 before - len(scraped), len(scraped))

        tracker = merge_into_tracker(tracker, scraped)
        tracker = score_all_unscored(tracker)

        # Lightweight observability for the dashboard sidebar.
        tracker.setdefault("_meta", {})
        tracker["_meta"]["last_scrape_completed"] = (
            __import__("datetime").datetime.now().isoformat(timespec="seconds")
        )

        save_tracker(tracker)

    log.info("=== Run complete ===")


if __name__ == "__main__":
    run()
