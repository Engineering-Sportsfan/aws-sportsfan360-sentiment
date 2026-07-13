import os
import json
import time
from datetime import datetime, timezone, timedelta
from google import genai
from google.genai import types
from firebase_store import init_firebase
from google.cloud.firestore_v1 import Increment

# ── Gemini Client ─────────────────────────────────────────────────────────────
# Uses GEMINI_API_KEY if available (AI Studio) to avoid authentication issues.
# Falls back to Vertex AI if key is not set.
api_key = os.getenv("GEMINI_API_KEY")
if api_key:
    client = genai.Client(api_key=api_key)
    print("🔑 Using Google AI Studio API Key for Gemini Client.")
else:
    client = genai.Client(
        vertexai=True,
        project=os.getenv("GCP_PROJECT_ID", "fleet-gift-498306-p7"),
        location=os.getenv("GCP_LOCATION", "us-central1")
    )
    print("☁️ Using Vertex AI for Gemini Client.")

IST = timezone(timedelta(hours=5, minutes=30))
COOLDOWN_MINUTES = 15  # Minimum gap between posts in the same room/feed (matches 15-min schedule)

ROOM_COUNT_FIELD_BY_TYPE = {
    "post": "postCount",
    "chat": "postCount",
    "debate": "debateCount",
    "prediction": "predictionCount",
    "trivia": "triviaCount",
    "battle": "battleCount",
}

# ── Phase Lock Helpers ────────────────────────────────────────────────────────

def get_phase_lock_key(sport: str, match_id: str, phase: str, room_id: str = None) -> str:
    room_suffix = f"_{room_id}" if room_id else "_global"
    return f"dolly_phase_lock_{sport}_{match_id}_{phase}{room_suffix}"

PRE_MATCH_MAX_POSTS = 2   # 2 rounds of pre-match questions (~50 mins build-up)
POST_MATCH_MAX_POSTS = 2  # 2 rounds of post-match questions (~50 mins wrap-up)

def has_phase_been_posted(db, sport: str, match_id: str, phase: str, room_id: str = None) -> bool:
    """
    Returns True if Dolly should be blocked from posting in this specific room.
    - PRE-MATCH: Disabled completely (always returns True).
    - IN-PLAY: Spaced out every 20 minutes using the timestamp check.
    - POST-MATCH: Locked to max 1 post.
    """
    if phase == "PRE-MATCH":
        return True  # Stop/skip pre-match posting completely
    if phase == "IN-PLAY":
        return False # No phase locks for in-play (cooldown handles spacing)
        
    key = get_phase_lock_key(sport, match_id, phase, room_id)
    doc = db.collection("dollyPhaseLocks").document(key).get()
    if not doc.exists:
        return False
    data = doc.to_dict()
    posted_at = data.get("postedAt", 0)
    post_count = data.get("count", 1)
    
    elapsed_minutes = (time.time() * 1000 - posted_at) / (1000 * 60)
    
    if phase == "POST-MATCH":
        # Block permanently (max 1 post) for post-match
        return post_count >= 1
    return False

def stamp_phase_lock(db, sport: str, match_id: str, phase: str, room_id: str = None):
    """Stamps/increments this phase's post count in Firestore for this specific room."""
    key = get_phase_lock_key(sport, match_id, phase, room_id)
    doc = db.collection("dollyPhaseLocks").document(key).get()
    existing_count = doc.to_dict().get("count", 0) if doc.exists else 0
    db.collection("dollyPhaseLocks").document(key).set({
        "sport": sport,
        "matchId": match_id,
        "phase": phase,
        "roomId": room_id or "global",
        "postedAt": int(time.time() * 1000),
        "count": existing_count + 1,
    })

