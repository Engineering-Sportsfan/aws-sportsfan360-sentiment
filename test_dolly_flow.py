import os
import time
from firebase_store import init_firebase
from research_pipeline import run_match_research
from dolly_bot import run_dolly_for_sport

# Ensure environment variables are loaded
os.environ["GEMINI_API_KEY"] = os.getenv("GEMINI_API_KEY", "")

db = init_firebase()

def run_test():
    print("🚀 Starting End-to-End Dolly Upgrade Dry-Run...")
    
    # 1. Create a mock live match
    match_id = "test-spain-france-2026"
    db.collection("matches").document(match_id).set({
        "sport": "football",
        "competition": "FIFA World Cup 2026 Semi-Final",
        "team_a": "Spain",
        "team_b": "France",
        "kickoff_time": int(time.time() * 1000),
        "status": "live",
        "stage": "semi_final"
    })
    print("✅ Mock live match created in matches collection.")

    # 2. Run the automated pre-match research pipeline
    success = run_match_research(
        match_id=match_id,
        team_a="Spain",
        team_b="France",
        sport="football",
        competition="FIFA World Cup 2026 Semi-Final"
    )
    if not success:
        print("❌ Research pipeline failed.")
        return

    # 3. Create a mock RoAR room and link the match
    room_id = "test-roar-room-spain-france"
    db.collection("roarRooms").document(room_id).set({
        "name": "FIFA Discussion - Spain vs France Semi-Final",
        "sport": "football",
        "isActive": True,
        "createdAt": int(time.time() * 1000),
        "matchId": match_id
    })
    print("✅ Linked RoAR room created in Firestore.")

    # 4. Trigger Dolly core runner
    print("🐬 Triggering Dolly core runner...")
    run_dolly_for_sport("football", room_id=room_id)
    
    # 5. Verify questions were written to Firestore messages
    msgs = db.collection("roarRooms").document(room_id).collection("messages").stream()
    print("\n=== VERIFYING GENERATED STORYTELLING QUESTIONS ===")
    count = 0
    for msg in msgs:
        d = msg.to_dict()
        print(f"[{d.get('type')}] \"{d.get('text')}\"")
        print(f"  Options: {d.get('sideA')} vs {d.get('sideB')}")
        count += 1
    
    if count >= 2:
        print("\n🎉 SUCCESS! E2E Dolly Upgrade is fully verified and working perfectly!")
    else:
        print("\n❌ Failed to generate storytelling questions.")

    # Clean up test documents
    db.collection("matches").document(match_id).delete()
    db.collection("roarRooms").document(room_id).delete()
    print("🧹 Test documents cleaned up.")

if __name__ == "__main__":
    run_test()
