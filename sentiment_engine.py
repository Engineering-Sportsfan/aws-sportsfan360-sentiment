import os
import json
from urllib.parse import urlparse
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

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

# ── FIFA prompt — EXACT AS GIVEN BY BOSS ──────────────────

FIFA_SYSTEM = """You are SportsFan360, the daily app for loyal fans of FIFA and football sport."""

FIFA_TASK = """Create a dashboard report of the following over the last 24 hours:
1. Link of the 3 most trending FIFA ads
2. Link of the 2 FIFA ads that misfired
3. 5 most trending statements made by major analysts, commentators, or football players
4. Top 5 global searches
5. Top 5 fan emotions
6. Top 5 footballer emotions across the countries participating in this FIFA world cup
7. Top 5 national emotions across the countries participating in this FIFA world cup
8. 3 most trending memes
9. Summarize the top 5 signals.

Context: Football. Soccer. FIFA World Cup 2026. We are doing a sentiment analysis of the FIFA World Cup 2026.
Audience: Football fans. India and global. Urban & semi-urban. Gen Z male. Gen Z female.
Persona: Football fan. Tone is friendly, energetic and sports friendly.
Constraints: Do not make up information that isn't on the internet. Do not repeat information from last report.
Instructions: do not create WORD document.

Return ONLY a valid JSON object with exactly this structure. Start your response directly with the opening curly brace '{' and end with the closing curly brace '}'. Do not write any introductory or conversational text, and do not wrap it in markdown code blocks:

{
  "trending_ads": [
    {"rank": 1, "brand": "", "campaign_name": "", "link": "", "why_trending": "", "sentiment": "", "virality_score": ""}
  ],
  "misfired_ads": [
    {"rank": 1, "brand": "", "campaign_name": "", "link": "", "what_went_wrong": "", "fan_reaction": ""}
  ],
  "trending_statements": [
    {"rank": 1, "person": "", "role": "", "statement": "", "source": "", "why_trending": ""}
  ],
  "top_searches": [
    {"rank": 1, "search_term": "", "why_trending": ""}
  ],
  "fan_emotions": [
    {"rank": 1, "emotion": "", "score": "", "trend": "", "driven_by": ""}
  ],
  "footballer_emotions": [
    {"rank": 1, "emotion": "", "players": "", "trend": ""}
  ],
  "national_emotions": [
    {"rank": 1, "country": "", "dominant_emotion": "", "key_driver": "", "mood_score": ""}
  ],
  "trending_memes": [
    {"rank": 1, "title": "", "format": "", "why_trending": "", "virality_score": ""}
  ],
  "top_signals": [
    {"rank": 1, "signal_title": "", "summary": "", "sportsfan360_play": ""}
  ]
}

Rules:
- trending_ads: exactly 3 items. If there is no active ad campaign, analyze major sponsors/brand campaigns related to the upcoming tournament or the sport generally.
- misfired_ads: exactly 2 items. If no brand campaigns have misfired, use "Insufficient data for this period" only for the misfired ads section, or highlight brands facing pre-tournament public backlash.
- trending_statements: exactly 5 items.
- top_searches: exactly 5 items.
- fan_emotions: exactly 5 items. Even in the pre-tournament phase, do NOT return "Insufficient data for this period". Instead, analyze fan emotions driven by ticket prices, squad announcements, tournament anticipation, or warm-up games.
- footballer_emotions: exactly 5 items. Analyze emotions driven by squad call-ups, injury concerns, retirement announcements, or training camp vibes.
- national_emotions: exactly 5 items. Analyze national moods based on qualification pathways, visa struggles, or local fan hype.
- trending_memes: exactly 3 items. If no memes are active, identify viral social topics or trends.
- If a specific URL/link is not found, use the official main domain/homepage where the campaign/news is reported (e.g., "www.fifa.com", "www.espncricinfo.com", "www.afaqs.com") or leave it as "" (empty string).
- NEVER output placeholder links, dummy URLs (like ending in 12345), or Rickroll links.
- When outputting links, NEVER prefix them with "https://" or "http://". Instead, output them starting with "www." or the domain name (e.g., "www.youtube.com/watch?v=..." or "sportsbusinessjournal.com/..."). This is critical to prevent Google Cloud link rewriting.
- Only use real, direct, raw source URLs found via search — never return Google Cloud redirect links (like vertexaisearch.cloud.google.com)."""

# ── WT20W prompt ───────────────────────────────────────────

WT20W_SYSTEM = """You are SportsFan360, the daily app for loyal fans of cricket and women's sports."""

