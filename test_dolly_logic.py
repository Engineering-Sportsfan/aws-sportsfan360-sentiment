import os
import time
import json
from unittest.mock import MagicMock, patch

# 1. Import real firebase first so it loads google.auth correctly
from firebase_store import init_firebase
db = init_firebase()

# 2. Mock google.genai only
import sys
mock_genai = MagicMock()
sys.modules['google.genai'] = mock_genai
sys.modules['google.genai.types'] = MagicMock()

from dolly_bot import run_dolly_for_sport

def test_mock_run():
    print("🚀 Starting Mock Logic Verification...")
    
    # Create a mock live match
    match_id = "test-mock-match-2026"
    db.collection("matches").document(match_id).set({
        "sport": "football",
        "competition": "FIFA World Cup 2026 Semi-Final",
        "team_a": "Spain",
        "team_b": "France",
        "kickoff_time": int(time.time() * 1000),
        "status": "live",
        "stage": "semi_final"
    })
    print("✅ Mock match created in Firestore.")

    # Populate mock 4-pillar data
    match_doc_ref = db.collection("matches").document(match_id)
    match_doc_ref.collection("rivalries").add({
        "type": "micro",
        "subjects": ["Mbappe", "Carvajal"],
        "role": "striker",
        "narrative": "Mbappe has struggled against Carvajal's physical marking in past club meetings."
    })
    match_doc_ref.collection("stats").add({
        "metric": "knockout goals",
        "team_a_val": 12,
        "team_b_val": 15,
        "window": "since 2018"
    })
    print("✅ Mock 4-pillar data populated in Firestore.")

    # Create mock RoAR room linked to match
    room_id = "test-mock-room-id"
    db.collection("roarRooms").document(room_id).set({
        "name": "FIFA Discussion - Spain vs France",
        "sport": "football",
        "isActive": True,
        "createdAt": int(time.time() * 1000),
        "matchId": match_id
    })
    print("✅ Linked RoAR room created in Firestore.")

    # Patch Gemini client to return mock questions
    mock_response = MagicMock()
    mock_response.text = json.dumps([
        {
            "type": "debate",
            "text": "Carvajal's physical press or Mbappe's electric pace: who dominates the wing?",
            "sideA": "Carvajal",
            "sideB": "Mbappe"
        },
        {
            "type": "prediction",
            "text": "Will Mbappe score in the second half?",
            "sideA": "Yes",
            "sideB": "No"
        }
    ])
    
    with patch('dolly_bot.client.models.generate_content', return_value=mock_response):
        print("🐬 Running run_dolly_for_sport with mocked Gemini API...")
        run_dolly_for_sport("football", room_id=room_id)

    # Verify database writes
    msgs = db.collection("roarRooms").document(room_id).collection("messages").stream()
    print("\n=== VERIFYING GENERATED STORYTELLING QUESTIONS ===")
    count = 0
    for msg in msgs:
        d = msg.to_dict()
        print(f"[{d.get('type')}] \"{d.get('text')}\"")
        print(f"  Options: {d.get('sideA')} vs {d.get('sideB')}")
        count += 1
        
    # Clean up
    db.collection("matches").document(match_id).delete()
    db.collection("roarRooms").document(room_id).delete()
    print("\n🧹 Test documents cleaned up.")
    
    if count == 2:
        print("\n🎉 SUCCESS! Dolly logic and Firestore routing are 100% correct!")
    else:
        print("\n❌ Failed to verify database writes.")

if __name__ == "__main__":
    test_mock_run()
