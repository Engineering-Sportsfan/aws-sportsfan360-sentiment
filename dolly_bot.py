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
COOLDOWN_MINUTES = 15  # Minimum gap between posts in the same room/feed (matches 15-min schedule)

ROOM_COUNT_FIELD_BY_TYPE = {
    "post": "postCount",
    "chat": "postCount",
    "debate": "debateCount",
    "prediction": "predictionCount",
    "trivia": "triviaCount",
    "battle": "battleCount",
    "analysis": "postCount",   # Dolly Phase 6 analysis reads
    "story": "postCount",     # Dolly Phase 6 story arcs
}

# ── Phase Lock Helpers ────────────────────────────────────────────────────────

def get_phase_lock_key(sport: str, match_id: str, phase: str, room_id: str = None, bot_uid: str = "dolly-dolphin-bot") -> str:
    room_suffix = f"_{room_id}" if room_id else "_global"
    return f"dolly_phase_lock_{sport}_{match_id}_{phase}{room_suffix}_{bot_uid}"

PRE_MATCH_MAX_POSTS = 2   # 2 rounds of pre-match questions (~50 mins build-up)
POST_MATCH_MAX_POSTS = 2  # 2 rounds of post-match questions (~50 mins wrap-up)

def has_phase_been_posted(db, sport: str, match_id: str, phase: str, room_id: str = None, bot_uid: str = "dolly-dolphin-bot") -> bool:
    """
    Returns True if Dolly should be blocked from posting in this specific room.
    - IN-PLAY: Spaced out every 20 minutes using the timestamp check.
    - POST-MATCH: Locked to max 1 post.
    - PRE-MATCH: Locked to max 1 post.
    """
    if phase == "IN-PLAY":
        return False # No phase locks for in-play (cooldown handles spacing)
        
    key = get_phase_lock_key(sport, match_id, phase, room_id, bot_uid)
    doc = db.collection("dollyPhaseLocks").document(key).get()
    if not doc.exists:
        return False
    data = doc.to_dict()
    post_count = data.get("count", 1)
    
    if phase in ["POST-MATCH", "PRE-MATCH"]:
        return post_count >= 1
    return False



def stamp_phase_lock(db, sport: str, match_id: str, phase: str, room_id: str = None, bot_uid: str = "dolly-dolphin-bot"):
    """Stamps/increments this phase's post count in Firestore for this specific room."""
    key = get_phase_lock_key(sport, match_id, phase, room_id, bot_uid)
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

def was_recently_posted(db, room_id=None, sport="cricket", cooldown_minutes=COOLDOWN_MINUTES, bot_uid="dolly-dolphin-bot") -> bool:
    """Returns True if Dolly posted in this feed/room for this specific sport within the cooldown window."""
    cutoff_ms = int((time.time() - cooldown_minutes * 60) * 1000)
    try:
        if room_id:
            # Check only messages of the target sport to allow dual-sport rooms
            msgs = db.collection("roarRooms").document(room_id).collection("messages") \
                .where("authorUid", "==", bot_uid) \
                .where("sport", "==", sport).stream()
            for msg in msgs:
                if msg.to_dict().get("createdAt", 0) > cutoff_ms:
                    return True
        else:
            posts = db.collection("roarPosts") \
                .where("authorUid", "==", bot_uid) \
                .where("sport", "==", sport).stream()
            for post in posts:
                if post.to_dict().get("createdAt", 0) > cutoff_ms:
                    return True
    except Exception as e:
        print(f"⚠️ Cooldown check error: {e}")
    return False

def get_existing_questions(db, room_id=None, sport="cricket", bot_uid="dolly-dolphin-bot"):
    """Fetches last 30 question texts in this specific room or feed to prevent duplicate posts."""
    questions = []
    try:
        if room_id:
            room_ref = db.collection("roarRooms").document(room_id).collection("messages") \
                .where("authorUid", "==", bot_uid).stream()
            room_posts = sorted([d for d in room_ref],
                                 key=lambda x: x.to_dict().get("createdAt", 0), reverse=True)
            for doc in room_posts[:30]:
                text = doc.to_dict().get("text")
                if text:
                    questions.append(text)
        else:
            global_ref = db.collection("roarPosts").where("authorUid", "==", bot_uid) \
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