def was_recently_posted(db, room_id=None, sport="cricket", cooldown_minutes=COOLDOWN_MINUTES) -> bool:
    """Returns True if Dolly posted in this feed/room for this specific sport within the cooldown window."""
    cutoff_ms = int((time.time() - cooldown_minutes * 60) * 1000)
    try:
        if room_id:
            # Check only messages of the target sport to allow dual-sport rooms
            msgs = db.collection("roarRooms").document(room_id).collection("messages") \
                .where("authorUid", "==", "dolly-dolphin-bot") \
                .where("sport", "==", sport).stream()
            for msg in msgs:
                if msg.to_dict().get("createdAt", 0) > cutoff_ms:
                    return True
        else:
            posts = db.collection("roarPosts") \
                .where("authorUid", "==", "dolly-dolphin-bot") \
                .where("sport", "==", sport).stream()
            for post in posts:
                if post.to_dict().get("createdAt", 0) > cutoff_ms:
                    return True
    except Exception as e:
        print(f"⚠️ Cooldown check error: {e}")
    return False

def get_existing_questions(db, room_id=None, sport="cricket"):
    """Fetches last 30 question texts in this specific room or feed to prevent duplicate posts."""
    questions = []
    try:
        if room_id:
            room_ref = db.collection("roarRooms").document(room_id).collection("messages") \
                .where("authorUid", "==", "dolly-dolphin-bot").stream()
            room_posts = sorted([d for d in room_ref],
                                 key=lambda x: x.to_dict().get("createdAt", 0), reverse=True)
            for doc in room_posts[:30]:
                text = doc.to_dict().get("text")
                if text:
                    questions.append(text)
        else:
            global_ref = db.collection("roarPosts").where("authorUid", "==", "dolly-dolphin-bot") \
                .where("sport", "==", sport).stream()
            global_posts = sorted([d for d in global_ref],
                                   key=lambda x: x.to_dict().get("createdAt", 0), reverse=True)
            for doc in global_posts[:30]:
                text = doc.to_dict().get("text")
                if text:
                    questions.append(text)
    except Exception as e:
        print(f"⚠️ Error fetching existing questions: {e}")
    return list(set(questions))

# ── Dynamic Match Detection via Gemini ───────────────────────────────────────

def detect_current_match(sport: str) -> dict | None:
    """
    Uses Gemini + Google Search to dynamically find the current or next upcoming match.
    Returns a dict with match details, or None if no credible match found.
    """
    now_ist = datetime.now(IST).strftime("%A, %d %B %Y %I:%M %p IST")

    if sport == "cricket":
        search_query = f"""
        Search Google right now for: "India vs England 1st T20I match live score OR match status today {now_ist}".
        Find the match details for India vs England Men's 1st T20I starting tonight at 10:00 PM IST (Chester-le-Street).
        
        If you find the match, return ONLY this JSON:
        {{
          "found": true,
          "matchId": "ind-vs-eng-t20-jul1",
          "teams": "India vs England",
          "tournament": "India tour of England 2026, 1st T20I",
          "venue": "Chester-le-Street",
          "matchDate": "2026-07-01",
          "phase": "PRE-MATCH or IN-PLAY or POST-MATCH",
          "liveScore": "Score if live, else null",
          "keyPlayers": "Harry Brook, Jacob Bethell, Ishan Kishan, Suryakumar Yadav",
          "format": "T20"
        }}
        
        If the match is not found or not today, fallback to search for any other live international cricket match today and return the same JSON format. If no match at all, return:
        {{"found": false}}
        """
    else:  # football / FIFA
        search_query = f"""
        Search Google right now for: "England vs DR Congo FIFA World Cup 2026 match live score OR status today {now_ist}".
        Find the match details for England vs DR Congo starting tonight at 9:30 PM IST (Atlanta).
        
        If you find the match, return ONLY this JSON:
        {{
          "found": true,
          "matchId": "eng-vs-drcongo-fifa-jul1",
          "teams": "England vs DR Congo",
          "tournament": "FIFA World Cup 2026, Round of 32",
          "venue": "Atlanta",
          "matchDate": "2026-07-01",
          "phase": "PRE-MATCH or IN-PLAY or POST-MATCH",
          "liveScore": "Score if live, else null",
          "keyPlayers": "Jude Bellingham, Harry Kane, Yoane Wissa, Meschack Elia",
          "format": "90 mins"
        }}
        
        If the match is not found or not today, fallback to search for any other live FIFA World Cup 2026 match today and return the same JSON format. If no match at all, return:
        {{"found": false}}
        """

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=search_query,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.1
            )
        )
        raw = response.text.strip()
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end == 0:
            print(f"⚠️ Gemini returned no JSON for match detection ({sport}).")
            return None
        data = json.loads(raw[start:end])
        if not data.get("found"):
            print(f"ℹ️ No active or upcoming {sport} match found by Gemini.")
            return None
        print(f"✅ Match detected: {data.get('teams')} — Phase: {data.get('phase')}")
        return data
    except Exception as e:
        print(f"❌ Match detection error ({sport}): {e}")
        return None

