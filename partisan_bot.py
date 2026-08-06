import json
import os
import time
from datetime import datetime
import pytz
from google import genai
from google.genai import types
from firebase_store import init_firebase
from google.cloud.firestore_v1.transforms import Increment
from db_helpers import (
    db_check_partisan_lock,
    db_stamp_partisan_lock,
    db_get_room,
    db_get_match,
    db_save_room_message,
    db_save_bot_post,
    db_get_matches_by_status
)
from dolly_bot import get_upcoming_real_match

# ── API Initialization ────────────────────────────────────────────────────────
_gemini_client = None

def get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if api_key:
            _gemini_client = genai.Client(api_key=api_key)
        else:
            gcp_project = os.getenv("GCP_PROJECT_ID")
            if not gcp_project:
                raise ValueError("GCP_PROJECT_ID is not set")
            _gemini_client = genai.Client(
                vertexai=True,
                project=gcp_project,
                location=os.getenv("GCP_LOCATION", "us-central1")
            )
    return _gemini_client

IST = pytz.timezone('Asia/Kolkata')

# ── Config ────────────────────────────────────────────────────────────────────
COOLDOWN_MINUTES = 10  # Partisan bots can post every 10 mins

# ── Helpers ───────────────────────────────────────────────────────────────────

def has_posted_recently(db, sport: str, match_id: str, room_id: str, bot_uid: str) -> bool:
    return db_check_partisan_lock(sport, match_id, room_id, bot_uid)

def stamp_partisan_lock(db, sport: str, match_id: str, room_id: str, bot_uid: str):
    db_stamp_partisan_lock(sport, match_id, room_id, bot_uid)

def get_bot_profile(db, bot_uid: str):
    # Fallbacks based on mockup
    if "krishna" in bot_uid.lower(): return "Krishna"
    if "radha" in bot_uid.lower(): return "Radha"
    return bot_uid


# ── Core Runner ───────────────────────────────────────────────────────────────

def run_partisan_bot(bot_uid: str, team: str, sport: str, room_id: str):
    """
    Executes a partisan bot (e.g. Krishna/Radha).
    It acts as a deeply emotional, biased fan of `team`.
    """
    db = init_firebase()
    bot_username = get_bot_profile(db, bot_uid)
    print(f"\n🔥 Partisan Bot [{bot_username}] ({bot_uid}) running for {team} in room [{room_id}]")

    # 1. Resolve Match
    match_id = None
    match_data = None

    is_testing_room = False
    if room_id:
        room_info = db_get_room(room_id)
        if room_info:
            is_testing_room = room_info.get("isTestingRoom", False)
            match_id = room_info.get("matchId")
            if match_id:
                match_data = db_get_match(match_id)
            elif is_testing_room:
                # Standalone testing match context
                ta = room_info.get("simulatedTeamA")
                tb = room_info.get("simulatedTeamB")
                if not ta or not tb:
                    # Default simulated teams if not defined
                    ta, tb = "India", "Pakistan"
                
                match_id = "test-match"
                match_data = {
                    "status": "live",
                    "team_a": ta,
                    "team_b": tb,
                    "sport": sport
                }
                print(f"🛠️ Standalone Testing Room detected. Simulated match context enabled for {bot_username}: {ta} vs {tb}.")

    if not match_data:
        # Fallback to live match if no room context
        live_matches = db_get_matches_by_status(sport, "live")
        if live_matches:
            match_id = live_matches[0]["id"]
            match_data = live_matches[0]
            
    if not match_data:
        print(f"⏭️ No active match found for {bot_username}. Skipping.")
        return

    # Check match status gating (live vs concluded)
    status = match_data.get("status")
    if status == "completed":
        updated_at_val = match_data.get("updated_at") or match_data.get("updatedAt")
        if hasattr(updated_at_val, "timestamp"):
            updated_at_ms = int(updated_at_val.timestamp() * 1000)
        else:
            updated_at_ms = int(updated_at_val or 0)
            
        now_ms = int(time.time() * 1000)
        if now_ms - updated_at_ms > 2700000:
            print(f"🔒 Match [{match_id}] concluded > 45 mins ago. Skipping {bot_username}.")
            return
    elif status != "live" and not is_testing_room:
        print(f"⏭️ Match [{match_id}] is {status}. Skipping {bot_username}.")
        return

    # 2. Check Cooldown
    if has_posted_recently(db, sport, match_id, room_id, bot_uid):
        print(f"⏳ Cooldown active for {bot_username}. Skipping.")
        return

    # 3. Fetch Live Score Context
    live_context = ""
    try:
        now_ist = datetime.now(IST).strftime("%I:%M %p IST")
        search_query = f"{match_data.get('team_a')} vs {match_data.get('team_b')} {sport} live scorecard ball by ball score today {now_ist}"
        response = get_gemini_client().models.generate_content(
            model="gemini-2.5-flash",
            contents=search_query,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.1
            )
        )
        live_context = response.text.strip()
    except Exception as e:
        print(f"⚠️ Live score search failed for {bot_username}: {e}")

    # 4. Generate Emotionally Biased Chat Message
    prompt = f"""
    You are {bot_username}, an extremely passionate, biased, and emotional fan of {team} in {sport}.
    The match is: {match_data.get('team_a')} vs {match_data.get('team_b')}.
    
    Current Live Score Context (from Google Search):
    {live_context}
    
    YOUR PERSONA INSTRUCTIONS:
    - You MUST act like a highly invested fan of {team} watching the game live.
    - If {team} is doing well (taking wickets, scoring fast, winning), be arrogant, excited, and brag. Use emojis like 🔥 or 🎉.
    - If {team} is losing or doing poorly, cope, complain about the umpire, blame luck, or demand a player be dropped. Use emojis like 😭, 💀, or 😩.
    - Keep your message short and punchy. Maximum 1 or 2 sentences.
    - Write it exactly like a text message in a WhatsApp group. Lowercase is fine. Do NOT use hashtags.
    - Mention a specific player or event from the live score context if possible to prove you are watching right now.
    
    Return ONLY a valid JSON object:
    {{
        "text": "your emotional message here"
    }}
    """

    try:
        response = get_gemini_client().models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.7)
        )
        raw = response.text.strip()
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end == 0:
            print(f"⚠️ Gemini returned no JSON for {bot_username}.")
            return
            
        payload = json.loads(raw[start:end])
        text = payload.get("text", "").strip()
        
        if text:
            # 5. Publish
            extra_payload = {
                "authorBadge": "SUPER_FAN",
                "isBot": True,
                "botRole": "partisan",
                "botTeam": team
            }
            if room_id:
                db_save_room_message(room_id, text, bot_uid, bot_username, sport, "chat", extra_payload=extra_payload)
            else:
                db_save_bot_post(text, bot_uid, bot_username, sport, "chat", extra_payload=extra_payload)
                
            stamp_partisan_lock(db, sport, match_id, room_id, bot_uid)
            
    except Exception as e:
        print(f"❌ Partisan generation failed for {bot_username}: {e}")
