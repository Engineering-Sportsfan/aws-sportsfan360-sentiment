from fastapi import FastAPI, BackgroundTasks, Query, Depends, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import threading
import os
from apscheduler.schedulers.blocking import BlockingScheduler
from sentiment_engine import run_sentiment_engine
from firebase_store import save_report, get_latest_report, list_reports, get_report
from dolly_bot import dolly_auto_run_all_rooms, dolly_auto_run_all_cricket_rooms
from research_pipeline import run_match_research
from athlete_pipeline import run_athlete_pipeline
from athlete_schemas import TriggerType
import firebase_store

load_dotenv()

app = FastAPI(title="SportsFan360 Sentiment Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def require_pipeline_key(x_api_key: str = Header(None)):
    expected = os.getenv("ATHLETE_PIPELINE_KEY")
    if not expected or x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return True


@app.get("/health")
def health():
    return {"status": "ok", "service": "SportsFan360 Sentiment Engine"}

@app.post("/run-now")
def run_now(sport: str = "FIFA_WC_2026"):
    report = run_sentiment_engine(sport)
    if report:
        timestamp = save_report(report, sport)
        return {"status": "success", "saved_as": timestamp, "sport": sport}
    return {"status": "failed"}

@app.post("/run-dolly")
def run_dolly(background_tasks: BackgroundTasks):
    """Manual trigger: runs Dolly for all sports (cricket + football) across all rooms."""
    background_tasks.add_task(dolly_auto_run_all_rooms)
    return {"status": "triggered", "message": "Dolly full run started in background (cricket + football)."}

@app.get("/test-generation")
def test_generation():
    """Diagnostic endpoint to run a mock Dolly generation and see the questions instantly."""
    from dolly_bot import generate_questions
    mock_match = {
        "teams": "India vs England",
        "tournament": "T20I Series 2026",
        "venue": "Manchester",
        "phase": "IN-PLAY",
        "liveScore": "142/4 (16.2 overs) - India batting",
        "keyPlayers": "Suryakumar Yadav, Hardik Pandya, Jofra Archer, Jos Buttler",
        "format": "T20"
    }
    try:
        polls = generate_questions(mock_match, "cricket", "")
        return {"status": "success", "generated_questions": polls}
    except Exception as e:
        return {"status": "error", "error_message": str(e)}

@app.post("/run-research")
def run_research(
    match_id: str = Query(...), 
    team_a: str = Query(...), 
    team_b: str = Query(...), 
    sport: str = Query(...), 
    competition: str = Query(...), 
    background_tasks: BackgroundTasks = None
):
    """Triggers automated pre-match LLM research grounding for a specific scheduled match."""
    # Ensure background_tasks is initialized
    if not background_tasks:
        from fastapi import BackgroundTasks as FastAPITasks
        background_tasks = FastAPITasks()
    background_tasks.add_task(run_match_research, match_id, team_a, team_b, sport, competition)
    return {"status": "triggered", "message": f"Pre-match research pipeline started for match [{match_id}]."}

@app.get("/latest")
def latest(sport: str = "FIFA_WC_2026"):
    report = get_latest_report(sport)
    if report:
        return report
    return {"status": "no reports yet"}

@app.get("/list-reports")
def api_list_reports(sport: str = "FIFA_WC_2026", limit: int = 50):
    return {"reports": list_reports(sport, limit)}

@app.get("/get-report")
def api_get_report(sport: str = "FIFA_WC_2026", timestamp: str = None):
    report = get_report(sport, timestamp)
    if report:
        return report
    return {"status": "error", "message": f"Report not found for timestamp: {timestamp}"}


@app.post("/run-athlete-pipeline")
def run_athlete_pipeline_endpoint(
    athlete_id: str = Query(...),
    sport: str = Query(...),
    athlete_name: str = Query(...),
    trigger: str = Query(
        "manual_recheck",
        description="new_athlete_onboarding | scheduled_recheck | manual_recheck",
    ),
    _auth: bool = Depends(require_pipeline_key),
):
    """
    On-demand trigger — admin panel 'add athlete' (trigger=new_athlete_onboarding)
    or 'recheck now' (trigger=manual_recheck). Runs synchronously so the admin
    UI can show the result (drafted / error) immediately.

    No more athlete_source_id / is_new_athlete / fingerprint check — Gemini's
    search grounding looks the athlete up by name directly.
    """
    try:
        trigger_type = TriggerType(trigger)
    except ValueError:
        return {"status": "error", "message": f"Unknown trigger '{trigger}'"}

    result = run_athlete_pipeline(athlete_id, sport, athlete_name, trigger_type)
    return result.to_dict()


@app.get("/athlete-review-queue")
def api_list_pending_reviews(
    sport: str = None,
    limit: int = 50,
    _auth: bool = Depends(require_pipeline_key),
):
    """Admin panel review-queue screen reads from here."""
    return {"drafts": firebase_store.list_pending_reviews(sport, limit)}


@app.get("/athlete-review-queue/{draft_id}")
def api_get_review_draft(draft_id: str, _auth: bool = Depends(require_pipeline_key)):
    draft = firebase_store.get_review_draft(draft_id)
    if draft:
        return draft
    return {"status": "error", "message": f"Draft not found: {draft_id}"}


@app.post("/athlete-review-queue/{draft_id}/resolve")
def api_resolve_review_draft(
    draft_id: str,
    decision: str = Query(..., description="approved | rejected | edited"),
    reviewed_by: str = Query(...),
    final_data: dict = None,
    _auth: bool = Depends(require_pipeline_key),
):
    """
    Human approve/edit/reject. Only this endpoint can move a draft's data
    onto the live athlete record, and only on approved/edited.
    """
    if decision not in ("approved", "rejected", "edited"):
        return {"status": "error", "message": "decision must be approved, rejected, or edited"}
    ok = firebase_store.resolve_review_draft(draft_id, decision, reviewed_by, final_data)
    return {"status": "success" if ok else "error", "draft_id": draft_id, "decision": decision}


def start_scheduler():
    runs_per_day = int(os.getenv("RUNS_PER_DAY", 2))
    interval_hours = 24 / runs_per_day
    scheduler = BlockingScheduler()
    
    def run_all_sports():
        print("⏰ Starting scheduled background run for all sports...")
        for sport in ["FIFA_WC_2026", "WT20W_WC_2026"]:
            try:
                report = run_sentiment_engine(sport)
                if report:
                    save_report(report, sport)
            except Exception as e:
                print(f"❌ Scheduled run error for {sport}: {e}")
                
    scheduler.add_job(
        run_all_sports,
        'interval',
        hours=interval_hours
    )

    # ── Dolly Auto Schedule: every 15 minutes ──────────────────────────────
    # - Pre-match is completely disabled (handled by backend team).
    # - During a live match (In-Play) Dolly posts every 15 minutes.
    # - Post-match Dolly posts exactly 1 final post.
    scheduler.add_job(
        dolly_auto_run_all_rooms,
        'interval',
        minutes=15,
        id="dolly_interval"
    )

    print("🐬 Dolly auto-scheduled: every 15 minutes.")

    #  Athlete Data Automation: scheduled recheck sweep 
    # No more fingerprint/change-detection — Gemini re-drafts every tracked
    # athlete on a slow weekly cadence (steady-state updates). Fast-moving
    # events (e.g. an athlete winning a medal) are handled separately by the
    # admin panel's manual "recheck now" button (trigger=manual_recheck on
    # /run-athlete-pipeline), not by this sweep.

    def run_athlete_recheck_sweep():
        print("⏰ Starting scheduled athlete data recheck sweep...")
        try:
            athletes = firebase_store.list_automated_athletes()
        except Exception as e:
            print(f"❌ Could not list automated athletes: {e}")
            return
        for athlete in athletes:
            try:
                result = run_athlete_pipeline(
                    athlete_id=athlete["id"],
                    sport=athlete.get("sport"),
                    athlete_name=athlete.get("name"),
                    trigger_type=TriggerType.SCHEDULED_RECHECK,
                )
                print(f"  {athlete['id']}: {result.status} — {result.message}")
            except Exception as e:
                print(f"❌ Athlete recheck error for {athlete.get('id')}: {e}")

    athlete_recheck_days = int(os.getenv("ATHLETE_RECHECK_INTERVAL_DAYS", 7))
    scheduler.add_job(
        run_athlete_recheck_sweep,
        'interval',
        days=athlete_recheck_days,
        id="athlete_recheck_sweep",
    )
    print(f"🏃 Athlete data recheck sweep scheduled: every {athlete_recheck_days} days.")

    scheduler.start()

# Start scheduler in background when server starts
thread = threading.Thread(target=start_scheduler, daemon=True)
thread.start()