# ── Question Generator ────────────────────────────────────────────────────────

def generate_questions(match: dict, sport: str, existing_str: str, pre_match_count: int = 0) -> list:
    """
    Uses Gemini + Google Search to generate unique, non-hallucinatory questions.
    For PRE-MATCH, generates exactly 1 question, alternating between prediction (if even count) and debate (if odd).
    For IN-PLAY, generates 2 questions (1 prediction + 1 debate).
    For POST-MATCH, generates 2 questions (1 prediction + 1 debate).
    """
    phase = match.get("phase", "PRE-MATCH")
    teams = match.get("teams", "Unknown Teams")
    tournament = match.get("tournament", "")
    venue = match.get("venue", "")
    live_score = match.get("liveScore") or "Not available"
    key_players = match.get("keyPlayers", "")
    format_ = match.get("format", "")

    if sport == "cricket":
        sport_label = "cricket"
        phase_instruction = {
            "PRE-MATCH": "Focus on pre-match build-up: toss predictions, key player battles, pitch conditions, and team strategy.",
            "IN-PLAY": f"The match is LIVE. Current score: {live_score}. Focus on what is happening RIGHT NOW in the match based on this live context.",
            "POST-MATCH": "The match has ended. Focus on match review: key performances, impact of the result, and what it means for the tournament."
        }.get(phase, "")
    else:
        sport_label = "football"
        phase_instruction = {
            "PRE-MATCH": "Focus on pre-match build-up: formation predictions, key player battles, and which team has the tactical advantage.",
            "IN-PLAY": f"The match is LIVE. Current score: {live_score}. Focus on what is happening RIGHT NOW based on this live context.",
            "POST-MATCH": "The match has ended. Focus on match review: goal scorers, key moments, and what this result means for the tournament."
        }.get(phase, "")

    # For PRE-MATCH, alternate types based on the count of already posted pre-match questions
    if phase == "PRE-MATCH":
        if pre_match_count % 2 == 0:
            target_type = "prediction"
            target_instruction = "Generate exactly 1 PREDICTION. No debate questions."
        else:
            target_type = "debate"
            target_instruction = "Generate exactly 1 DEBATE. No prediction questions."
    else:
        target_type = "both"
        target_instruction = "Generate exactly 1 prediction AND 1 debate."

    prompt = f"""
    You are Dolly, a passionate and highly knowledgeable {sport_label} fan and analyst.
    
    MATCH DETAILS (verified from live Google Search):
    - Match: {teams}
    - Tournament: {tournament}
    - Venue: {venue}
    - Format: {format_}
    - Current Phase: {phase}
    - Live Score: {live_score}
    - Key Players: {key_players}
    
    PHASE INSTRUCTION: {phase_instruction}
    
    TARGET: {target_instruction}
    
    YOUR QUESTION STYLE:
    - Short and punchy. Maximum 2 sentences. 1 sentence is even better.
    - Confident, direct, slightly opinionated — like a knowledgeable fan asking a friend.
    - No jargon overload. Any casual fan should instantly understand.
    - Makes people want to pick a side or answer immediately.
    
    TWO TYPES OF QUESTIONS:
    1. PREDICTION — One specific, verifiable outcome. Answerable with a name, number, or yes/no.
       Good: "Will Smriti Mandhana be India's top scorer today?"
       Good: "Will this match see a penalty shootout?"
       Bad: "What do you think will happen?"
    
    2. DEBATE — Two genuinely opposing sides specific to this match and moment.
       Good: "India's spinners vs Australia's power hitters — who wins the key battle?"
       Good: "Mbappe or Vinicius Jr — who has the bigger game today?"
       Bad: "Who is the better team overall?"
    
    STRICT RULES TO PREVENT HALLUCINATION:
    - ONLY ask questions about things you found in the live search results above.
    - Do NOT invent stats, scores, or player names. Only use what is confirmed.
    - If you are not certain about something, do not include it in a question.
    - If the match details are insufficient to ask good questions, return an empty list [].
    - No Gen-Z slang, no emojis in the questions, no exclamation marks.
    - Options (sideA, sideB) must be 1 to 4 words only.
    
    Do NOT generate questions similar to any of these already posted:
    {existing_str if existing_str else "None"}
    
    Return ONLY a valid JSON list of objects:
    [
      {{
        "type": "prediction" or "debate",
        "text": "Short question?",
        "sideA": "Option A",
        "sideB": "Option B"
      }}
    ]
    """

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.3
            )
        )
        raw = response.text.strip()
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start == -1 or end == 0:
            print("⚠️ Gemini returned no JSON for question generation.")
            return []
        polls = json.loads(raw[start:end])
        print(f"📊 Gemini generated {len(polls)} questions for phase [{phase}].")
        return polls
    except Exception as e:
        print(f"❌ Question generation error: {e}")
        return []

