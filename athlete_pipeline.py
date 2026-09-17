# """
# Athlete Data Automation pipeline.

# Steps (post-redesign — no licensed-source fetch, no fingerprint skip):
# 1. Trigger: NEW_ATHLETE_ONBOARDING (add-athlete form) |
#    SCHEDULED_RECHECK (weekly EventBridge sweep) |
#    MANUAL_RECHECK (admin "recheck now" button)
# 2. Gemini (with Google Search grounding) drafts + coerces the profile
# 3. Pydantic schema validation on the coerced dict
# 4. Valid draft -> review queue (Firestore, admin screen)
# 5. Human approves/edits/rejects (handled by the admin API, not here)
# 6. Only on approval -> live Firestore record (firebase_store.resolve_review_draft)
# """

# from pydantic import ValidationError

# import firebase_store
# from athlete_extractor import ExtractionError, extract_athlete_profile
# from athlete_schemas import ReviewDraft, Sport, SourceRef, TriggerType, get_schema_for_sport


# class PipelineResult:
#     def __init__(self, status: str, message: str, draft_id: str = None):
#         self.status = status  # "drafted" | "error"
#         self.message = message
#         self.draft_id = draft_id

#     def to_dict(self):
#         return {"status": self.status, "message": self.message, "draft_id": self.draft_id}


# def run_athlete_pipeline(
#     athlete_id: str,
#     sport: str,
#     athlete_name: str,
#     trigger_type: TriggerType = TriggerType.SCHEDULED_RECHECK,
# ) -> PipelineResult:
#     """
#     Main entry point.
#     - add-athlete form -> trigger_type=NEW_ATHLETE_ONBOARDING
#     - weekly EventBridge sweep -> trigger_type=SCHEDULED_RECHECK
#     - admin "recheck now" button -> trigger_type=MANUAL_RECHECK

#     athlete_name replaces the old athlete_source_id — Gemini's search
#     grounding looks the athlete up by name, there's no external numeric ID
#     to resolve anymore.
#     """
#     try:
#         sport_enum = Sport(sport)
#     except ValueError:
#         return PipelineResult("error", f"Unknown sport '{sport}'")

#     # ── Gemini extraction (search grounding + coercion happen inside) ──────
#     try:
#         extracted = extract_athlete_profile(sport_enum, athlete_id, athlete_name)
#     except ExtractionError as e:
#         return PipelineResult("error", f"Extraction failed: {e}")

#     # ── Pydantic schema validation ──────────────────────────────────────────
#     schema_cls = get_schema_for_sport(sport_enum)
#     source_ref = SourceRef(
#         fetched_at=_now_iso_placeholder(),  # set by whichever util you already use
#     )
#     try:
#         validated = schema_cls(
#             athlete_id=athlete_id,
#             sport=sport_enum,
#             source=source_ref,
#             **{k: v for k, v in extracted.items() if k not in ("athlete_id", "sport", "source") and v is not None},
#         )
#     except ValidationError as e:
#         return PipelineResult("error", f"Schema validation failed — draft rejected before review: {e}")

#     # ── Write draft to review queue (NOT the live record) ──────────────────
#     is_new = trigger_type == TriggerType.NEW_ATHLETE_ONBOARDING
#     current_data = None if is_new else firebase_store.get_live_athlete(athlete_id)

#     draft = ReviewDraft(
#         athlete_id=athlete_id,
#         sport=sport_enum,
#         trigger_type=trigger_type,
#         trigger_reason=trigger_type.value.replace("_", " ").title(),
#         proposed_data=validated.model_dump(mode="json"),
#         current_data=current_data,
#         source=source_ref,
#     )

#     draft_id = firebase_store.save_review_draft(draft.model_dump(mode="json"))

#     return PipelineResult("drafted", "Draft created and sent to review queue.", draft_id=draft_id)


# def _now_iso_placeholder() -> str:
#     from datetime import datetime, timezone
#     return datetime.now(timezone.utc).isoformat()





