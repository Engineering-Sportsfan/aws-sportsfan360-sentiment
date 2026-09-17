"""
batch_generate_cricket_players.py

Generates LLM (Gemini, search-grounded) draft JSONs for the 30-player
Indian Cricket T20 Asian Games 2026 squad, one file PER FORMAT per player
(T20I and IPL) -> 60 files total.

Output shape matches the CORRECT reference schema (kuldeep_yadav.json):
playerId-based, coreInfo/record_highlight/analytics.battingStats /
analytics.bowlingStats / analytics.seasonalData -- NOT the old
athlete_id + separate "performance" block shape.

Since a player gets two files (T20I and IPL) that would otherwise both
be format="T20", a top-level "tournament" field is included so the two
files stay distinguishable downstream (push_live_players_batch.py keys
its MS_Transactions sort key off it).

Requires: GEMINI_API_KEY in your environment (or a .env file), and the
google-genai SDK:
    pip install google-genai --break-system-packages

Usage:
    python batch_generate_cricket_players.py
    python batch_generate_cricket_players.py --only ravi_bishnoi,jasprit_bumrah
    python batch_generate_cricket_players.py --formats t20i
    python batch_generate_cricket_players.py --out-dir llm_cricketplayers_drafts
    python batch_generate_cricket_players.py --overwrite

Output:
    llm_cricketplayers_drafts/<playerId>_t20i.json
    llm_cricketplayers_drafts/<playerId>_ipl.json
"""

import os
import re
import json
import time
import argparse
import urllib.parse
import datetime as dt
from pathlib import Path
from typing import Optional

import requests
from player_image_lookup import get_player_photo_url

try:
    from google import genai
    from google.genai import types
except ImportError:
    raise SystemExit(
        "Missing dependency. Run: pip install google-genai --break-system-packages"
    )

# ---------------------------------------------------------------------------
# Squad (from Cricket_T20_Squad_Asian_Games_2026.pdf, Ministry sanction order
# dated 22.08.2026, Annexure-I)
# ---------------------------------------------------------------------------

MEN = [
    "Ravi Bishnoi", "Jasprit Bumrah", "Varun Chakaravarthy", "Shivam Dube",
    "Shreyas Iyer", "Nitish Kumar Reddy", "Ishan Kishan", "Washington Sundar",
    "Tilak Varma", "Axar Patel", "Harshit Rana", "Abhishek Sharma",
    "Arshdeep Singh", "Vaibhav Suryavanshi", "Sanju Samson",
]

WOMEN = [
    "Renuka Singh Thakur", "Arundhati Reddy", "Shafali Verma", "Harmanpreet Kaur",
    "Bharti Fulmali", "Kranti Goud", "Richa Ghosh", "Kamalini G",
    "Smriti Mandhana", "Sree Charani", "Shreyanka Patil", "Pratika Rawal",
    "Deepti Sharma", "Nandani Sharma", "Radha Yadav",
]

PLAYERS = [{"name": n, "gender": "Male"} for n in MEN] + [
    {"name": n, "gender": "Female"} for n in WOMEN
]

FORMATS = {
    "t20i": {"format": "T20", "tournament": "T20I"},
    "ipl": {"format": "T20", "tournament": "IPL"},
}

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
TIME_LIMIT_SECONDS = 45
MAX_RETRY_PASSES = 3

_RATE_LIMIT_MARKERS = ("429", "rate limit", "resource exhausted", "quota")


def _looks_like_rate_limit(exc: Exception) -> bool:
    return any(m in str(exc).lower() for m in _RATE_LIMIT_MARKERS)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


# ---------------------------------------------------------------------------
# Prompt & Schema Template -- matches kuldeep_yadav.json's real shape
# ---------------------------------------------------------------------------

