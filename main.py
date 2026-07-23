from fastapi import FastAPI, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import os
from sentiment_engine import run_sentiment_engine
from firebase_store import save_report, get_latest_report, list_reports, get_report
from dolly_bot import dolly_auto_run_all_rooms, dolly_auto_run_all_cricket_rooms
from research_pipeline import run_match_research

load_dotenv()

app = FastAPI(title="SportsFan360 Sentiment Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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

@app.post("/run-dispatcher")
def run_dispatcher():
    """Manual trigger: runs the Central Bot Dispatcher for all sports across all rooms."""
    from bot_dispatcher import run_bot_dispatcher
    run_bot_dispatcher()
    return {"status": "success", "message": "Bot Dispatcher full run completed."}

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
    competition: str = Query(...)
):
    """Triggers automated pre-match LLM research grounding for a specific scheduled match."""
    success = run_match_research(match_id, team_a, team_b, sport, competition)
    if success:
        return {"status": "success", "message": f"Pre-match research pipeline completed for match [{match_id}]."}
    else:
        return {"status": "failed", "message": f"Pre-match research pipeline failed for match [{match_id}]."}

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

# Removed apscheduler thread logic; use EventBridge cron rules directly.

from mangum import Mangum
from bot_dispatcher import run_bot_dispatcher

mangum_handler = Mangum(app)

def handler(event, context):
    # Check if this is an EventBridge scheduled event (Cron Job)
    if event.get("source") == "aws.events":
        # Safe default: if no task is specified, default to bot_interval for backwards compatibility
        task = event.get("detail", {}).get("task", "bot_interval")
        
        if task == "sentiment_reports":
            print("EventBridge cron trigger detected. Running Sentiment Reports...")
            from sentiment_engine import run_sentiment_engine
            from firebase_store import save_report
            # Run for both major sports
            for sport in ["FIFA_WC_2026", "WT20W_WC_2026"]:
                report = run_sentiment_engine(sport)
                if report:
                    save_report(report, sport)
            return {"status": "success", "message": "Sentiment reports generated."}
        else:
            # Central Bot Dispatcher handles routing to Dolly, Krishna, Radha, etc.
            print(f"EventBridge cron trigger detected. Running {task} (Central Bot Dispatcher)...")
            run_bot_dispatcher()
            return {"status": "success", "message": f"{task} run completed."}
    
    # Otherwise, it's an API Gateway / Function URL HTTP request, route to FastAPI
    return mangum_handler(event, context)