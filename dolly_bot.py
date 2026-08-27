import os
import json
import time
from datetime import datetime, timezone, timedelta
from google import genai
from google.genai import types
from firebase_store import init_firebase
from google.cloud.firestore_v1 import Increment

# ── Gemini Client ─────────────────────────────────────────────────────────────
api_key = os.getenv("GEMINI_API_KEY")
if api_key:
    client = genai.Client(api_key=api_key)
    print("🔑 Using Google AI Studio API Key for Gemini Client.")
else:
    gcp_project = os.getenv("GCP_PROJECT_ID")
    if not gcp_project:
        raise ValueError("GCP_PROJECT_ID is not set")
    client = genai.Client(
        vertexai=True,
        project=gcp_project,
        location=os.getenv("GCP_LOCATION", "us-central1")
    )
    print("☁️ Using Vertex AI for Gemini Client.")

IST = timezone(timedelta(hours=5, minutes=30))
COOLDOWN_MINUTES = 15  # Minimum gap between posts in the same room/feed

ROOM_COUNT_FIELD_BY_TYPE = {
    "post": "postCount",
    "chat": "postCount",
    "debate": "debateCount",
    "prediction": "predictionCount",
    "trivia": "triviaCount",
    "battle": "battleCount",
}

# ── DynamoDB/Firebase wrappers ───────────────────────────────────────────────
from db_helpers import (
    db_check_phase_lock,
    db_stamp_phase_lock,
    db_was_recently_posted,
    db_get_existing_questions,
    db_get_rooms,
    db_get_room,
    db_get_match,
    db_update_match_status,
    db_get_match_research,
    db_save_room_message,
    db_save_bot_post,
    db_get_matches_by_status
)

# ── Phase Lock Helpers ────────────────────────────────────────────────────────

def get_phase_lock_key(sport: str, match_id: str, phase: str, room_id: str = None) -> str:
    room_suffix = f"_{room_id}" if room_id else "_global"
    return f"dolly_phase_lock_{sport}_{match_id}_{phase}{room_suffix}"

def has_phase_been_posted(db, sport: str, match_id: str, phase: str, room_id: str = None) -> bool:
    if phase == "IN-PLAY":
        return False # No phase locks for in-play
    return db_check_phase_lock(sport, match_id, phase, room_id)

def stamp_phase_lock(db, sport: str, match_id: str, phase: str, room_id: str = None):
    db_stamp_phase_lock(sport, match_id, phase, room_id)

def was_recently_posted(db, room_id=None, sport="cricket", cooldown_minutes=COOLDOWN_MINUTES, bot_uid="dolly-dolphin-bot") -> bool:
    return db_was_recently_posted(room_id, sport, cooldown_minutes, bot_uid)

def get_existing_questions(db, room_id=None, sport="cricket", bot_uid="dolly-dolphin-bot"):
    return db_get_existing_questions(room_id, sport, bot_uid)

# ── Question Generator ────────────────────────────────────────────────────────

def generate_questions(match: dict, sport: str, existing_str: str, pre_match_count: int = 0) -> list:
    # Not used in analysis model, preserved for compatibility
    return []

# ── Post to Firestore ─────────────────────────────────────────────────────────

def publish_questions(db, polls: list, sport: str, room_id=None, bot_uid="dolly-dolphin-bot", bot_username="Dolly"):
    """Writes generated questions to DynamoDB and Firebase (room or global feed)."""
    for poll in polls:
        text = poll.get("text", "").strip()
        if not text:
            continue
        type_val = poll.get("type", "prediction")
        polls_data = [poll]
        extra_payload = {"title": poll.get("title")} if poll.get("title") else None
        
        if room_id:
            db_save_room_message(room_id, text, bot_uid, bot_username, sport, type_val, polls_data, extra_payload=extra_payload)
        else:
            db_save_bot_post(text, bot_uid, bot_username, sport, type_val, polls_data, extra_payload=extra_payload)

# ── Core Runner ───────────────────────────────────────────────────────────────

