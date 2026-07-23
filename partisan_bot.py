import json
import os
import time
from datetime import datetime
import pytz
from google import genai
from google.genai import types
from firebase_store import init_firebase
from google.cloud.firestore_v1.transforms import Increment

# ── API Initialization ────────────────────────────────────────────────────────
# Uses the SAME pattern as dolly_bot.py — env var first, then Vertex AI fallback
api_key = os.getenv("GEMINI_API_KEY")
if api_key:
    client = genai.Client(api_key=api_key)
else:
    gcp_project = os.getenv("GCP_PROJECT_ID")
    if not gcp_project:
        raise ValueError("GCP_PROJECT_ID is not set")
    client = genai.Client(
        vertexai=True,
        project=gcp_project,
        location=os.getenv("GCP_LOCATION", "us-central1")
    )

IST = pytz.timezone('Asia/Kolkata')

# ── Config ────────────────────────────────────────────────────────────────────
COOLDOWN_MINUTES = 10  # Partisan bots can post every 10 mins

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_partisan_lock_key(sport: str, match_id: str, room_id: str, bot_uid: str) -> str:
    room_suffix = f"_{room_id}" if room_id else "_global"
    return f"partisan_lock_{sport}_{match_id}{room_suffix}_{bot_uid}"

def has_posted_recently(db, sport: str, match_id: str, room_id: str, bot_uid: str) -> bool:
    """Check if this specific bot posted recently in this room based on the timestamp lock."""
    key = get_partisan_lock_key(sport, match_id, room_id, bot_uid)
    doc = db.collection("partisanLocks").document(key).get()
    if not doc.exists:
        return False
    posted_at = doc.to_dict().get("postedAt", 0)
    elapsed_minutes = (time.time() * 1000 - posted_at) / (1000 * 60)
    return elapsed_minutes < COOLDOWN_MINUTES

def stamp_partisan_lock(db, sport: str, match_id: str, room_id: str, bot_uid: str):
    key = get_partisan_lock_key(sport, match_id, room_id, bot_uid)
    db.collection("partisanLocks").document(key).set({
        "sport": sport,
        "matchId": match_id,
        "roomId": room_id or "global",
        "botUid": bot_uid,
        "postedAt": int(time.time() * 1000),
    })

def get_bot_profile(db, bot_uid: str):
    """Fetch the bot's display name from the users collection."""
    try:
        doc = db.collection("users").document(bot_uid).get()
        if doc.exists:
            return doc.to_dict().get("username", bot_uid)
    except Exception as e:
        print(f"⚠️ Failed to load bot profile for {bot_uid}: {e}")
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

    if room_id:
        room_doc = db.collection("roarRooms").document(room_id).get()
        if not room_doc.exists:
            # This room_id may belong to a linked watchalong room — check there too
            room_doc = db.collection("watchAlongRooms").document(room_id).get()
        if room_doc.exists:
            match_id = room_doc.to_dict().get("matchId")
            if match_id:
                match_doc = db.collection("matches").document(match_id).get()
                if match_doc.exists:
                    match_data = match_doc.to_dict()

    if not match_data:
        # Fallback to live match if no room context
        matches_ref = db.collection("matches").where("sport", "==", sport).where("status", "==", "live").stream()
        for doc in matches_ref:
            match_id = doc.id
            match_data = doc.to_dict()
            break
            
    if not match_data or match_data.get("status") != "live":
        print(f"⏭️ No live match found for {bot_username}. Partisans only post during live action. Skipping.")
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
        response = client.models.generate_content(
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
        response = client.models.generate_content(
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
            # 5. Publish to Firestore
            now_ms = int(time.time() * 1000)
            if room_id:
                msg_ref = db.collection("roarRooms").document(room_id).collection("messages").document()
                room_ref = db.collection("roarRooms").document(room_id)
                
                batch = db.batch()
                batch.set(msg_ref, {
                    "msgId": msg_ref.id,
                    "roomId": room_id,
                    "authorUid": bot_uid,
                    "authorUsername": bot_username,
                    "authorBadge": "SUPER_FAN",
                    "type": "chat",          # Partisans just chat, no polls
                    "text": text,
                    "fireCount": 0,
                    "noChanceCount": 0,
                    "heartCount": 0,
                    "replyCount": 0,
                    "sport": sport,
                    "isBot": True,
                    "botRole": "partisan",
                    "botTeam": team,         # Binds the color coding in UI
                    "createdAt": now_ms,
                    "updatedAt": now_ms,
                })
                batch.update(room_ref, {"fanCount": Increment(1)})
                batch.commit()
                print(f"✅ {bot_username} posted in room [{room_id}]: {text}")
            else:
                post_ref = db.collection("roarPosts").document()
                post_ref.set({
                    "postId": post_ref.id,
                    "authorUid": bot_uid,
                    "authorUsername": bot_username,
                    "authorBadge": "SUPER_FAN",
                    "type": "chat",
                    "sport": sport,
                    "text": text,
                    "agreeCount": 0,
                    "disagreeCount": 0,
                    "replyCount": 0,
                    "likeCount": 0,
                    "isLive": True,
                    "status": "active",
                    "audience": "Everyone",
                    "isBot": True,
                    "botRole": "partisan",
                    "botTeam": team,
                    "createdAt": now_ms,
                    "updatedAt": now_ms
                })
                print(f"✅ {bot_username} posted to Global Feed: {text}")
                
            stamp_partisan_lock(db, sport, match_id, room_id, bot_uid)
            
    except Exception as e:
        print(f"❌ Partisan generation failed for {bot_username}: {e}")