WT20W_TASK = """Create a dashboard report of the following over the last 24 hours:
1. Link of the 3 most trending ICC Women's T20 World Cup 2026 ads
2. Link of the 2 ICC Women's T20 World Cup 2026 ads that misfired
3. 5 most trending statements made by major analysts, commentators, or women cricketers
4. Top 5 global searches
5. Top 5 fan emotions
6. Top 5 cricketer emotions across the countries participating in this Women's T20 World Cup
7. Top 5 national emotions across the countries participating in this Women's T20 World Cup
8. 3 most trending memes
9. Summarize the top 5 signals.

Context: Cricket. T20. ICC Women's T20 World Cup 2026. Women's cricket. Female athletes.
Audience: Cricket fans. India and global. Urban & semi-urban. Gen Z male. Gen Z female.
Persona: Cricket fan. Tone is friendly, energetic and sports friendly.
Constraints: Do not make up information that isn't on the internet. Do not repeat information from last report. Women's cricket ONLY — no men's cricket players.

Return ONLY a valid JSON object with exactly this structure. Start your response directly with the opening curly brace '{' and end with the closing curly brace '}'. Do not write any introductory or conversational text, and do not wrap it in markdown code blocks:

{
  "trending_ads": [
    {"rank": 1, "brand": "", "campaign_name": "", "link": "", "why_trending": "", "sentiment": "", "virality_score": ""}
  ],
  "misfired_ads": [
    {"rank": 1, "brand": "", "campaign_name": "", "link": "", "what_went_wrong": "", "fan_reaction": ""}
  ],
  "trending_statements": [
    {"rank": 1, "person": "", "role": "", "statement": "", "source": "", "why_trending": ""}
  ],
  "top_searches": [
    {"rank": 1, "search_term": "", "why_trending": ""}
  ],
  "fan_emotions": [
    {"rank": 1, "emotion": "", "score": "", "trend": "", "driven_by": ""}
  ],
  "player_emotions": [
    {"rank": 1, "emotion": "", "players": "", "trend": ""}
  ],
  "national_emotions": [
    {"rank": 1, "country": "", "dominant_emotion": "", "key_driver": "", "mood_score": ""}
  ],
  "trending_memes": [
    {"rank": 1, "title": "", "format": "", "why_trending": "", "virality_score": ""}
  ],
  "top_signals": [
    {"rank": 1, "signal_title": "", "summary": "", "sportsfan360_play": ""}
  ]
}

Rules:
- trending_ads: exactly 3 items. If there is no active ad campaign, analyze major sponsors/brand campaigns related to the upcoming tournament or women's cricket generally.
- misfired_ads: exactly 2 items. If no brand campaigns have misfired, use "Insufficient data for this period" only for the misfired ads section, or highlight brands facing pre-tournament public backlash.
- trending_statements: exactly 5 items — women cricketers ONLY.
- player_emotions: exactly 5 items — WOMEN cricketers ONLY, no men's players. Even in the pre-tournament phase, do NOT return "Insufficient data for this period". Instead, analyze cricketer emotions driven by squad selections, training camps, upcoming warm-up preparations, or retirement announcements.
- fan_emotions: exactly 5 items. Analyze fan emotions driven by tournament excitement, ticketing, or team preparations.
- If a specific URL/link is not found, use the official main domain/homepage where the campaign/news is reported (e.g., "www.icc-cricket.com", "www.espncricinfo.com", "www.afaqs.com") or leave it as "" (empty string).
- NEVER output placeholder links, dummy URLs (like ending in 12345), or Rickroll links.
- When outputting links, NEVER prefix them with "https://" or "http://". Instead, output them starting with "www." or the domain name (e.g., "www.youtube.com/watch?v=..." or "sportsbusinessjournal.com/..."). This is critical to prevent Google Cloud link rewriting.
- Only use real, direct, raw source URLs found via search — never return Google Cloud redirect links (like vertexaisearch.cloud.google.com)."""


def run_sentiment_engine(sport: str = "FIFA_WC_2026") -> dict:
    print(f"\n🚀 Starting Gemini Sentiment Engine for {sport}...")

    system_prompt = FIFA_SYSTEM if sport == "FIFA_WC_2026" else WT20W_SYSTEM
    task = FIFA_TASK if sport == "FIFA_WC_2026" else WT20W_TASK

    def clean_links(data):
        if isinstance(data, dict):
            for k, v in list(data.items()):
                if k == "link" and isinstance(v, str):
                    val = v.strip()
                    if not val:
                        data[k] = ""
                        continue
                    
                    if "vertexaisearch.cloud.google.com" in val:
                        if sport == "FIFA_WC_2026":
                            data[k] = "https://www.fifa.com"
                        else:
                            data[k] = "https://www.icc-cricket.com"
                        continue
                    
                    # Prepend https:// if protocol is missing
                    if not val.startswith("http://") and not val.startswith("https://"):
                        val = "https://" + val
                    
                    try:
                        parsed = urlparse(val)
                        # Truncate to base domain to guarantee link loads successfully
                        data[k] = f"{parsed.scheme}://{parsed.netloc}"
                    except Exception:
                        if sport == "FIFA_WC_2026":
                            data[k] = "https://www.fifa.com"
                        else:
                            data[k] = "https://www.icc-cricket.com"
                else:
                    clean_links(v)
        elif isinstance(data, list):
            for item in data:
                clean_links(item)

    try:
        print(f"🔍 Gemini searching real internet for {sport}...")

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=task,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.1
            )
        )

        raw = response.text.strip()
        
        # Find the first '{' and the last '}' to extract the JSON block cleanly
        start_idx = raw.find('{')
        end_idx = raw.rfind('}')
        if start_idx != -1 and end_idx != -1:
            raw = raw[start_idx:end_idx + 1]

        report = json.loads(raw)
        
        # Post-process all links to resolve redirect issues and prefix requirements
        clean_links(report)
        
        print(f"✅ Gemini: {sport} report generated and links cleaned!")
        return report

    except json.JSONDecodeError as e:
        print(f"❌ JSON parse error: {e}")
        print(f"Raw response: {raw[:500]}")
        return None
    except Exception as e:
        print(f"❌ Error: {e}")
        return None


if __name__ == "__main__":
    fifa = run_sentiment_engine("FIFA_WC_2026")
    if fifa:
        print(json.dumps(fifa, indent=2))

    wt20w = run_sentiment_engine("WT20W_WC_2026")
    if wt20w:
        print(json.dumps(wt20w, indent=2))