def run_dolly_for_sport(sport: str, room_id=None, bot_uid="dolly-dolphin-bot", bot_username="Dolly", supported_team=None):
    """
    Full pipeline for one sport.
    """
    db = init_firebase()
    target = f"Room [{room_id}]" if room_id else "Global Feed"
    print(f"\n🐬 Dolly running for sport={sport}, target={target}")

    match_id = None
    match_data = None

    # Step 1: Linked Match Resolution
    is_testing_room = False
    if room_id:
        room_data = db_get_room(room_id)
        if room_data:
            is_testing_room = room_data.get("isTestingRoom", False)
            match_id = room_data.get("matchId")
            if match_id:
                match_data = db_get_match(match_id)
                if match_data:
                    print(f"🔗 Bound to focus match: {match_data.get('team_a')} vs {match_data.get('team_b')} via room matchId [{match_id}]")

    # Step 2: Fallback to detect live or upcoming match from matches table
    if not match_data:
        live_matches = db_get_matches_by_status(sport, "live")
        if live_matches:
            match_id = live_matches[0]["id"]
            match_data = live_matches[0]
            print(f"🎯 Detected active live match from table: {match_data.get('team_a')} vs {match_data.get('team_b')} [{match_id}]")

    # If no live match, check for upcoming matches that should be live now
    if not match_data:
        now_ms = int(time.time() * 1000)
        upcoming_matches = db_get_matches_by_status(sport, "upcoming")
        for m_data in upcoming_matches:
            kickoff = m_data.get("kickoff_time", 0)
            if kickoff > 0 and now_ms >= (kickoff - 5 * 60 * 1000):
                match_id = m_data["id"]
                match_data = m_data
                db_update_match_status(match_id, "live")
                match_data["status"] = "live"
                print(f"⏰ Kickoff time reached! Auto-transitioned match [{match_id}] to LIVE: {match_data.get('team_a')} vs {match_data.get('team_b')}")
                break

    if room_id and not match_data:
        room_data = db_get_room(room_id)
        if room_data:
            match_id = room_data.get("matchId")
            if match_id:
                m_data = db_get_match(match_id)
                if m_data and m_data.get("status") == "upcoming":
                    kickoff = m_data.get("kickoff_time", 0)
                    now_ms = int(time.time() * 1000)
                    if kickoff > 0 and now_ms >= (kickoff - 5 * 60 * 1000):
                        match_data = m_data
                        db_update_match_status(match_id, "live")
                        match_data["status"] = "live"
                        print(f"⏰ Linked room kickoff reached! Auto-transitioned match [{match_id}] to LIVE: {match_data.get('team_a')} vs {match_data.get('team_b')}")

    if not match_data:
        print(f"⏭️ No active live match scheduled in database for {sport}. Dolly will stay silent.")
        return

    status = match_data.get("status", "live")
    kickoff = match_data.get("kickoff_time", 0)
    now_ms = int(time.time() * 1000)

    if status == "upcoming" and kickoff > 0 and now_ms >= (kickoff - 5 * 60 * 1000):
        db_update_match_status(match_id, "live")
        match_data["status"] = "live"
        status = "live"
        print(f"⏰ Kickoff reached for linked match [{match_id}]! Auto-transitioned to LIVE.")

    if status == "upcoming":
        phase = "PRE-MATCH"
    elif status == "completed":
        phase = "POST-MATCH"
    else:
        phase = "IN-PLAY"

    teams = f"{match_data.get('team_a')} vs {match_data.get('team_b')}"

    # Step 3: Phase lock check
    if has_phase_been_posted(db, sport, match_id, phase, room_id):
        print(f"🔒 Phase lock active: Already posted [{phase}] for match [{match_id}] in target [{target}]. Skipping.")
        return

    # Step 4: Cooldown check
    if was_recently_posted(db, room_id=room_id, sport=sport):
        print(f"⏳ Cooldown active: A post was made less than {COOLDOWN_MINUTES} mins ago in target [{target}]. Skipping.")
        return

    # Step 5: Fetch 4-Pillar Data
    rivalries = []
    stats = []
    history = []
    form = {}

    try:
        research_data = db_get_match_research(match_id)
        rivalries = research_data.get("rivalries", [])
        stats = research_data.get("stats", [])
        history = research_data.get("matchup_history", [])
        form = research_data.get("tournament_form", {})
    except Exception as e:
        print(f"⚠️ Failed to load 4-pillar data: {e}. Fallback to live score questions.")

    # Create search instructions incorporating live scores
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
        print(f"⚠️ Live score search failed: {e}")

    # Build Prompt Persona
    persona = f"You are {bot_username}, a passionate and highly knowledgeable sports analyst."
    if supported_team:
        persona += f" You are a deeply partisan fan of {supported_team}, and your analysis and questions are heavily biased towards {supported_team}."

    if phase == "PRE-MATCH":
        phase_instruction = f"""Generate exactly 1 "analysis" (Pre-Match Read - key team news, rankings context, toss significance) and 1 "story" (Story Arc - a player narrative, historical rivalry, or storyline going into the match) for the upcoming match: {teams}."""
        json_example = """[
      { "type": "analysis", "title": "Pre-Match Read", "text": "Short punchy paragraph with 2-3 key insights about the upcoming match." },
      { "type": "story", "title": "Story: <Name of Storyline>", "text": "Short punchy paragraph about a specific rivalry or player narrative." }
    ]"""
    elif phase == "POST-MATCH":
        phase_instruction = f"""Generate exactly 1 "analysis" (Post-Match Read - final scorecard context, match-defining moment, key duels) and 1 "story" (Story Arc - what this result means for the series, a player's legacy, or narrative conclusion) for the completed match: {teams}."""
        json_example = """[
      { "type": "analysis", "title": "Post-Match Read", "text": "Short punchy paragraph summarizing the key turning points and final outcome." },
      { "type": "story", "title": "Story: <Name of Storyline>", "text": "Short punchy paragraph wrapping up a player's performance arc or series impact." }
    ]"""
    else:
        phase_instruction = f"""Generate exactly 1 prediction, 1 debate, and 1 story (Story Arc) for the live match: {teams}."""
        json_example = """[
      { "type": "prediction", "text": "Short question?", "sideA": "Option A", "sideB": "Option B" },
      { "type": "debate", "text": "Short question?", "sideA": "Option A", "sideB": "Option B" },
      { "type": "story", "title": "Story: <Name of Storyline>", "text": "Short punchy paragraph about a specific rivalry or player narrative." }
    ]"""

    # Build Prompt
    prompt = f"""
    {persona}
    {phase_instruction}
    
    Live Score Context (from live Google Search):
    {live_context}
    
    Rivalries Data:
    {json.dumps(rivalries)}
    
    Historical Stats Data:
    {json.dumps(stats)}
    
    Matchup History:
    {json.dumps(history)}
    
    Tournament Form:
    {json.dumps(form)}
    
    YOUR QUESTION/CONTENT STYLE:
    - Short and punchy. 
    - Confident, direct, articulate, and opinionated — like a professional analyst.
    - NO generic, lazy, or simple "who wins" or "is team X good" questions.
    - MUST specifically reference at least one active player by name to make the content highly specific and non-generic.
    - If IN-PLAY, MUST ask about the CURRENT live status (e.g. current score, wickets, which batsmen are currently at the crease, or who is currently bowling).
    - No Gen-Z slang, no emojis, no exclamation marks.
    - Options (sideA, sideB) must be 1 to 4 words only (if applicable).
    - Frame your content using your boss's Gen Z Sports Fan Discussion Category Matrix:
      
      DISCUSSION CATEGORIES TO WEAVE IN (BASED ON PHASES):
      1. PERFORMANCE (Tactical debates, athlete performance arcs, rivalries, stats & data analytics).
         * During Match: Focus on tactical decisions ("Who should bowl next?", "X hasn't touched the ball in 20 mins", "expected goals curse").
         * Between Matches / Post-Match: Focus on strategic decisions ("Wrong XI selected?", "Did the coach have any tactical idea?", "Slump or blip?").
      2. EMOTION & CULTURE (Memes & humor, controversies, red cards, VAR/DRS robbing, nostalgia, match build-up).
         * During Match: Focus on controversial moments ("VAR/DRS robbery", "He dived — clear card", "Stadium energy/WC atmosphere").
         * Between/Post-Match: Focus on post-match reactions ("Coach press-conference unfiltered drama", "Line-up reaction threads").
      3. ATHLETE AS PERSON (Personal life/identity, mental health/wellbeing, airport fit, fashion & lifestyle).
         * Examples: Focus on mental fatigue and pressure ("Visible player distress / community worry", "Form linked to fatigue / coach rotation", "collab drops").
      4. FAN PARTICIPATION (Fantasy sports, creator edit clips, transfer speculation, mini-league standings).
         * Examples: Focus on fan investment and valuation ("0 points gameweek disaster", "transfer value kill", "target linked to team").

    CRITICAL QUALITY CHECK:
    - You must select one specific category from the matrix above (e.g., DRS controversy, performance slumps, mental pressure, or transfer value) and write your prediction/debate/story about it.
    - DO NOT write generic questions like "Who will win?" or "Will team X score Y runs?". Focus on the narrative, the player's character, or the tactical friction.

    Return ONLY a valid JSON list of objects:
    {json_example}
    """

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.3
            )
        )
        raw = response.text.strip()
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start == -1 or end == 0:
            print("⚠️ Gemini returned no JSON for question generation.")
            return
        polls = json.loads(raw[start:end])
        


        # Step 6: Publish
        publish_questions(db, polls, sport, room_id=room_id, bot_uid=bot_uid, bot_username=bot_username)
        stamp_phase_lock(db, sport, match_id, phase, room_id)
        print(f"✅ Dolly done for sport={sport}, phase={phase}, target={target}")
    except Exception as e:
        print(f"❌ Question generation failure: {e}")


