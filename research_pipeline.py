import os
import json
import time
from firebase_store import init_firebase
from google import genai
from google.genai import types

db = init_firebase()

# ── Gemini Client Setup ────────────────────────────────────────────────────────
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

def run_match_research(match_id: str, team_a: str, team_b: str, sport: str, competition: str):
    """
    Runs automated LLM pre-match research to populate the 4 Content Pillars in Firestore.
    Uses Google Search grounding (Gemini GoogleSearch tool) to retrieve live context.
    """
    print(f"🕵️ Dolly starting Google Search Grounding Homework for Match [{match_id}]: {team_a} vs {team_b} ({sport})")
    
    if sport == "cricket":
        role_hints = "batsman, bowler, captain, coach"
        stat_hints = "strike rates, economy rates, average, wickets"
    else:
        role_hints = "striker, midfielder, defender, goalkeeper, coach"
        stat_hints = "clean sheets, possession rate, knockout conversion rates, late goals conceded"

    research_prompt = f"""
    Perform deep sports research on the upcoming {competition} match: {team_a} vs {team_b} ({sport}).
    Search the live web to find rich, high-confidence backstory narratives.
    
    Generate output strictly in JSON format with exactly 4 keys:
    1. "rivalries": A list of objects containing:
       - "type": "macro" (team vs team) or "micro" (player vs player)
       - "subjects": [string, string] (the teams or player names involved)
       - "role": one of: {role_hints} (only for micro)
       - "narrative": 2-4 sentences detailing the backstory, conflict, or pressure.
       
    2. "stats": A list of objects containing:
       - "metric": A compelling metric (e.g., "{stat_hints}")
       - "team_a_val": value for {team_a}
       - "team_b_val": value for {team_b}
       - "window": time window or scope of this metric
       
    3. "matchup_history": A list of objects containing:
       - "date": Date of past fixture (YYYY-MM-DD)
       - "result": Score and winner
       - "scorers": list of player names who scored
       - "notable_events": drama or controversial moments in that game
       
    4. "tournament_form": An object containing:
       - "team_a": {{"whats_working": "...", "gaps": "..."}}
       - "team_b": {{"whats_working": "...", "gaps": "..."}}
    """

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=research_prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.2
            )
        )
        
        raw = response.text.strip()
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end == 0:
            print("⚠️ Research response did not contain valid JSON.")
            return False
            
        data = json.loads(raw[start:end])
        
        # Write to Firestore subcollections under the specific match document
        match_doc_ref = db.collection("matches").document(match_id)
        
        # Pillar 1: Rivalries
        for r in data.get("rivalries", []):
            match_doc_ref.collection("rivalries").add({**r, "createdAt": time.time()})
            
        # Pillar 2: Stats
        for s in data.get("stats", []):
            match_doc_ref.collection("stats").add({**s, "createdAt": time.time()})
            
        # Pillar 3: Matchup History
        for h in data.get("matchup_history", []):
            match_doc_ref.collection("matchup_history").add({**h, "createdAt": time.time()})
            
        # Pillar 4: Tournament Form
        match_doc_ref.collection("tournament_form").document("latest").set({
            **data.get("tournament_form", {}),
            "updatedAt": time.time()
        })
        
        print(f"✅ Google Search Grounding Homework populated successfully for match: {match_id}")
        return True
    except Exception as e:
        print(f"❌ Failed to run pre-match research pipeline: {e}")
        return False
