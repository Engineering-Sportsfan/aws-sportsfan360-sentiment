import os
import time
from dotenv import load_dotenv
from apscheduler.schedulers.blocking import BlockingScheduler
from sentiment_engine import run_sentiment_engine
from firebase_store import save_report, get_latest_report
from datetime import datetime

load_dotenv()

RUNS_PER_DAY = int(os.getenv("RUNS_PER_DAY", 2))

SPORTS = [
    "FIFA_WC_2026",
    "WT20W_WC_2026"
]

def should_run_startup(sport: str, interval_hours: float) -> bool:
    try:
        latest = get_latest_report(sport)
        if not latest:
            return True
        timestamp_str = latest.get("timestamp")
        if not timestamp_str:
            return True
        last_run_time = datetime.strptime(timestamp_str, "%Y-%m-%d_%H-%M-%S")
        time_elapsed = datetime.now() - last_run_time
        
        # If less than 80% of the interval has elapsed, skip
        if time_elapsed.total_seconds() < (interval_hours * 3600 * 0.8):
            print(f"⏭️ Skipping startup run for {sport}. Last run was {time_elapsed.total_seconds()/60:.1f} mins ago (interval: {interval_hours}h)")
            return False
    except Exception as e:
        print(f"⚠️ Error checking last run: {e}")
    return True

def run_pipeline(sport: str):
    print(f"\n{'='*50}")
    print(f"🏃 Running sentiment pipeline — {sport}")
    print(f"{'='*50}")

    report = run_sentiment_engine(sport)

    if report:
        timestamp = save_report(report, sport)
        print(f"✅ Pipeline complete! Saved as: {timestamp}")
    else:
        print(f"❌ Pipeline failed for {sport}")

def run_all(is_startup: bool = False, interval_hours: float = 12.0):
    for sport in SPORTS:
        if is_startup and not should_run_startup(sport, interval_hours):
            continue
        run_pipeline(sport)

if __name__ == "__main__":
    interval_hours = 24 / RUNS_PER_DAY
    print(f"🕐 Scheduler starting — runs every {interval_hours}h ({RUNS_PER_DAY}x daily)")
    print(f"📋 Sports: {', '.join(SPORTS)}")

    # Run once immediately if needed
    print("▶️ Checking startup run requirements...")
    run_all(is_startup=True, interval_hours=interval_hours)

    # Schedule
    scheduler = BlockingScheduler()
    scheduler.add_job(run_all, 'interval', hours=interval_hours)

    print(f"✅ Scheduler running — next run in {interval_hours} hours")
    print("Press Ctrl+C to stop")

    try:
        scheduler.start()
    except KeyboardInterrupt:
        print("\n⏹️ Scheduler stopped")