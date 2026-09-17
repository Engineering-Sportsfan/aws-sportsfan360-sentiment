from fastapi import FastAPI, BackgroundTasks, Query, Depends, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import os
# from sentiment_engine import run_sentiment_engine
from firebase_store import save_report, get_latest_report, list_reports, get_report
from dolly_bot import dolly_auto_run_all_rooms
from research_pipeline import run_match_research
# from athlete_pipeline import run_athlete_pipeline
from athlete_pipeline import generate_athlete_profile
from athlete_schemas import TriggerType
import firebase_store
from typing import Optional


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
    from sentiment_engine import run_sentiment_engine
    report = run_sentiment_engine(sport)
    if report:
        timestamp = save_report(report, sport)
        return {"status": "success", "saved_as": timestamp, "sport": sport}
    return {"status": "failed"}

@app.post("/run-dispatcher")
def run_dispatcher():
    """Manual trigger: runs the Central Bot Dispatcher for all sports across all rooms."""
    from bot_dispatcher import run_bot_dispatcher
    run_bot_dispatcher()
    return {"status": "success", "message": "Bot Dispatcher full run completed."}

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



@app.post("/run-athlete-pipeline")
async def run_athlete_pipeline_endpoint(
    athlete_id: str = Query(..., description="Unique slug ID of the athlete"),
    athlete_name: str = Query(..., description="Full name of the athlete"),
    sport: Optional[str] = Query(None, description="Sport or auto-detected"),
    _auth: bool = Depends(require_pipeline_key),
):
    """
    On-demand athlete profile generation powered by Gemini with Google Search grounding.
    Writes directly to Firestore and returns the generated profile payload.
    """
    resolved_sport = sport.strip() if sport and sport.strip() else "auto"
    result = generate_athlete_profile(athlete_name=athlete_name.strip(), sport=resolved_sport)
    return result


# ── Mangum AWS Lambda Handler ───────────────────────────────────────────────
from mangum import Mangum
from bot_dispatcher import run_bot_dispatcher

mangum_handler = Mangum(app)

def handler(event, context):
    # Check if this is an EventBridge scheduled event (Cron Job)
    if event.get("source") == "aws.events":
        task = event.get("detail", {}).get("task", "bot_interval")
        
        if task == "sentiment_reports":
            print("EventBridge cron trigger detected. Running Sentiment Reports...")
            # Run for both major sports
            for sport in ["FIFA_WC_2026", "WT20W_WC_2026"]:
                report = run_sentiment_engine(sport)
                if report:
                    save_report(report, sport)
            return {"status": "success", "message": "Sentiment reports generated."}
        elif task == "athlete_recheck_sweep":
            print("EventBridge cron trigger detected. Running Athlete Recheck Sweep...")
            try:
                athletes = firebase_store.list_automated_athletes()
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
            except Exception as e:
                print(f"❌ Could not list automated athletes: {e}")
            return {"status": "success", "message": "Athlete recheck sweep completed."}
        else:
            # Central Bot Dispatcher handles routing to Dolly, Krishna, Radha, etc.
            print(f"EventBridge cron trigger detected. Running {task} (Central Bot Dispatcher)...")
            run_bot_dispatcher()
            return {"status": "success", "message": f"{task} run completed."}
    
    # Otherwise, it's an API Gateway / Function URL HTTP request, route to FastAPI
    return mangum_handler(event, context)