# ── Automated Full Run ────────────────────────────────────────────────────────

def find_infinity_room_id(db) -> str | None:
    """
    Dynamically finds the SF360 Infinity Room.
    """
    try:
        rooms = db_get_rooms()
        for room in rooms:
            name = (room.get("name") or "").lower()
            if "infinity" in name:
                room_id = room.get("id") or room.get("roomId", "").replace("ROOM#", "")
                print(f"🌐 Infinity Room found: {room_id} ('{room.get('name')}')")
                return room_id
    except Exception as e:
        print(f"⚠️ Could not find Infinity Room: {e}")
    return None


def run_bots_for_room(room, sport_val: str):
    room_id = room.get("id") or room.get("roomId", "").replace("ROOM#", "")
    bot_config = room.get("botConfig", {})
    if not isinstance(bot_config, dict) or not bot_config:
        run_dolly_for_sport(sport_val, room_id=room_id, bot_uid="dolly-dolphin-bot", bot_username="Dolly")
        return

    has_active_bot = False
    for bot_id, cfg in bot_config.items():
        if cfg:
            bot_username = "Dolly"
            if "krishna" in bot_id:
                bot_username = "Krishna"
            elif "radha" in bot_id:
                bot_username = "Radha"
            
            supported_team = None
            if isinstance(cfg, dict):
                supported_team = cfg.get("team")
                
            run_dolly_for_sport(
                sport_val, 
                room_id=room_id, 
                bot_uid=bot_id, 
                bot_username=bot_username, 
                supported_team=supported_team
            )
            has_active_bot = True

    if not has_active_bot:
        run_dolly_for_sport(sport_val, room_id=room_id, bot_uid="dolly-dolphin-bot", bot_username="Dolly")