def publish_questions(db, polls: list, sport: str, room_id=None, bot_uid="dolly-dolphin-bot", bot_username="Dolly"):
    """Writes generated questions to Firestore (room or global feed)."""
    for poll in polls:
        now_ms = int(time.time() * 1000)
        text = poll.get("text", "").strip()
        bullet_points = poll.get("bulletPoints", [])
        # Analysis cards have empty text by design — content is in bulletPoints.
        # Only skip if BOTH are empty (truly empty/broken response from LLM).
        if not text and not bullet_points:
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
                    "authorUid": bot_uid,
                    "authorUsername": bot_username,
                    "authorBadge": "RISING_FAN",
                    "type": poll.get("type", "prediction"),
                    "text": text,
                    "sideA": poll.get("sideA", "Yes"),
                    "sideB": poll.get("sideB", "No"),
                    "cardType": poll.get("cardType", ""),
                    "title": poll.get("title", ""),
                    "bulletPoints": poll.get("bulletPoints", []),
                    "fireCount": 0,
                    "noChanceCount": 0,
                    "heartCount": 0,
                    "replyCount": 0,
                    "sport": sport,
                    "isBot": True,
                    "botRole": "neutral",
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
                "authorUid": bot_uid,
                "authorUsername": bot_username,
                "authorBadge": "RISING_FAN",
                "type": poll.get("type", "prediction"),
                "sport": sport,
                "text": text,
                "sideA": poll.get("sideA", "Yes"),
                "sideB": poll.get("sideB", "No"),
                "cardType": poll.get("cardType", ""),
                "title": poll.get("title", ""),
                "bulletPoints": poll.get("bulletPoints", []),
                "agreeCount": 0,
                "disagreeCount": 0,
                "replyCount": 0,
                "likeCount": 0,
                "isLive": True,
                "status": "active",
                "audience": "Everyone",
                "isBot": True,
                "botRole": "neutral",
                "createdAt": now_ms,
                "updatedAt": now_ms
            })
            print(f"🐬 Global [{sport}]: [{poll.get('type')}] \"{text}\"")

# ── Core Runner ───────────────────────────────────────────────────────────────

def run_dolly_for_sport(sport: str, room_id=None, bot_uid="dolly-dolphin-bot", bot_username="Dolly"):
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
    # Check roarRooms first, then watchAlongRooms (for linked integrated rooms)
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

    if match_data.get("status") == "live":
        phase = "IN-PLAY"
    elif match_data.get("status") == "upcoming":
        kickoff = match_data.get("kickoff_time", 0)
        now_ms = int(time.time() * 1000)
        # If it's within 1 hour of kickoff, it's pre-match build up
        if kickoff > 0 and now_ms >= (kickoff - 60 * 60 * 1000) and now_ms < (kickoff - 5 * 60 * 1000):
            phase = "PRE-MATCH"
        elif kickoff > 0 and now_ms >= (kickoff - 5 * 60 * 1000):
            db.collection("matches").document(match_id).update({"status": "live", "updated_at": now_ms})
            match_data["status"] = "live"
            phase = "IN-PLAY"
            print(f"⏰ Kickoff reached for match [{match_id}]! Auto-transitioned to LIVE.")
        else:
            print(f"🔒 Match [{match_id}] is upcoming but too early for pre-match (not within 1 hour). Skipping.")
            return
    elif match_data.get("status") == "completed":
        phase = "POST-MATCH"
    else:
        print(f"🔒 Match [{match_id}] is {match_data.get('status')}. Skipping.")
        return

    teams = f"{match_data.get('team_a')} vs {match_data.get('team_b')}"

    # Step 3: Phase lock check
    if has_phase_been_posted(db, sport, match_id, phase, room_id, bot_uid):
        print(f"🔒 Phase lock active: Already posted [{phase}] for match [{match_id}] in target [{target}]. Skipping.")
        return

    # Step 4: Cooldown check
    if was_recently_posted(db, room_id=room_id, sport=sport, bot_uid=bot_uid):
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

    # Build Prompt
    prompt = f"""
    You are Dolly, a passionate and highly knowledgeable sports analyst and storyteller.
    Current match: {teams}. Current phase: {phase}.
    Your job is to produce one structured content card for this match — either a deep narrative Story Arc or a structured Analysis Read, depending on the phase.
    
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
    - If Phase is PRE-MATCH or POST-MATCH: You MUST output exactly ONE object with cardType "analysis". 
      "text" is empty. "title" is your analysis headline. "bulletPoints" is a list of 3 insightful points. "type" is "analysis".
    - If Phase is IN-PLAY: You MUST output exactly ONE object with cardType "story". 
      "text" is your deep narrative story (e.g. rivalry angle, player arc). "title" is your story headline. "type" is "story".
    - Do NOT generate generic predictions or debates anymore. Focus purely on Analysis and Story Arcs.

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
      {
        "type": "analysis" or "story",
        "cardType": "analysis" or "story",
        "title": "Short headline",
        "text": "Full narrative story (only if cardType is story, else empty)",
        "bulletPoints": ["Point 1", "Point 2", "Point 3"]
      }
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
        
        # Step 6: Publish
        publish_questions(db, polls, sport, room_id=room_id, bot_uid=bot_uid, bot_username=bot_username)
        stamp_phase_lock(db, sport, match_id, phase, room_id, bot_uid)
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