SCHEMA_TEMPLATE = """{
  "playerId": "<playerId>",
  "sportId": "cricket",
  "format": "T20",
  "tournament": "<T20I or IPL>",
  "currentClubId": null,
  "coreInfo": {
    "playerId": "<playerId>",
    "name": "<full player name>",
    "country": "India",
    "flag": "IN",
    "role": "<e.g. Batter / Bowler / All-rounder / Wicketkeeper-Batter>",
    "battingStyle": "<e.g. Right-handed or Left-handed>",
    "bowlingStyle": "<e.g. Right-arm fast, Left-arm unorthodox spin, or null if not a bowler>",
    "dateOfBirth": "YYYY-MM-DD",
    "birthPlace": "<city, state, India>",
    "heightCm": null,
    "jerseyNo": "<jersey number as a string, or null>",
    "debutDate": "<YYYY-MM-DD debut date in this tournament, or null>",
    "profileImage": null,
    "bio": "<3-5 sentence career bio, factual, grounded in search results>"
  },
  "record_highlight": {
    "category": "<name of a notable personal record, e.g. Highest Individual Score or Best Bowling Figures>",
    "result": "<the record figure, e.g. '85 runs' or '5/40'>",
    "type": "PR",
    "typeFull": "Personal Record",
    "opponent": "<opponent team>",
    "venue": "<venue name>",
    "date": "YYYY-MM-DD",
    "aiInsight": "<2-3 sentence insight on why this record/moment mattered>",
    "progressData": [
      {"year": "<year>", "value": null, "event": "<vs Opponent, Venue>"}
    ],
    "benchmarks": [
      {"label": "Personal", "value": null, "holder": "<player name>", "date": null, "venue": null},
      {"label": "National", "value": null, "holder": null, "date": null, "venue": null},
      {"label": "World", "value": null, "holder": null, "date": null, "venue": null}
    ]
  },
  "analytics": {
    "battingStats": {
      "matches": null,
      "innings": null,
      "runs": null,
      "average": null,
      "strikeRate": null,
      "hundreds": null,
      "fifties": null,
      "highScore": null,
      "ballsFaced": null,
      "fours": null,
      "sixes": null,
      "dismissals": null,
      "ducks": null
    },
    "bowlingStats": {
      "matches": null,
      "innings": null,
      "wickets": null,
      "average": null,
      "economy": null,
      "strikeRate": null,
      "bestBowling": null,
      "oversBowled": null,
      "ballsBowled": null,
      "runsConceded": null,
      "fourWicketHauls": null,
      "fiveWicketHauls": null,
      "maidens": null
    },
    "seasonalData": [
      {"year": "<year>", "matches": null, "runs": null, "wickets": null, "average": null}
    ]
  }
}"""


def build_prompt(player_name: str, gender: str, tournament: str) -> str:
    return f"""You are drafting a structured sports-data profile for {player_name}, an Indian
national cricketer ({gender}), specifically covering their {tournament} career
statistics and career narrative.

Search for real, current, verifiable information about {player_name}'s
{tournament} career. If {player_name} has not played any {tournament} matches
at all (e.g. a domestic-only or uncapped player), it is fine and expected for
battingStats/bowlingStats/record_highlight to come back mostly null -- do NOT
invent numbers. Only fill fields you can actually find or reasonably derive
from search results.

Career-stat rules:
- battingStats and bowlingStats MUST reflect the player's TOTAL CAREER
  {tournament} numbers as of today, not a single match or season.
- Only fill a numeric field if you found a source for it. Leave null
  otherwise -- do not guess or extrapolate.
- Include all findable batting stats: matches, innings, runs, average, strikeRate,
  hundreds, fifties, highScore (e.g. "85" or "104*"), ballsFaced, fours, sixes,
  dismissals, ducks.
- Include all findable bowling stats: matches, innings, wickets, average, economy,
  strikeRate, bestBowling (e.g. "5/40"), oversBowled, ballsBowled, runsConceded,
  fourWicketHauls, fiveWicketHauls, maidens (rare in T20; 0 or null is acceptable).
- If the player is primarily a batter, bowlingStats will mostly be null
  (and vice versa for a specialist bowler). That is expected, not an error.
- seasonalData should be a list of real year-by-year rows if you can find them
  (matches, runs, wickets, average per year); omit years you can't verify
  rather than inventing them.

NULL-AVOIDANCE (for non-numeric narrative fields only): search properly
before defaulting bio/birthPlace/role/battingStyle/bowlingStyle/debutDate to
null -- these are usually findable even for less famous domestic players.
Only null them after a genuine search attempt fails.

CRITICAL FORMATTING RULES:
- Return ONLY a single raw JSON object matching the schema below.
- Do NOT output markdown fences (no ```json).
- Do NOT include comments (// or /* */).
- Do NOT include trailing commas.
- Ensure all string values are properly quoted with double quotes.

{SCHEMA_TEMPLATE}

Set "playerId" and "coreInfo.playerId" to "<slug>", "coreInfo.name" to
"{player_name}", and "tournament" to "{tournament}".
"""