def dolly_auto_run_all_rooms():
    """
    Master runner.
    """
    db = init_firebase()
    posted_room_ids = set()

    # ── Step 1: SF360 Infinity Room ──
    print("\n🌐 ── Dolly: SF360 Infinity Room ──")
    infinity_room_id = find_infinity_room_id(db)
    if infinity_room_id:
        run_dolly_for_sport("cricket", room_id=infinity_room_id)
        run_dolly_for_sport("football", room_id=infinity_room_id)
        posted_room_ids.add(infinity_room_id)
    else:
        print("⚠️ Infinity Room not found. Skipping.")

    # ── Step 2: Global feed ──
    print("\n🌍 ── Dolly: Global Feed ──")
    run_dolly_for_sport("cricket", room_id=None)
    run_dolly_for_sport("football", room_id=None)

    # ── Step 3: All cricket rooms ──
    print("\n🏏 ── Dolly: Cricket Rooms ──")
    all_rooms = db_get_rooms()
    for room in all_rooms:
        room_id = room.get("id") or room.get("roomId", "").replace("ROOM#", "")
        sport_val = room.get("sport", "")
        if sport_val == "cricket" and room_id not in posted_room_ids:
            run_bots_for_room(room, "cricket")
            posted_room_ids.add(room_id)

    # ── Step 4: All football rooms ──
    print("\n⚽ ── Dolly: Football Rooms ──")
    for room in all_rooms:
        room_id = room.get("id") or room.get("roomId", "").replace("ROOM#", "")
        sport_val = room.get("sport", "")
        if sport_val == "football" and room_id not in posted_room_ids:
            run_bots_for_room(room, "football")
            posted_room_ids.add(room_id)

    print("\n🐬 Dolly full run complete.")


def dolly_auto_run_all_cricket_rooms():
    dolly_auto_run_all_rooms()


if __name__ == "__main__":
    dolly_auto_run_all_rooms()