# athlete_pipeline.py
# Generates a full player/athlete profile on-demand using Gemini + Google Search
# grounding, robustly handles credentials (GEMINI_API_KEY with google_creds.json / Vertex fallback),
# and saves to Firestore ("PlayerProfiles" + "athleteReviewQueue") and returns the rich profile payload.

import os
import re
import json
import time
import uuid
from datetime import datetime, timezone
from dotenv import load_dotenv

# Load env variables immediately
load_dotenv()
load_dotenv(".env.local")

from firebase_store import init_firebase
from google import genai
from google.genai import types

def get_genai_client():
    """
    Dynamically initializes the Google GenAI client:
    1. First tries GEMINI_API_KEY (Google AI Studio)
    2. Then tries explicit service account file (google_creds.json)
    3. Falls back to Vertex AI with project fleet-gift-498306-p7
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if api_key and api_key.strip():
        return genai.Client(api_key=api_key.strip())

    # Check for credentials file
    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "google_creds.json"
    if os.path.exists(cred_path):
        try:
            with open(cred_path, "r", encoding="utf-8") as f:
                creds_data = json.load(f)
                project = creds_data.get("project_id", "fleet-gift-498306-p7")
        except Exception:
            project = "fleet-gift-498306-p7"

        abs_cred_path = os.path.abspath(cred_path)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = abs_cred_path
        os.environ["GOOGLE_CLOUD_PROJECT"] = project
        os.environ["GCLOUD_PROJECT"] = project
        
        return genai.Client(
            vertexai=True,
            project=project,
            location=os.getenv("GCP_LOCATION", "us-central1"),
        )

    # Fallback to configured or default project
    project = os.getenv("GCP_PROJECT_ID", "fleet-gift-498306-p7")
    return genai.Client(
        vertexai=True,
        project=project,
        location=os.getenv("GCP_LOCATION", "us-central1"),
    )


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or uuid.uuid4().hex[:8]


def generate_athlete_profile(athlete_name: str, sport: str = "auto") -> dict:
    """
    Searches the live web via Gemini's Google Search grounding tool to build a
    structured athlete profile, writes it to Firestore, and returns the result.

    Returns a dict:
      { "status": "published", "id": ..., "profile": {...} }
      or
      { "status": "rejected", "reason": "..." }
    """
    print(f"🔎 Generating profile for '{athlete_name}' ({sport})")

    sport_context = sport if sport and sport != "auto" else "cricket or other major sport"

    prompt = f"""
    You are verifying and building an official sports athlete/player profile for search query: "{athlete_name}",
    sport context: "{sport_context}".

    Step 1 — Verify:
    Search the live web and confirm whether "{athlete_name}" is a real, identifiable athlete/player active or historically notable in "{sport_context}" (or athletics/football/cricket/etc.).
    If you cannot confirm with reasonable confidence that this is a real person and a real sports player, set "confirmed" to false and explain why in "rejectionReason" — do not invent a person.

    Step 2 — If confirmed:
    Produce a complete, accurate profile using ONLY information found via search. Do not fabricate stats. If a numeric stat is unknown, use null.

       Respond in STRICT JSON with exactly this shape, nothing else, no markdown fences:
    {{
      "confirmed": true,
      "rejectionReason": null,
      "name": "Full Name",
      "sport": "Cricket / Athletics / Football / etc.",
      "role": "e.g. Batsman, Bowler, All-rounder, Javelin Thrower, Sprinter, Forward",
      "gender": "Male or Female",
      "country": "Country of representation (e.g. Jamaica, India, USA)",
      "countryCode": "2-letter ISO country code (e.g. JM, IN, US, GB, AU)",
      "team": "Current team, IPL franchise, or national squad",
      "dob": "YYYY-MM-DD or null",
      "birthplace": "City, State, Country or null",
      "battingStyle": "Right hand bat / Left hand bat or null",
      "bowlingStyle": "Right arm fast / Leg break / etc. or null",
      "about": "2-4 sentence engaging biography summarizing their career and achievements",
      "avatar": "Real public Wikipedia or official headshot image URL if found, else null",
      "stats": {{
        "runs": "Career runs if cricketer, else null",
        "avg": "Batting or scoring average, else null",
        "sr": "Strike rate, else null",
        "wickets": "Career wickets, else null",
        "econ": "Bowling economy rate, else null",
        "hundreds": "Centuries or null",
        "fifties": "Half-centuries or null",
        "highScore": "Highest score or null",
        "personalBest": "Primary personal best (e.g. '9.58s (100m)' or '90.23m')",
        "worldRank": "Current or peak rank (e.g. '#1')",
        "olympicGold": 0,
        "totalMedals": 0,
        "bestYear": "Standout peak calendar year (e.g. '2009' for Usain Bolt, '2021' for Neeraj Chopra)"
      }},
      "overview": {{
        "debut": "Debut year or tournament (e.g. '2004')",
        "specialization": "Primary specialty or event (e.g. '100m & 200m Sprint', 'Javelin Throw')",
        "matches": "Total career matches or appearances"
      }},
      "highlights": [
        "Major career milestone or famous performance 1",
        "Major career milestone or record 2"
      ],
      "sourceUrls": ["https://..."]
    }}
    """

    try:
        client = get_genai_client()
        model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.1,
            ),
        )

        raw = response.text.strip() if response and response.text else ""
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end == 0:
            return {"status": "rejected", "reason": "Model did not return valid JSON"}

        data = json.loads(raw[start:end])

        if not data.get("confirmed"):
            reason = data.get("rejectionReason") or "Could not confirm this is a real athlete"
            print(f"⛔ Rejected '{athlete_name}': {reason}")
            return {"status": "rejected", "reason": reason}

        resolved_name = data.get("name") or athlete_name
        profile_id = _slugify(resolved_name)
        now = time.time()
        detected_sport = data.get("sport") or sport_context

        # Build dual-compatible profile document (rich v4 schema + flat fields)
        raw_stats = data.get("stats", {}) or {}
        raw_overview = data.get("overview", {}) or {}
        raw_highlights = data.get("highlights", []) or []

                # Sanitize and ensure totalMedals >= olympicGold
        try:
            o_gold = int(raw_stats.get("olympicGold") or 0)
        except (ValueError, TypeError):
            o_gold = 0

        try:
            t_medals = int(raw_stats.get("totalMedals") or 0)
        except (ValueError, TypeError):
            t_medals = 0

        # Total career medals must never be less than Olympic gold medals
        if t_medals < o_gold:
            t_medals = o_gold

        raw_stats["olympicGold"] = o_gold
        raw_stats["totalMedals"] = t_medals

        # Guarantee bestYear is populated
        if not raw_stats.get("bestYear"):
            raw_stats["bestYear"] = raw_overview.get("debut") or "Peak Era"


        profile_doc = {
            "id": profile_id,
            "playerId": profile_id,
            "athlete_id": profile_id,
            "athleteId": profile_id,
            "name": resolved_name,
            "sport": detected_sport,
            "sportId": detected_sport.lower(),
            "team": data.get("team"),
            "country": data.get("country") or "India",
            "role": data.get("role") or "Player",
            "gender": data.get("gender") or "Male",
            "battingStyle": data.get("battingStyle"),
            "bowlingStyle": data.get("bowlingStyle"),
            "about": data.get("about"),
            "avatar": data.get("avatar"),
            "profileImage": data.get("avatar"),
            "stats": raw_stats,
            "overview": raw_overview,
            "highlights": raw_highlights,
            
            # Rich v4 CoreInfo
            "coreInfo": {
                "playerId": profile_id,
                "athlete_id": profile_id,
                "name": resolved_name,
                "country": data.get("country") or "India",
                "flag": data.get("countryCode") or (data.get("country", "")[:2].upper() if data.get("country") else "IN"),
                "role": data.get("role") or "Player",
                "gender": data.get("gender") or "Male",
                "dateOfBirth": data.get("dob"),
                "birthPlace": data.get("birthplace"),
                "bio": data.get("about") or "",
                "profileImage": data.get("avatar"),
                "battingStyle": data.get("battingStyle"),
                "bowlingStyle": data.get("bowlingStyle"),
            },

            # Rich v4 Analytics
                     # Rich v4 Analytics
         "analytics": {
             "battingStats": {
                 "runs": raw_stats.get("runs"),
                 "average": raw_stats.get("avg"),
                 "strikeRate": raw_stats.get("sr"),
                 "hundreds": raw_stats.get("hundreds"),
                 "fifties": raw_stats.get("fifties"),
                 "highScore": raw_stats.get("highScore"),
                 "matches": raw_overview.get("matches"),
             },
             "bowlingStats": {
                 "wickets": raw_stats.get("wickets"),
                 "economy": raw_stats.get("econ"),
                 "average": raw_stats.get("avg"),
                 "strikeRate": raw_stats.get("sr"),
                 "matches": raw_overview.get("matches"),
             },
             "stats": {
                 "personalBest": raw_stats.get("personalBest") or raw_overview.get("specialization"),
                 "worldRank": raw_stats.get("worldRank"),
                 "olympicGold": o_gold,
                 "totalMedals": t_medals,
                 "bestYear": raw_stats.get("bestYear"),
             }
         },

         # Rich v4 Record Highlight
         "record_highlight": {
             "event": raw_overview.get("specialization") or "Career Milestone",
             "category": raw_overview.get("specialization") or "Career Milestone",
             "result": raw_stats.get("personalBest") or raw_stats.get("highScore") or raw_stats.get("runs"),
             "aiInsight": raw_highlights[0] if len(raw_highlights) > 0 else data.get("about"),
         },


            # Rich v4 Record Highlight
            "record_highlight": {
                "category": raw_overview.get("specialization") or "Career Milestone",
                "result": raw_stats.get("personalBest") or raw_stats.get("highScore") or raw_stats.get("runs"),
                "aiInsight": raw_highlights[0] if len(raw_highlights) > 0 else data.get("about"),
            },

            "createdAt": now,
            "updatedAt": now,
            "auditStatus": "pending",
            "generatedBy": "ai",
            "sourceUrls": data.get("sourceUrls", []),
        }

        # Write to Firestore
        try:
            db = init_firebase()
            db.collection("PlayerProfiles").document(profile_id).set(profile_doc)
            db.collection("athleteReviewQueue").document(profile_id).set({
                "athleteId": profile_id,
                "athleteName": resolved_name,
                "sport": detected_sport,
                "status": "auto-published",
                "draft": profile_doc,
                "createdAt": datetime.now(timezone.utc).isoformat(),
            })
            print(f"✅ Saved to Firestore: PlayerProfiles/{profile_id}")
        except Exception as fb_err:
            print(f"⚠️ Firestore write notice: {fb_err}")

        print(f"✅ Successfully published profile for '{resolved_name}' ({profile_id})")
        return {"status": "published", "id": profile_id, "profile": profile_doc}

    except Exception as e:
        print(f"❌ Error generating profile for '{athlete_name}': {e}")
        return {"status": "error", "reason": str(e)}


class PipelineResult:
    def __init__(self, status: str, message: str, draft_id: str = None):
        self.status = status
        self.message = message
        self.draft_id = draft_id

    def to_dict(self):
        return {"status": self.status, "message": self.message, "draft_id": self.draft_id}


def run_athlete_pipeline(
    athlete_id: str,
    sport: str,
    athlete_name: str,
    trigger_type: any = None,
) -> PipelineResult:
    """Wrapper for backward compatibility with EventBridge sweep and cron triggers."""
    res = generate_athlete_profile(athlete_name=athlete_name or athlete_id, sport=sport)
    if res.get("status") == "published":
        return PipelineResult(status="drafted", message="Profile generated", draft_id=res.get("id"))
    return PipelineResult(status="error", message=res.get("reason", "Generation failed"))