# ---------------------------------------------------------------------------
# Robust JSON Parsing & Cleaning
# ---------------------------------------------------------------------------

def extract_json(text: str) -> dict:
    if not text:
        raise ValueError("Empty text received from LLM")

    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE | re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.MULTILINE).strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or start > end:
        raise ValueError(f"No JSON object found in response: {cleaned[:300]}")

    json_str = cleaned[start : end + 1]

    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        pass

    json_str = re.sub(r'(?<!http:)(?<!https:)//.*?(?=\n|$)', '', json_str)
    json_str = re.sub(r'/\*.*?\*/', '', json_str, flags=re.DOTALL)
    json_str = re.sub(r',\s*([}\]])', r'\1', json_str)
    json_str = re.sub(r':\s*([0-9]+\*)\s*([,}])', r': "\1"\2', json_str)
    json_str = re.sub(r'\b(None|undefined)\b', 'null', json_str)

    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        json_str = re.sub(r'<[^>]*>', 'null', json_str)
        try:
            return json.loads(json_str)
        except json.JSONDecodeError as final_e:
            raise RuntimeError(
                f"Could not parse JSON from Gemini response: {final_e}\n--- raw snippet ---\n{json_str[:600]}"
            ) from final_e


# ---------------------------------------------------------------------------
# Gemini call w/ search grounding
# ---------------------------------------------------------------------------

def call_gemini(client, prompt: str, time_limit: int = TIME_LIMIT_SECONDS):
    config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
        temperature=0.2,
    )
    start = time.time()
    resp = client.models.generate_content(model=MODEL, contents=prompt, config=config)
    elapsed = time.time() - start
    if elapsed > time_limit:
        print(f"    (warning: Gemini call took {elapsed:.1f}s, over {time_limit}s budget)")

    text = getattr(resp, "text", None)
    if not text:
        fr = sr = None
        try:
            fr = resp.candidates[0].finish_reason
            sr = resp.candidates[0].safety_ratings
        except Exception:
            pass
        raise RuntimeError(f"Empty Gemini response. finish_reason={fr} safety_ratings={sr}")

    sources = []
    try:
        grounding = resp.candidates[0].grounding_metadata
        if grounding and grounding.grounding_chunks:
            for chunk in grounding.grounding_chunks:
                uri = getattr(getattr(chunk, "web", None), "uri", None)
                if uri:
                    sources.append(uri)
    except Exception:
        pass

    return text, sources, elapsed


# ---------------------------------------------------------------------------
# Extra-stats enrichment (fallback if key extra stats were missed in pass 1)
# ---------------------------------------------------------------------------

