"""
dashboard.py — Local Streamlit Career CRM for the job agent.

Run with:   streamlit run dashboard.py

Reads/writes the SAME job_tracker.json that job_agent.py produces, using the
SAME FileLock so a background scrape and a live click can't corrupt each other.

DIVISION OF AUTHORITY (must hold for concurrency safety):
  • job_agent.py  writes scoring fields + _snapshot, and NEVER touches _tracking.
  • dashboard.py  writes ONLY _tracking, and NEVER touches scoring/_snapshot.
Because the two processes write disjoint regions of each record, the file lock
only has to serialize the writes — it never has to resolve a logical conflict.

STREAMLIT MENTAL MODEL: the whole script re-runs top-to-bottom on every click.
So: (1) the tracker read is cached and explicitly cleared after any write, and
(2) the paid Stage 2 API call is cached per job_key so tab-switches don't re-bill.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime
from pathlib import Path

import anthropic
import streamlit as st
from filelock import FileLock

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG  — must match job_agent.py
# ─────────────────────────────────────────────────────────────────────────────

TRACKER_PATH = Path("job_tracker.json")
LOCK = FileLock("job_tracker.json.lock")          # SAME lock as the scraper
CV_PATH = Path("cv.txt")
STAGE2_PROMPT_PATH = Path("stage2_system_prompt.txt")   # optional override

CLAUDE_MODEL = "claude-sonnet-4-6"
STAGE2_MAX_TOKENS = 2500

STATUSES = ["New", "Saved for Later", "Applied", "Interviewing", "Archived"]
ACTIVE_PIPELINE = {"Applied", "Interviewing"}
TODAY = date.today()

TIER_META = {
    "HIGH":   ("🟢", "HIGH MATCH — Apply Now"),
    "MEDIUM": ("🟡", "MEDIUM MATCH — Worth a Look"),
}

st.set_page_config(page_title="Career CRM", page_icon="🎯", layout="wide")


# ─────────────────────────────────────────────────────────────────────────────
# READ PATH  — cached so constant re-runs don't hammer the disk
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def load_tracker() -> dict:
    """Cached read. MUST be invalidated (load_tracker.clear()) after every write,
    or the UI will keep rendering pre-write state."""
    if not TRACKER_PATH.exists():
        return {}
    try:
        text = TRACKER_PATH.read_text(encoding="utf-8").strip()
        return json.loads(text) if text else {}
    except json.JSONDecodeError:
        st.error("job_tracker.json is corrupt. Restore from backups/ and reload.")
        return {}


def get_jobs(tracker: dict) -> list[dict]:
    """All real job objects. Skips the root '_meta' key (it's not a job) — this
    is the gotcha flagged when job_agent started writing _meta to the root."""
    return [v for k, v in tracker.items() if k != "_meta"]


def tracking(entry: dict) -> dict:
    """Defensive accessor — older records may predate some _tracking fields."""
    return entry.get("_tracking", {})


def days_in_stage(entry: dict) -> int:
    """Days since the most recent status change, from status_history."""
    hist = tracking(entry).get("status_history", [])
    if not hist:
        return 0
    try:
        last = date.fromisoformat(hist[-1]["date"])
        return (TODAY - last).days
    except (ValueError, KeyError):
        return 0


def next_action(entry: dict) -> str:
    """Local heuristic for the CRM nudge column. (Earlier we discussed having
    the daily Claude call generate this; until that field exists in the schema,
    this rule-based version keeps the column useful with zero token cost.)"""
    status = tracking(entry).get("application_status", "New")
    d = days_in_stage(entry)
    if status == "Applied":
        return "⏰ Follow up — overdue" if d >= 7 else f"Wait / follow up ({7 - d}d)"
    if status == "Interviewing":
        return "✍️ Prep + send thank-you note"
    if status == "Saved for Later":
        return f"⏳ Apply soon (saved {d}d ago)"
    return "—"


def job_date(entry: dict) -> str:
    """Best-effort posting date for sorting (newest first).
    Prefers date_posted from the scraper; falls back to first_seen from _tracking.
    Returns an ISO string so lexicographic sort == chronological sort."""
    dp = str(entry.get("date_posted") or "").strip()
    if dp and dp not in ("Not listed", "None", ""):
        return dp
    return tracking(entry).get("first_seen", "1970-01-01")


def classify_title(entry: dict) -> str:
    """Keyword-based job type label. Uses detected_persona if stored, otherwise
    derives it from the title. Returns a display-friendly string."""
    LABELS = {
        "data_analyst":    "📊 Data / BI",
        "product_manager": "🧩 Product",
        "project_manager": "📋 Project / PMO",
        "operations":      "⚙️ Operations",
        "other":           "🔗 Other",
    }
    # Prefer scored primary persona if available
    personas = entry.get("persona_scores", {})
    primary = next((p for p, v in personas.items() if v.get("primary")), "")
    if not primary:
        primary = entry.get("detected_persona", "other")
    return LABELS.get(primary, "🔗 Other")


# ─────────────────────────────────────────────────────────────────────────────
# WRITE PATH  — lock, read FRESH inside lock, patch ONLY _tracking, atomic write
# ─────────────────────────────────────────────────────────────────────────────

def _atomic_write(tracker: dict) -> None:
    target_dir = TRACKER_PATH.resolve().parent
    with tempfile.NamedTemporaryFile(
        "w", dir=target_dir, delete=False, encoding="utf-8", suffix=".tmp"
    ) as tmp:
        json.dump(tracker, tmp, indent=2, ensure_ascii=False)
        tmp_path = tmp.name
    os.replace(tmp_path, TRACKER_PATH)            # atomic swap


def update_status_on_disk(job_key: str, new_status: str) -> None:
    """
    The single mutation entry point. Critical ordering:
      1. acquire the shared lock
      2. read the tracker FRESH from disk (NOT the value Streamlit rendered from —
         the scraper may have rewritten the file since this page loaded)
      3. patch only the _tracking sub-object for this one job
      4. atomic write, release lock
    Patching the single field rather than writing back the rendered object is what
    prevents a stale full-object write from clobbering a concurrent change.
    """
    with LOCK:
        tracker = json.loads(TRACKER_PATH.read_text(encoding="utf-8"))
        entry = tracker.get(job_key)
        if entry is None:
            return
        trk = entry.setdefault("_tracking", {})
        if trk.get("application_status") == new_status:
            return                                 # no-op; avoids junk history
        trk["application_status"] = new_status
        trk.setdefault("status_history", []).append(
            {"status": new_status, "date": TODAY.isoformat()}
        )
        _atomic_write(tracker)

    load_tracker.clear()                           # invalidate the cached read


STALE_DAYS = 4   # jobs sitting in "New" longer than this are considered stale


def archive_stale_jobs() -> int:
    """Move all New jobs older than STALE_DAYS to Archived. Returns count moved."""
    cutoff = date.today()
    moved = 0
    with LOCK:
        tracker = json.loads(TRACKER_PATH.read_text(encoding="utf-8"))
        for entry in tracker.values():
            if not isinstance(entry, dict):
                continue
            trk = entry.get("_tracking", {})
            if trk.get("application_status") != "New":
                continue
            first_seen_str = trk.get("first_seen", "")
            try:
                age = (cutoff - date.fromisoformat(first_seen_str)).days
            except ValueError:
                continue
            if age >= STALE_DAYS:
                trk["application_status"] = "Archived"
                trk.setdefault("status_history", []).append(
                    {"status": "Archived", "date": cutoff.isoformat()}
                )
                moved += 1
        if moved:
            _atomic_write(tracker)
    if moved:
        load_tracker.clear()
    return moved

def status_dropdown(entry: dict, scope: str) -> None:
    """Render a status <select> for one job. On change, persist + rerun.
    `scope` keeps Streamlit widget keys unique across tabs (same job can appear
    in more than one tab)."""
    key = entry.get("job_key", "")
    current = tracking(entry).get("application_status", "New")
    idx = STATUSES.index(current) if current in STATUSES else 0
    chosen = st.selectbox(
        "Status", STATUSES, index=idx, key=f"status_{scope}_{key}",
        label_visibility="collapsed",
    )
    if chosen != current:
        update_status_on_disk(key, chosen)
        st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 2  — live Claude call, cached per job_key so it never re-bills
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_STAGE2_PROMPT = """You are in Career Consultant mode for a single candidate.
The candidate's General CV is below. You are given ONE target job (its full
description and its Stage 1 scoring summary). Produce a precise, actionable
briefing so the candidate can edit their CV themselves in under 30 minutes.
Do NOT rewrite the whole CV. Output in Markdown using EXACTLY these sections:

## ATS Keyword Audit
For each important keyword in the JD, classify it:
- ✅ Already in CV  - ⚠️ Partially present (give rephrase)
- ❌ Missing but learnable from IE&M background (say how to add)
- 🚫 Genuinely missing (acknowledge honestly)

## Section Reordering
Which CV sections/bullets to move UP for this role, and why.

## Bullet Point Rewrites
3–5 existing bullets to rewrite. Show BEFORE: / AFTER: with target keywords
embedded naturally.

## IE&M Academic Leverage
Which coursework, capstone projects, or IE&M frameworks (Six Sigma, DMAIC, Lean,
simulation/OR) to surface for THIS role.

## Honest Gap Assessment
Gaps that can't be papered over. Recommend: apply anyway / address in cover
letter / skip.

<candidate_cv>
{CV_TEXT}
</candidate_cv>"""


def _stage2_system_prompt() -> str:
    base = (STAGE2_PROMPT_PATH.read_text(encoding="utf-8")
            if STAGE2_PROMPT_PATH.exists() else DEFAULT_STAGE2_PROMPT)
    cv = CV_PATH.read_text(encoding="utf-8") if CV_PATH.exists() else "(CV not found)"
    return base.replace("{CV_TEXT}", cv)


@st.cache_data(show_spinner="✨ Consulting Claude…")
def get_tailoring(job_key: str, jd_text: str, title: str, company: str,
                  scoring_summary: str) -> str:
    """
    Cached on job_key (+ inputs): the FIRST click pays for the call; every later
    render of the same job returns the cached briefing for free. job_key is the
    cache identity, so two different jobs never collide.
    """
    client = anthropic.Anthropic()                 # reads ANTHROPIC_API_KEY
    user_msg = (
        f"TARGET JOB: {title} @ {company}\n\n"
        f"STAGE 1 SCORING SUMMARY:\n{scoring_summary}\n\n"
        f"FULL JOB DESCRIPTION:\n{jd_text}"
    )
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=STAGE2_MAX_TOKENS,
        system=_stage2_system_prompt(),
        messages=[{"role": "user", "content": user_msg}],
    )
    return resp.content[0].text


def scoring_summary(entry: dict) -> str:
    om = entry.get("overall_match", {})
    personas = entry.get("persona_scores", {})
    primary = next((p for p, v in personas.items() if v.get("primary")), "—")
    return (
        f"Overall: {om.get('tier','?')} ({om.get('score','?')}/10) | "
        f"Primary persona: {primary}\n"
        f"Alignments: {'; '.join(entry.get('top_alignments', [])) or '—'}\n"
        f"Gaps: {'; '.join(entry.get('key_gaps', [])) or '—'}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# CARD RENDERER  (shared by Firehose & Watchlist)
# ─────────────────────────────────────────────────────────────────────────────

def render_job_card(entry: dict, scope: str, show_aging: bool = False) -> None:
    om      = entry.get("overall_match", {})
    scored  = bool(om)
    jtype   = classify_title(entry)
    source  = entry.get("source", "")

    with st.container(border=True):
        top, ctrl = st.columns([4, 1])
        with top:
            st.markdown(f"**{entry.get('title','(untitled)')}** — "
                        f"{entry.get('company','?')}")
            meta = f"📍 {entry.get('location','?')}  ·  {jtype}"
            if scored:
                meta += f"  ·  🎯 {om.get('score','?')}/10"
            if source:
                meta += f"  ·  🔎 {source}"
            if show_aging:
                meta += f"  ·  ⏳ saved {days_in_stage(entry)}d ago"
            st.caption(meta)
        with ctrl:
            status_dropdown(entry, scope)

        if scored:
            # Show Claude alignments & gaps when available
            aligns = entry.get("top_alignments", [])
            gaps   = entry.get("key_gaps", [])
            if aligns:
                st.markdown("✅ " + "  ·  ".join(aligns))
            if gaps:
                st.markdown("⚠️ " + "  ·  ".join(gaps))
        else:
            # Show description snippet when no scoring
            desc = entry.get("description", "")
            if desc:
                snippet = desc[:220].strip().replace("\n", " ")
                st.caption(snippet + ("…" if len(desc) > 220 else ""))

        if entry.get("url"):
            st.markdown(f"[↗ View / Apply]({entry['url']})")


# ─────────────────────────────────────────────────────────────────────────────
# APP
# ─────────────────────────────────────────────────────────────────────────────

tracker = load_tracker()
jobs = get_jobs(tracker)

# Buckets
by_status: dict[str, list[dict]] = {s: [] for s in STATUSES}
for j in jobs:
    by_status[tracking(j).get("application_status", "New")].append(j)

# ── Sidebar: observability + manual refresh ─────────────────────────────────
with st.sidebar:
    st.header("🎯 Career CRM")
    meta = tracker.get("_meta", {})
    last = meta.get("last_scrape_completed")
    if last:
        try:
            pretty = datetime.fromisoformat(last).strftime("%b %d, %H:%M")
            st.caption(f"🕒 Jobs as of {pretty}")
        except ValueError:
            st.caption(f"🕒 Last scrape: {last}")
    else:
        st.caption("🕒 No scrape recorded yet — run job_agent.py")

    st.metric("Total tracked", len(jobs))
    st.metric("🔥 Active pipeline",
              len(by_status["Applied"]) + len(by_status["Interviewing"]))
    st.metric("👀 Watchlist", len(by_status["Saved for Later"]))
    st.metric("✨ New", len(by_status["New"]))

    if st.button("🔄 Reload from disk"):
        load_tracker.clear()
        st.rerun()

# ── Tabs ─────────────────────────────────────────────────────────────────────
tab_fire, tab_crm, tab_ai = st.tabs(
    ["🔥 Morning Firehose", "📋 Career CRM", "✨ AI Consultant"]
)

# ── TAB 1: MORNING FIREHOSE  (status == New only) ───────────────────────────
with tab_fire:
    new_jobs = by_status["New"]

    # ── Stale jobs panel ────────────────────────────────────────────────────
    stale = [j for j in new_jobs
             if (TODAY - date.fromisoformat(
                 tracking(j).get("first_seen", TODAY.isoformat())
             )).days >= STALE_DAYS]

    if stale:
        sc1, sc2, sc3 = st.columns([2.5, 2, 2])
        with sc1:
            st.warning(f"⏰ {len(stale)} משרות ישנות (4+ ימים ב-New)")
        with sc2:
            if st.button("העבר משרות ישנות לארכיון"):
                n = archive_stale_jobs()
                st.success(f"הועברו {n} משרות לארכיון.")
                st.rerun()
        with sc3:
            show_stale = st.toggle("הצג משרות ישנות בלבד", value=False)
    else:
        show_stale = False

    st.divider()

    if not new_jobs:
        st.info("No new unactioned jobs. Run job_agent.py to scrape today's batch.")
    else:
        # ── Controls row ────────────────────────────────────────────────────
        f1, f2 = st.columns([2, 2])

        with f1:
            persona_labels = {
                "הכל":             "הכל",
                "data_analyst":    "📊 Data / BI",
                "product_manager": "🧩 Product",
                "project_manager": "📋 Project / PMO",
                "operations":      "⚙️ Operations",
                "other":           "🔗 Other",
            }
            persona_filter = st.selectbox(
                "סוג משרה",
                options=list(persona_labels.keys()),
                format_func=lambda x: persona_labels[x],
                key="fire_persona",
            )

        with f2:
            last_scrape_date = ""
            if meta.get("last_scrape_completed"):
                try:
                    last_scrape_date = datetime.fromisoformat(
                        meta["last_scrape_completed"]
                    ).date().isoformat()
                except ValueError:
                    pass
            last_run_only = st.toggle(
                f"🔁 ריצה אחרונה בלבד ({last_scrape_date or '—'})",
                value=False,
                key="fire_lastrun",
            )

        # ── Apply filters ────────────────────────────────────────────────────
        filtered = new_jobs

        if show_stale:
            filtered = stale
        elif last_run_only and last_scrape_date:
            filtered = [j for j in filtered
                        if tracking(j).get("first_seen", "") == last_scrape_date]

        if persona_filter != "הכל":
            filtered = [j for j in filtered
                        if classify_title(j) == persona_labels[persona_filter]]

        # Sort by date (newest first) always
        filtered = sorted(
            filtered,
            key=lambda x: job_date(x),
            reverse=True,
        )

        # ── Render ───────────────────────────────────────────────────────────
        if not filtered:
            st.info("אין משרות שמתאימות לסינון הנוכחי.")
        else:
            st.caption(f"{len(filtered)} משרות מוצגות.")
            for j in filtered:
                render_job_card(j, scope="fire")

# ── TAB 2: CAREER CRM  (Active pipeline + Watchlist) ────────────────────────
with tab_crm:
    st.subheader("🔥 Active Pipeline")
    pipeline = by_status["Applied"] + by_status["Interviewing"]
    if not pipeline:
        st.info("Nothing in the pipeline yet. Mark a job 'Applied' to start tracking.")
    else:
        # Sort: longest in stage first (the most likely to need a nudge)
        for j in sorted(pipeline, key=days_in_stage, reverse=True):
            with st.container(border=True):
                c1, c2, c3, c4 = st.columns([3, 1, 2, 1.3])
                with c1:
                    st.markdown(f"**{j.get('title','?')}** — {j.get('company','?')}")
                    st.caption(f"📍 {j.get('location','?')}")
                with c2:
                    st.metric("Days in stage", days_in_stage(j))
                with c3:
                    st.markdown("**Next action**")
                    st.markdown(next_action(j))
                with c4:
                    status_dropdown(j, scope="crm")

    st.divider()
    st.subheader("👀 Watchlist — Saved, Not Yet Applied")
    watch = by_status["Saved for Later"]
    if not watch:
        st.info("No saved jobs. Use the Firehose to save interesting roles here.")
    else:
        for j in sorted(watch, key=days_in_stage, reverse=True):
            render_job_card(j, scope="watch", show_aging=True)

# ── TAB 3: AI CONSULTANT  (Stage 2 tailoring) ───────────────────────────────
with tab_ai:
    st.subheader("✨ Tailor My CV for a Role")
    if not jobs:
        st.info("No jobs tracked yet.")
    else:
        # Only show jobs that have a cached JD snapshot — older entries that
        # predate snapshot caching can't be tailored and are excluded here.
        tailorable = [j for j in jobs if j.get("_snapshot", {}).get("full_description")]
        if not tailorable:
            st.info("No jobs with cached descriptions yet. Run job_agent.py first.")
        else:
            labelled = sorted(
                tailorable,
                key=lambda x: x.get("overall_match", {}).get("score", 0), reverse=True,
            )
            options = {
                f"{j.get('title','?')} — {j.get('company','?')} "
                f"({j.get('overall_match',{}).get('score','?')}/10)": j
                for j in labelled
            }
            choice = st.selectbox("Choose a job", list(options.keys()))
            chosen = options[choice]

            with st.container(border=True):
                st.markdown(f"**{chosen.get('title','?')}** — {chosen.get('company','?')}")
                st.caption(scoring_summary(chosen).replace("\n", "  ·  "))

            jd = chosen.get("_snapshot", {}).get("full_description", "")
            job_key = chosen.get("job_key", "")

            if st.button("✨ Tailor My CV for this Role", type="primary"):
                briefing = get_tailoring(
                    job_key, jd, chosen.get("title", ""), chosen.get("company", ""),
                    scoring_summary(chosen),
                )
                st.markdown(briefing)
                st.caption("💡 Cached for this role — re-opening won't re-bill the API.")
