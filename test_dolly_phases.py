import json
import time
from unittest.mock import MagicMock, patch

# ── 1. Mock DB Functions BEFORE importing dolly_bot ──
patch('dolly_bot.init_firebase', return_value=MagicMock()).start()
patch('dolly_bot.db_check_phase_lock', return_value=False).start()
patch('dolly_bot.db_stamp_phase_lock').start()
patch('dolly_bot.db_was_recently_posted', return_value=False).start()

# ── 2. Import dolly_bot ──
from dolly_bot import run_dolly_for_sport

# ── 3. Mock Gemini API ──
mock_genai = MagicMock()
import sys
sys.modules['google.genai'] = mock_genai
sys.modules['google.genai.types'] = MagicMock()

def test_mock_run():
    print("🚀 Starting PURE MOCK Verification (No Database Needed)...")
    
    # ── TEST 1: PRE-MATCH ──
    print("\n⏳ Testing PRE-MATCH Phase...")
    mock_room_pre = {"isTestingRoom": True, "matchId": "mock-match-pre"}
    mock_match_pre = {
        "team_a": "India",
        "team_b": "Australia",
        "kickoff_time": int((time.time() + 3600) * 1000), # 1 hr in future
        "status": "upcoming"
    }

    mock_response_pre = MagicMock()
    mock_response_pre.text = json.dumps([
        {
            "type": "analysis",
            "title": "Pre-Match Read",
            "text": "India looks strong, but Australia's bowling attack could be the difference today."
        },
        {
            "type": "story",
            "title": "Story: The Ultimate Rivalry",
            "text": "Whenever these two meet, sparks fly. Can Kohli silence Cummins today?"
        }
    ])
    
    with patch('dolly_bot.db_get_room', return_value=mock_room_pre), \
         patch('dolly_bot.db_get_match', return_value=mock_match_pre), \
         patch('dolly_bot.db_get_match_research', return_value={}), \
         patch('dolly_bot.publish_questions') as mock_publish, \
         patch('dolly_bot.client.models.generate_content', return_value=mock_response_pre):
        
        run_dolly_for_sport("cricket", room_id="mock-room-1")
        
        print("✅ Pre-Match Generation Triggered:")
        published_polls = mock_publish.call_args[0][1]
        for poll in published_polls:
            print(f"   [{poll.get('type')}] Title: \"{poll.get('title')}\" - Text: \"{poll.get('text')}\"")

    # ── TEST 2: POST-MATCH ──
    print("\n⏳ Testing POST-MATCH Phase...")
    mock_room_post = {"isTestingRoom": True, "matchId": "mock-match-post"}
    mock_match_post = {
        "team_a": "Spain",
        "team_b": "France",
        "kickoff_time": int((time.time() - 7200) * 1000), # 2 hrs ago
        "status": "completed"
    }

    mock_response_post = MagicMock()
    mock_response_post.text = json.dumps([
        {
            "type": "analysis",
            "title": "Post-Match Read",
            "text": "France's defense held strong, knocking out Spain 1-0."
        },
        {
            "type": "story",
            "title": "Story: End of an Era",
            "text": "Spain goes home early again. Is it time for a new generation?"
        }
    ])
    
    with patch('dolly_bot.db_get_room', return_value=mock_room_post), \
         patch('dolly_bot.db_get_match', return_value=mock_match_post), \
         patch('dolly_bot.db_get_match_research', return_value={}), \
         patch('dolly_bot.publish_questions') as mock_publish, \
         patch('dolly_bot.client.models.generate_content', return_value=mock_response_post):
        
        run_dolly_for_sport("football", room_id="mock-room-2")
        
        print("✅ Post-Match Generation Triggered:")
        published_polls = mock_publish.call_args[0][1]
        for poll in published_polls:
            print(f"   [{poll.get('type')}] Title: \"{poll.get('title')}\" - Text: \"{poll.get('text')}\"")

    print("\n🎉 SUCCESS! Dolly Pre/Post Match logic verified locally without hitting GCP/Firebase!")

if __name__ == "__main__":
    test_mock_run()