def fetch_extra_cricket_stats(client, name: str, tournament: str) -> dict:
    prompt = f"""
You are looking up specific career cricket statistics for {name} in {tournament}
cricket. Use Google Search to find current, accurate figures.

Search for and report ONLY these fields, as a single JSON object:
{{
  "battingStats": {{
    "ballsFaced": null,
    "fours": null,
    "sixes": null,
    "dismissals": null,
    "highScore": "85",
    "ducks": 0
  }},
  "bowlingStats": {{
    "oversBowled": null,
    "ballsBowled": null,
    "runsConceded": null,
    "fourWicketHauls": 0,
    "fiveWicketHauls": 0,
    "maidens": 0
  }}
}}

Set values to null if not found. For highScore, return a string (e.g. "85" or "104*" if not out).
Respond with ONLY the raw JSON object above. No prose, no markdown code fences, no comments.
"""
    def _one_call() -> dict:
        resp = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=2048,
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )
        raw = (resp.text or "").strip()
        return extract_json(raw)

    try:
        try:
            data = _one_call()
        except Exception:
            time.sleep(2)
            data = _one_call()
        if not isinstance(data, dict):
            return {}
        return {
            "battingStats": data.get("battingStats") if isinstance(data.get("battingStats"), dict) else {},
            "bowlingStats": data.get("bowlingStats") if isinstance(data.get("bowlingStats"), dict) else {},
        }
    except Exception as e:
        print(f"    (extra-stats note for {name}: {e})")
        return {}


def merge_extra_cricket_stats(draft: dict, extra: dict) -> None:
    if not extra:
        return
    analytics = draft.setdefault("analytics", {})

    batting = analytics.get("battingStats") or {}
    for k, v in (extra.get("battingStats") or {}).items():
        if v is not None or k not in batting or batting.get(k) is None:
            batting[k] = v
    if batting:
        analytics["battingStats"] = batting

    bowling = analytics.get("bowlingStats") or {}
    for k, v in (extra.get("bowlingStats") or {}).items():
        if v is not None or k not in bowling or bowling.get(k) is None:
            bowling[k] = v
    if bowling:
        analytics["bowlingStats"] = bowling


def _needs_extra_stats(draft: dict) -> bool:
    """Check if extra stats (ballsFaced/fours/etc, oversBowled/etc) are missing."""
    analytics = draft.get("analytics", {})
    batting = analytics.get("battingStats") or {}
    bowling = analytics.get("bowlingStats") or {}

    batting_has_data = any(batting.get(k) is not None for k in ("runs", "matches", "average", "highScore"))
    bowling_has_data = any(bowling.get(k) is not None for k in ("wickets", "matches", "average", "bestBowling"))

    batting_extra_done = batting.get("ballsFaced") is not None or batting.get("fours") is not None
    bowling_extra_done = bowling.get("oversBowled") is not None or bowling.get("fourWicketHauls") is not None

    if batting_has_data and not batting_extra_done:
        return True
    if bowling_has_data and not bowling_extra_done:
        return True
    return False


# ---------------------------------------------------------------------------
# Draft assembly
# ---------------------------------------------------------------------------