# ── Post to Firestore ─────────────────────────────────────────────────────────

def publish_questions(db, polls: list, sport: str, room_id=None):
    """Writes generated questions to Firestore (room or global feed)."""
    for poll in polls:
        now_ms = int(time.time() * 1000)
        text = poll.get("text", "").strip()
        if not text:
            continue

        if room_id:
            msg_ref = db.collection("roarRooms").document(room_id).collection("messages").document()
            room_ref = db.collection("roarRooms").document(room_id)
            count_field = ROOM_COUNT_FIELD_BY_TYPE.get(poll.get("type", "prediction"))

            update_payload = {"fanCount": Increment(1)}
            if count_field:
                update_payload[count_field] = Increment(1)

            try:
                batch = db.batch()
                batch.set(msg_ref, {
                    "msgId": msg_ref.id,
                    "roomId": room_id,
                    "authorUid": "dolly-dolphin-bot",
                    "authorUsername": "Dolly",
                    "authorBadge": "RISING_FAN",
                    "type": poll.get("type", "prediction"),
                    "text": text,
                    "sideA": poll.get("sideA", "Yes"),
                    "sideB": poll.get("sideB", "No"),
                    "fireCount": 0,
                    "noChanceCount": 0,
                    "heartCount": 0,
                    "replyCount": 0,
                    "sport": sport,
                    "createdAt": now_ms,
                    "updatedAt": now_ms,
                })
                batch.update(room_ref, update_payload)
                batch.commit()
                print(f"🐬 Room [{room_id}]: [{poll.get('type')}] \"{text}\" (count_field={count_field})")
            except Exception as e:
                print(f"❌ Failed to publish+count Dolly message in room [{room_id}]: {e}")
        else:
            post_ref = db.collection("roarPosts").document()
            post_ref.set({
                "postId": post_ref.id,
                "authorUid": "dolly-dolphin-bot",
                "authorUsername": "Dolly",
                "authorBadge": "RISING_FAN",
                "type": poll.get("type", "prediction"),
                "sport": sport,
                "text": text,
                "sideA": poll.get("sideA", "Yes"),
                "sideB": poll.get("sideB", "No"),
                "agreeCount": 0,
                "disagreeCount": 0,
                "replyCount": 0,
                "likeCount": 0,
                "isLive": True,
                "status": "active",
                "audience": "Everyone",
                "createdAt": now_ms,
                "updatedAt": now_ms
            })
            print(f"🐬 Global [{sport}]: [{poll.get('type')}] \"{text}\"")

# ── Core Runner ───────────────────────────────────────────────────────────────