def build_draft(player: dict, tournament_key: str, raw: dict, sources: list) -> dict:
    player_id = slugify(player["name"])
    fmt = FORMATS[tournament_key]

    core_info = raw.get("coreInfo", {}) or {}
    core_info["playerId"] = player_id
    core_info["name"] = player["name"]
    core_info["country"] = "India"
    core_info["flag"] = "IN"

    draft = {
        "playerId": player_id,
        "sportId": "cricket",
        "format": fmt["format"],
        "tournament": fmt["tournament"],
        "currentClubId": raw.get("currentClubId"),
        "coreInfo": core_info,
        "record_highlight": raw.get("record_highlight", {}) or {},
        "analytics": raw.get("analytics", {}) or {},
        "grounding_sources": sources,
        "source": {
            "source_name": "Gemini (search-grounded — unverified, needs human review)",
            "source_url": "https://sportsfan360.internal/llm-draft",
            "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        },
        "_retry_log": [],
        "searchTokens": list(
            dict.fromkeys(
                [player_id]
                + player["name"].lower().split()
                + [player["name"].lower(), "cricket", "india", tournament_key]
            )
        ),
    }

    # Resolve real profile image via Wikipedia / Wikimedia Commons
    try:
        photo_url = get_player_photo_url(player["name"])
        draft["coreInfo"]["profileImage"] = photo_url
    except Exception as e:
        print(f"    (image lookup note for {player['name']}: {e})")

    return draft


def generate_one(client, player: dict, tournament_key: str, out_dir: Path):
    player_id = slugify(player["name"])
    tournament = FORMATS[tournament_key]["tournament"]
    out_path = out_dir / f"{player_id}_{tournament_key}.json"

    prompt = build_prompt(player["name"], player["gender"], tournament).replace(
        "<slug>", player_id
    )

    last_err = None
    backoff = 10.0
    for attempt in range(1, MAX_RETRY_PASSES + 1):
        try:
            print(f"    Fetching search-grounded data from Gemini...")
            text, sources, elapsed = call_gemini(client, prompt)
            raw = extract_json(text)
            draft = build_draft(player, tournament_key, raw, sources)

            if _needs_extra_stats(draft):
                print(f"    Enriching extra stats...")
                extra = fetch_extra_cricket_stats(client, player["name"], tournament)
                merge_extra_cricket_stats(draft, extra)

            out_path.write_text(json.dumps(draft, indent=2, ensure_ascii=False))
            print(f"  OK  {out_path.name} ({elapsed:.1f}s)")
            return True
        except Exception as e:
            last_err = e
            if _looks_like_rate_limit(e):
                print(f"  attempt {attempt} rate-limited ({e}); backing off {backoff:.0f}s")
                time.sleep(backoff)
                backoff *= 2
            else:
                wait = 5 * attempt
                print(f"  attempt {attempt} failed ({e}); retrying in {wait}s")
                time.sleep(wait)

    print(f"  FAILED {player_id} [{tournament_key}] after {MAX_RETRY_PASSES} attempts: {last_err}")
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", type=str, default=None, help="comma-separated playerIds to run")
    parser.add_argument("--formats", type=str, default="t20i,ipl", help="comma-separated: t20i,ipl")
    parser.add_argument("--out-dir", type=str, default="llm_cricketplayers_drafts")
    parser.add_argument("--rate-limit-seconds", type=float, default=2.0)
    parser.add_argument("--overwrite", action="store_true", help="Force overwrite existing draft files")
    args = parser.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key:
        client = genai.Client(api_key=api_key)
    elif os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCLOUD_PROJECT")
        if not project:
            creds_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
            try:
                with open(creds_path) as f:
                    project = json.load(f).get("project_id")
            except Exception as e:
                raise SystemExit(f"Could not read project_id from {creds_path}: {e}")
        if not project:
            raise SystemExit(
                f"No 'project_id' found in {os.environ['GOOGLE_APPLICATION_CREDENTIALS']} "
                "and GOOGLE_CLOUD_PROJECT isn't set either."
            )
        location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
        client = genai.Client(vertexai=True, project=project, location=location)
    else:
        raise SystemExit(
            "No credentials found. Set either GEMINI_API_KEY, or "
            "GOOGLE_APPLICATION_CREDENTIALS for Vertex AI auth "
            "(the same auth your other scripts use)."
        )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    formats = [f.strip().lower() for f in args.formats.split(",") if f.strip()]
    for f in formats:
        if f not in FORMATS:
            raise SystemExit(f"Unknown format '{f}'. Valid: {list(FORMATS)}")

    only = None
    if args.only:
        only = {s.strip() for s in args.only.split(",") if s.strip()}

    players = PLAYERS
    if only:
        players = [p for p in players if slugify(p["name"]) in only]
        missing = only - {slugify(p["name"]) for p in players}
        if missing:
            print(f"Warning: no match for --only ids: {missing}")

    total = len(players) * len(formats)
    done = 0
    ok = 0
    print(f"Generating {total} drafts ({len(players)} players x {len(formats)} formats) -> {out_dir}/")

    for player in players:
        player_id = slugify(player["name"])
        print(f"\n[{player_id}] ({player['gender']})")
        for fmt_key in formats:
            done += 1
            print(f" [{done}/{total}] {fmt_key.upper()}")
            success = generate_one(client, player, fmt_key, out_dir)
            if success:
                ok += 1
            time.sleep(args.rate_limit_seconds)

    print(f"\nDone. {ok}/{total} drafts written to {out_dir}/")


if __name__ == "__main__":
    main()