def run_dolly_for_sport(sport: str, room_id=None):
    """
    Full pipeline for one sport:
    1. Check if there is an active focus match linked to this room
    2. Fall back to global match detection if no link exists
    3. Check live status gating (silent if not live)
    4. Check cooldown & phase locks
    5. Fetch 4-Pillar data (Rivalries, Stats, History, Form) for story prompt enrichment
    6. Generate storytelling questions and publish to Firestore
    """
    db = init_firebase()
    target = f"Room [{room_id}]" if room_id else "Global Feed"
    print(f"\n🐬 Dolly running for sport={sport}, target={target}")

    match_id = None
    match_data = None

    # Step 1: Linked Match Resolution
    if room_id:
        room_doc = db.collection("roarRooms").document(room_id).get()
        if room_doc.exists:
            match_id = room_doc.to_dict().get("matchId")
            if match_id:
                match_doc = db.collection("matches").document(match_id).get()
                if match_doc.exists:
                    match_data = match_doc.to_dict()
                    print(f"🔗 Bound to focus match: {match_data.get('team_a')} vs {match_data.get('team_b')} via room matchId [{match_id}]")

    # Step 2: Fallback to detect live or upcoming match from matches table
    if not match_data:
        # Check live matches first
        matches_ref = db.collection("matches")\
            .where("sport", "==", sport)\
            .where("status", "==", "live")\
            .stream()
        for doc in matches_ref:
            match_id = doc.id
            match_data = doc.to_dict()
            print(f"🎯 Detected active live match from table: {match_data.get('team_a')} vs {match_data.get('team_b')} [{match_id}]")
            break

    # If no live match, check for upcoming matches that should be live now
    if not match_data:
        now_ms = int(time.time() * 1000)
        upcoming_ref = db.collection("matches")\
            .where("sport", "==", sport)\
            .where("status", "==", "upcoming")\
            .stream()
        for doc in upcoming_ref:
            m_data = doc.to_dict()
            kickoff = m_data.get("kickoff_time", 0)
            # If current time is past kickoff (or within 5 minutes before kickoff for build-up)
            if kickoff > 0 and now_ms >= (kickoff - 5 * 60 * 1000):
                match_id = doc.id
                match_data = m_data
                # Update status to live in Firestore automatically
                db.collection("matches").document(match_id).update({"status": "live", "updated_at": now_ms})
                match_data["status"] = "live"
                print(f"⏰ Kickoff time reached! Auto-transitioned match [{match_id}] to LIVE: {match_data.get('team_a')} vs {match_data.get('team_b')}")
                break

    if room_id and not match_data:
        # If we have a linked room but the match was upcoming, check if its kickoff time has arrived
        room_doc = db.collection("roarRooms").document(room_id).get()
        if room_doc.exists:
            match_id = room_doc.to_dict().get("matchId")
            if match_id:
                match_doc = db.collection("matches").document(match_id).get()
                if match_doc.exists:
                    m_data = match_doc.to_dict()
                    if m_data.get("status") == "upcoming":
                        kickoff = m_data.get("kickoff_time", 0)
                        now_ms = int(time.time() * 1000)
                        if kickoff > 0 and now_ms >= (kickoff - 5 * 60 * 1000):
                            match_data = m_data
                            db.collection("matches").document(match_id).update({"status": "live", "updated_at": now_ms})
                            match_data["status"] = "live"
                            print(f"⏰ Linked room kickoff reached! Auto-transitioned match [{match_id}] to LIVE: {match_data.get('team_a')} vs {match_data.get('team_b')}")

    if not match_data:
        print(f"⏭️ No active live match scheduled in database for {sport}. Dolly will stay silent.")
        return

    # Verify status
    if match_data.get("status") != "live":
        # Check if kickoff time for this specific match has arrived
        kickoff = match_data.get("kickoff_time", 0)
        now_ms = int(time.time() * 1000)
        if match_data.get("status") == "upcoming" and kickoff > 0 and now_ms >= (kickoff - 5 * 60 * 1000):
            db.collection("matches").document(match_id).update({"status": "live", "updated_at": now_ms})
            match_data["status"] = "live"
            print(f"⏰ Kickoff reached for linked match [{match_id}]! Auto-transitioned to LIVE.")
        else:
            print(f"🔒 Match [{match_id}] is {match_data.get('status')}. Dolly is gated to live matches only. Skipping.")
            return

    teams = f"{match_data.get('team_a')} vs {match_data.get('team_b')}"
    phase = "IN-PLAY" # Under live-only coverage model

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
        rival_docs = db.collection("matches").document(match_id).collection("rivalries").stream()
        rivalries = [r.to_dict() for r in rival_docs]
        
        stat_docs = db.collection("matches").document(match_id).collection("stats").stream()
        stats = [s.to_dict() for s in stat_docs]
        
        hist_docs = db.collection("matches").document(match_id).collection("matchup_history").stream()
        history = [h.to_dict() for h in hist_docs]
        
        form_doc = db.collection("matches").document(match_id).collection("tournament_form").document("latest").get()
        if form_doc.exists:
            form = form_doc.to_dict()
    except Exception as e:
        print(f"⚠️ Failed to load 4-pillar data: {e}. Fallback to live score questions.")

    # Create search instructions incorporating live scores
    live_context = ""
    try:
        now_ist = datetime.now(IST).strftime("%I:%M %p IST")
        search_query = f"India vs England T20 live scorecard ball by ball score today {now_ist}"
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

    # Build Prompt
    prompt = f"""
    You are Dolly, a passionate and highly knowledgeable sports analyst.
    Generate exactly 1 prediction AND 1 debate for the live match: {teams}.
    
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
    
    YOUR QUESTION STYLE:
    - Short and punchy. Maximum 2 sentences. 1 sentence is even better.
    - Confident, direct, articulate, and opinionated — like a professional analyst.
    - NO generic, lazy, or simple "who wins" or "is team X good" questions.
    - MUST ask about the CURRENT live status (e.g. current score, wickets, which batsmen are currently at the crease, or who is currently bowling).
    - MUST specifically reference at least one active player by name (e.g. "Will Suryakumar Yadav..." or "Can Archer's cutters...") to make the question highly specific and non-generic.
    - No Gen-Z slang, no emojis, no exclamation marks.
    - Options (sideA, sideB) must be 1 to 4 words only.
    - Frame your questions using your boss's Gen Z Sports Fan Discussion Category Matrix:
      
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
    - You must select one specific category from the matrix above (e.g., DRS controversy, performance slumps, mental pressure, or transfer value) and write your prediction/debate about it.
    - DO NOT write generic questions like "Who will win?" or "Will team X score Y runs?". Focus on the narrative, the player's character, or the tactical friction.

    ILLUSTRATIVE EXAMPLES TO PRIME YOUR OUTPUT (MATCH THESE STYLES):
    * Tactical Performance: "Virat's strike rate is awful — should he retire to the pavilion or accelerate?"
    * Team Controversy: "That was a blatant cheat — does that challenge deserve an immediate red card?"
    * DRS / VAR Drama: "That was never out — is DRS killing the spirit of this game?"
    * Player Performance Arc: "Slump or blip? The numbers say he's carrying the whole team this season."
    * Player Mental Health / Pressure: "The pressure got to him in the post-match presser — is coach rotation needed?"
    * Off-Pitch Lifestyle: "Airport fit goes viral — does X's off-pitch style match his on-pitch form?"
    * Social Causes / Action: "X took a knee today — will this spark team solidarity or fan division?"
    * Fan Creator / Transfer: "That performance will kill his transfer value — should we sell him this window?"
    Return ONLY a valid JSON list of objects:
    [
      {{
        "type": "prediction" or "debate",
        "text": "Short question?",
        "sideA": "Option A",
        "sideB": "Option B"
      }}
    ]
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
        
        # ── TODAY'S CUSTOM DEBATES (TEMPORARY FOR TONIGHT'S RUN) ──
        if sport == "cricket" and room_id:
            custom_debates = [
                {
                    "type": "debate",
                    "text": "Gambhir's gameplan for the white ball series in Ireland & England was just fine. It was a case of poor execution.",
                    "sideA": "Support",
                    "sideB": "Counter"
                },
                {
                    "type": "debate",
                    "text": "Team selection by India's think-tank is chiefly influenced by T20 performances. Is India missing an experienced hand or two to deal with swing & seam conditions in England?",
                    "sideA": "Needs Experience",
                    "sideB": "Back the Youth"
                },
                {
                    "type": "debate",
                    "text": "Caption this: Baz giving tips to GG",
                    "sideA": "Tactical Advice",
                    "sideB": "Just Banter"
                }
            ]
            # Fetch already posted questions to avoid duplicates
            posted_texts = get_existing_questions(db, room_id=room_id, sport=sport)
            
            # Find the first custom debate that hasn't been posted yet
            for cd in custom_debates:
                if cd["text"] not in posted_texts:
                    polls.append(cd)
                    print(f"📌 Injected custom boss debate: \"{cd['text']}\"")
                    break # Post exactly one custom debate per 20-min cycle alongside the normal poll
        # ──────────────────────────────────────────────────────────

        # Step 6: Publish
        publish_questions(db, polls, sport, room_id=room_id)
        stamp_phase_lock(db, sport, match_id, phase, room_id)
        print(f"✅ Dolly done for sport={sport}, phase={phase}, target={target}")
    except Exception as e:
        print(f"❌ Question generation failure: {e}")



# ── Automated Full Run ────────────────────────────────────────────────────────

def find_infinity_room_id(db) -> str | None:
    """
    Dynamically finds the SF360 Infinity Room by querying Firestore.
    Returns the room ID if found, else None.
    No hardcoding — works even if the room is renamed or recreated.
    """
    try:
        rooms = db.collection("roarRooms").stream()
        for room in rooms:
            data = room.to_dict()
            name = (data.get("name") or "").lower()
            if "infinity" in name:
                print(f"🌐 Infinity Room found: {room.id} ('{data.get('name')}')")
                return room.id
    except Exception as e:
        print(f"⚠️ Could not find Infinity Room: {e}")
    return None


def dolly_auto_run_all_rooms():
    """
    Master runner — runs automatically every 15 minutes via APScheduler on Render.

    Logic:
    - SF360 Infinity Room (common room): always gets BOTH cricket and football posts.
    - Cricket rooms: get cricket posts only (based on most upcoming cricket match).
    - Football rooms: get football posts only (based on most upcoming football match).
    - Global feed: gets both cricket and football posts.
    - No hardcoding of match details — Gemini + Google Search detects live/upcoming matches.
    - Anti-spam: 15-min cooldown between posts in any room.
    - Anti-hallucination: silent if no match found.
    - Pre-match: disabled. In-play: every 15 mins. Post-match: 1 post max.
    """
    db = init_firebase()
    posted_room_ids = set()  # Prevents double-posting to same room

    # ── Step 1: SF360 Infinity Room (common room — cricket + football) ────────
    print("\n🌐 ── Dolly: SF360 Infinity Room ──")
    infinity_room_id = find_infinity_room_id(db)
    if infinity_room_id:
        run_dolly_for_sport("cricket", room_id=infinity_room_id)
        run_dolly_for_sport("football", room_id=infinity_room_id)
        posted_room_ids.add(infinity_room_id)
    else:
        print("⚠️ Infinity Room not found. Skipping.")

    # ── Step 2: Global feed (cricket + football) ──────────────────────────────
    print("\n🌍 ── Dolly: Global Feed ──")
    run_dolly_for_sport("cricket", room_id=None)
    run_dolly_for_sport("football", room_id=None)

    # ── Step 3: All cricket rooms (cricket posts only) ────────────────────────
    print("\n🏏 ── Dolly: Cricket Rooms ──")
    cricket_rooms = db.collection("roarRooms").where("sport", "==", "cricket").stream()
    for room in cricket_rooms:
        if room.id not in posted_room_ids:
            run_dolly_for_sport("cricket", room_id=room.id)
            posted_room_ids.add(room.id)

    # ── Step 4: All football rooms (football posts only) ─────────────────────
    print("\n⚽ ── Dolly: Football Rooms ──")
    football_rooms = db.collection("roarRooms").where("sport", "==", "football").stream()
    for room in football_rooms:
        if room.id not in posted_room_ids:
            run_dolly_for_sport("football", room_id=room.id)
            posted_room_ids.add(room.id)

    print("\n🐬 Dolly full run complete.")


# ── Legacy alias for backward compatibility ───────────────────────────────────
def dolly_auto_run_all_cricket_rooms():
    dolly_auto_run_all_rooms()


if __name__ == "__main__":
    dolly_auto_run_all_rooms()
