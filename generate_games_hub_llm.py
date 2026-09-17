# # """
# # generate_games_hub_llm.py

# # Standalone content generator for SportsFan360's "Games Hub" screen — the
# # multi-sport event overview shown for things like the Asian Games /
# # Commonwealth Games / Olympics: a live MEDAL TABLE (ranked by gold, with a
# # focus-country card highlighting rank/medals/improvement-vs-previous-edition)
# # plus a TODAY'S EVENTS feed (per-sport schedule items with live status).

# # Modeled directly on generate_athlete_content_llm.py's architecture —
# # Gemini + Google Search grounding, NULL-AVOIDANCE prompting, the same
# # generic schema-driven coercion engine (copied, not imported, same reason
# # the athlete/match-center scripts each keep their own copy: avoids a
# # cross-file import for what's a handful of small pure functions) — but
# # this is a NEW, separate script/schema, sibling to
# # generate_athlete_content_llm.py and generate_match_center_llm.py, not an
# # extension of either.

# # Output shape (see GamesHubDocument):
# #   - gamesInfo: identity of the games edition (name, host city, dates, status)
# #   - medalTable: list[MedalTableEntry], ranked, all participating countries
# #     Gemini can find standings for
# #   - focusCountry: MedalTableEntry-shaped highlight PLUS
# #     previousEditionComparison (rank/medals at the prior edition + a plain
# #     "improved" | "declined" | "same" verdict) — this is the top red/amber
# #     card in the reference screenshots
# #   - todaysEvents: list[GamesEvent] — one entry per notable event happening
# #     "today" for the focus country, with sport, athlete/team name, event
# #     name, time, and status (LIVE / UPCOMING / FINISHED, the last carrying
# #     a result/medal outcome)

# # NOTE: search-grounded LLM output can go stale within hours for a live
# # multi-day games (medal counts and event status both change constantly).
# # This script is a DRAFTING aid for the review queue — like the athlete and
# # match-center scripts — not a live-scores feed. For genuinely real-time
# # medal counts/event status, a live sports-data API is the right source;
# # this script is for when no such feed is wired up yet, or for backfilling
# # historical/completed games editions.

# # Everything lives in this one file on purpose, matching the pattern of
# # every other script in this pipeline (generate_athlete_content_llm.py,
# # generate_match_center_llm.py, generate_records_explorer_llm.py).
# # """

# # import json
# # import os
# # import re
# # import sys
# # import typing
# # from datetime import datetime, timezone
# # from enum import Enum
# # from typing import Optional

# # from google import genai
# # from google.genai import types
# # from pydantic import BaseModel, Field, HttpUrl, ValidationError

# # import firebase_store


# # # ═══════════════════════════════════════════════════════════════════════
# # # SCHEMAS
# # # ═══════════════════════════════════════════════════════════════════════

# # class EventStatus(str, Enum):
# #     LIVE = "LIVE"
# #     UPCOMING = "UPCOMING"
# #     FINISHED = "FINISHED"


# # class SourceRef(BaseModel):
# #     source_name: str = Field(default="Gemini (Google Search grounding)")
# #     source_url: Optional[HttpUrl] = None
# #     fetched_at: str  # ISO8601 timestamp, set by the pipeline


# # class GamesInfo(BaseModel):
# #     name: str = Field(..., description="e.g. 'Asian Games 2026'")
# #     edition: Optional[str] = Field(None, description="e.g. '20th Asian Games'")
# #     hostCity: Optional[str] = Field(None, description="e.g. 'Aichi-Nagoya, Japan'")
# #     startDate: Optional[str] = Field(None, description="ISO date YYYY-MM-DD if known")
# #     endDate: Optional[str] = Field(None, description="ISO date YYYY-MM-DD if known")
# #     status: Optional[str] = Field(None, description="'UPCOMING' | 'LIVE' | 'CONCLUDED'")

# #     model_config = {"extra": "forbid"}


# # class MedalTableEntry(BaseModel):
# #     """
# #     ALL-TIME cumulative medal counts for this country across every past
# #     edition of these games UP THROUGH AND INCLUDING the current edition
# #     (e.g. for "Asian Games 2026", the sum of every Asian Games a country
# #     has ever competed in, 1951 through 2026) — NOT just the medals won at
# #     the single edition named in gamesInfo. See _build_prompt's
# #     ALL-TIME-CUMULATIVE instruction for how this is sourced.
# #     """
# #     rank: Optional[int] = Field(None, description="all-time cumulative rank, not this single edition's rank")
# #     country: str
# #     countryCode: Optional[str] = Field(None, description="2-letter ISO country code, e.g. 'IN', 'CN', 'JP'")
# #     gold: int = 0
# #     silver: int = 0
# #     bronze: int = 0
# #     total: int = 0

# #     model_config = {"extra": "forbid"}


# # class PreviousEditionComparison(BaseModel):
# #     """
# #     Compares the focus country's ALL-TIME cumulative rank/total as of the
# #     PREVIOUS edition (i.e. cumulative through previousEdition, not that
# #     edition's own single-games tally) against its all-time cumulative
# #     rank/total now, through the current edition named in gamesInfo.
# #     """
# #     previousEdition: Optional[str] = Field(None, description="e.g. 'Hangzhou 2023'")
# #     previousRank: Optional[int] = Field(None, description="all-time cumulative rank through previousEdition")
# #     previousTotal: Optional[int] = Field(None, description="all-time cumulative medal total through previousEdition")
# #     verdict: Optional[str] = Field(None, description="exactly one of: 'improved', 'declined', 'same'")
# #     summary: Optional[str] = Field(None, description="e.g. 'Rank #4 · 107 medals total (all-time, through Hangzhou 2023)'")

# #     model_config = {"extra": "forbid"}


# # class FocusCountryCard(MedalTableEntry):
# #     previousEditionComparison: Optional[PreviousEditionComparison] = None

# #     model_config = {"extra": "forbid"}


# # class GamesEvent(BaseModel):
# #     sport: str = Field(..., description="e.g. 'Shooting', 'Badminton', 'Football', 'Hockey', 'Kabaddi', 'Boxing', 'Wrestling'")
# #     eventName: str = Field(..., description="e.g. '10m Air Rifle Final', 'Men's Singles QF', 'IND vs KOR Quarter-Final'")
# #     participant: str = Field(..., description="athlete name or team/country matchup, e.g. 'Saurabh Chaudhary' or 'India Men'")
# #     time: Optional[str] = Field(None, description="display time, e.g. '10:00 AM'")
# #     status: EventStatus
# #     result: Optional[str] = Field(
# #         None, description="only for FINISHED events — outcome summary, e.g. 'Gold', 'Won 3-1' — null for LIVE/UPCOMING"
# #     )

# #     model_config = {"extra": "forbid"}


# # class GamesHubDocument(BaseModel):
# #     games_id: str
# #     gamesInfo: GamesInfo
# #     medalTable: list[MedalTableEntry] = Field(default_factory=list)
# #     focusCountry: Optional[FocusCountryCard] = None
# #     todaysEvents: list[GamesEvent] = Field(default_factory=list)
# #     grounding_sources: list[str] = Field(default_factory=list)
# #     source: SourceRef

# #     model_config = {"extra": "forbid"}


# # # ═══════════════════════════════════════════════════════════════════════
# # # GENERATION PIPELINE
# # # ═══════════════════════════════════════════════════════════════════════

# # api_key = os.getenv("GEMINI_API_KEY")
# # if api_key:
# #     client = genai.Client(api_key=api_key)
# # else:
# #     client = genai.Client(
# #         vertexai=True,
# #         project=os.getenv("GCP_PROJECT_ID", "fleet-gift-498306-p7"),
# #         location=os.getenv("GCP_LOCATION", "us-central1"),
# #     )

# # GEMINI_MODEL = os.getenv("GAMES_HUB_EXTRACTION_MODEL", "gemini-2.5-flash")
# # OUTPUT_DIR = "llm_games_hub_drafts"


# # class GenerationError(Exception):
# #     pass


# # def _slugify(name: str) -> str:
# #     return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


# # def _unwrap_optional(annotation):
# #     origin = typing.get_origin(annotation)
# #     if origin is typing.Union:
# #         args = [a for a in typing.get_args(annotation) if a is not type(None)]
# #         if len(args) == 1:
# #             return args[0], True
# #     return annotation, False


# # def _is_model(tp) -> bool:
# #     return isinstance(tp, type) and issubclass(tp, BaseModel)


# # def _generic_coerce_scalar(value, target_type):
# #     if value is None:
# #         return None
# #     if target_type is str:
# #         if isinstance(value, str):
# #             return value
# #         if isinstance(value, bool):
# #             return str(value)
# #         if isinstance(value, (int, float)):
# #             return str(value)
# #         if isinstance(value, dict):
# #             return "; ".join(
# #                 f"{k.replace('_', ' ').title()}: {v}" for k, v in value.items() if v is not None
# #             ) or None
# #         if isinstance(value, list):
# #             return ", ".join(str(v) for v in value) or None
# #         return str(value)
# #     if target_type is int:
# #         if isinstance(value, int) and not isinstance(value, bool):
# #             return value
# #         if isinstance(value, float):
# #             return int(value)
# #         if isinstance(value, str):
# #             match = re.search(r"-?\d+", value)
# #             return int(match.group()) if match else None
# #         if isinstance(value, list):
# #             return len(value)
# #         return value
# #     if target_type is float:
# #         if isinstance(value, (int, float)) and not isinstance(value, bool):
# #             return float(value)
# #         if isinstance(value, str):
# #             match = re.search(r"-?\d+\.?\d*", value)
# #             return float(match.group()) if match else None
# #         return value
# #     return value


# # def _coerce_to_model(data, model_cls, path=""):
# #     """Same generic coercion engine as generate_athlete_content_llm.py —
# #     auto-coerces fields toward the expected type, and backfills required
# #     string fields the LLM omitted entirely with 'N/A' rather than letting
# #     Pydantic raise a bare 'Field required' error."""
# #     if not isinstance(data, dict):
# #         return data

# #     result = {}
# #     for name, field in model_cls.model_fields.items():
# #         if name not in data:
# #             continue
# #         value = data[name]
# #         annotation, _ = _unwrap_optional(field.annotation)
# #         origin = typing.get_origin(annotation)
# #         field_path = f"{path}.{name}" if path else name

# #         if origin in (list, typing.List):
# #             item_types = typing.get_args(annotation)
# #             item_type = item_types[0] if item_types else str
# #             if value is None or isinstance(value, str):
# #                 value = []
# #             elif not isinstance(value, list):
# #                 value = [value]
# #             if _is_model(item_type):
# #                 result[name] = [
# #                     _coerce_to_model(item, item_type, path=field_path)
# #                     for item in value
# #                     if isinstance(item, dict)
# #                 ]
# #             else:
# #                 result[name] = [
# #                     _generic_coerce_scalar(v, item_type) if item_type in (str, int, float) else v
# #                     for v in value
# #                     if v is not None
# #                 ]
# #             continue

# #         if _is_model(annotation):
# #             if isinstance(value, str):
# #                 value = None
# #             result[name] = (
# #                 _coerce_to_model(value, annotation, path=field_path) if isinstance(value, dict) else value
# #             )
# #             continue

# #         if annotation in (str, int, float):
# #             coerced = _generic_coerce_scalar(value, annotation)
# #             if coerced is None and annotation is str and field.is_required():
# #                 coerced = "N/A"
# #             result[name] = coerced
# #             continue

# #         # Enum fields (EventStatus) — pass through, Pydantic validates.
# #         result[name] = value

# #     for name, field in model_cls.model_fields.items():
# #         if name in result:
# #             continue
# #         if not field.is_required():
# #             continue
# #         annotation, _ = _unwrap_optional(field.annotation)
# #         if annotation is str:
# #             result[name] = "N/A"

# #     return result


# # def _sanitize(data: dict, focus_country: str) -> dict:
# #     """Forces countryCode="IN" for the focus-country entries whenever
# #     country is India, same reasoning/pattern as the athlete pipeline's
# #     coreInfo.flag fix — don't trust Gemini's free-text country-code
# #     guess for a value the pipeline already knows deterministically."""
# #     def _fix(entry):
# #         if isinstance(entry, dict) and isinstance(entry.get("country"), str):
# #             if entry["country"].strip().lower() == focus_country.strip().lower():
# #                 entry["countryCode"] = _COUNTRY_CODE_OVERRIDES.get(focus_country.strip().lower(), entry.get("countryCode"))
# #         return entry

# #     for e in data.get("medalTable") or []:
# #         _fix(e)
# #     if isinstance(data.get("focusCountry"), dict):
# #         _fix(data["focusCountry"])
# #     return data


# # _COUNTRY_CODE_OVERRIDES = {"india": "IN"}


# # def _extract_grounding_sources(response) -> list[str]:
# #     urls: list[str] = []
# #     try:
# #         candidates = getattr(response, "candidates", None) or []
# #         for candidate in candidates:
# #             metadata = getattr(candidate, "grounding_metadata", None)
# #             if not metadata:
# #                 continue
# #             chunks = getattr(metadata, "grounding_chunks", None) or []
# #             for chunk in chunks:
# #                 web = getattr(chunk, "web", None)
# #                 uri = getattr(web, "uri", None) if web else None
# #                 if uri:
# #                     urls.append(uri)
# #     except Exception:
# #         pass
# #     return list(dict.fromkeys(urls))


# # def _extract_json(raw_text: str, response_meta=None) -> dict:
# #     raw = (raw_text or "").strip()
# #     start = raw.find("{")
# #     end = raw.rfind("}") + 1
# #     if start == -1 or end == 0:
# #         diag = ""
# #         try:
# #             candidates = getattr(response_meta, "candidates", None) or []
# #             if candidates:
# #                 finish_reason = getattr(candidates[0], "finish_reason", None)
# #                 diag = f" [finish_reason={finish_reason}]"
# #             else:
# #                 diag = " [no candidates returned]"
# #         except Exception:
# #             pass
# #         raise GenerationError(f"Gemini response was empty or contained no JSON (length {len(raw)}){diag}")
# #     try:
# #         return json.loads(raw[start:end])
# #     except json.JSONDecodeError as e:
# #         raise GenerationError(f"Gemini response was not valid JSON: {e} [length {len(raw)}]") from e


# # def _build_prompt(games_name: str, focus_country: str) -> str:
# #     return f"""
# # You are drafting a "Games Hub" content document for SportsFan360, a sports fan engagement
# # platform. You have access to Google Search — use it to find CURRENT, accurate information
# # about this multi-sport games event rather than relying only on what you already know.

# # Games: {games_name}
# # Focus country: {focus_country}

# # Produce a single JSON object with exactly these top-level keys: gamesInfo, medalTable,
# # focusCountry, todaysEvents.

# # NULL-AVOIDANCE: use null only after genuinely searching and coming up empty. Medal counts
# # and event schedules are exactly the kind of fact search grounding should nail down
# # confidently — do not default to null or an empty list out of caution.

# # gamesInfo: {{name, edition, hostCity, startDate, endDate, status}}. status is exactly one of
# # "UPCOMING", "LIVE", "CONCLUDED" based on today's date vs the games' actual dates.

# # medalTable — IMPORTANT, ALL-TIME CUMULATIVE, NOT this single edition: this must be each
# # country's ALL-TIME TOTAL medal count across EVERY past edition of these games, summed up
# # through and including the current edition named above (e.g. for "Asian Games 2026", the sum
# # of every Asian Games a country has won medals at, from the very first edition through 2026)
# # — NOT just the medals won at this one 2026 edition. Search for the games' all-time /
# # historical / cumulative medal standings (often published as "all-time medal table" on
# # Wikipedia or the games' official site) and add this edition's medals on top if the
# # all-time table you find predates this edition. One entry per country: {{rank (all-time
# # cumulative rank), country, countryCode (2-letter ISO code, e.g. "IN", "CN", "JP"), gold,
# # silver, bronze, total}} — all four counts are ALL-TIME CUMULATIVE totals. Include every
# # country you can find, not just the top few.

# # focusCountry: the SAME all-time-cumulative shape as one medalTable entry for
# # "{focus_country}" specifically, PLUS previousEditionComparison: {{previousEdition (name of
# # the prior edition of these games, e.g. "Hangzhou 2023" for the Asian Games), previousRank
# # (the all-time cumulative rank {focus_country} held AS OF that previous edition, before this
# # current edition's medals were added), previousTotal (the all-time cumulative medal total as
# # of that previous edition), verdict (exactly one of "improved" | "declined" | "same",
# # comparing that previous all-time rank to the current all-time rank after this edition),
# # summary (one short display string, e.g. "Rank #4 · 107 medals total (all-time, through
# # Hangzhou 2023)")}}. Search specifically for {focus_country}'s all-time cumulative standing as
# # of the previous edition to fill this — do not leave it null.

# # todaysEvents: notable events for {focus_country} happening TODAY (or, if the games haven't
# # started yet / have already concluded, the most recent/relevant day's schedule) — list of
# # {{sport, eventName, participant, time, status, result}}. status is exactly one of "LIVE",
# # "UPCOMING", "FINISHED". participant is the specific athlete name for individual events or a
# # team/matchup string (e.g. "India Men", "IND vs KOR") for team events. result is ONLY filled
# # for FINISHED events (e.g. "Gold", "Won 3-1", "4th Place") — null for LIVE/UPCOMING. Include
# # as many real events as you can find for {focus_country} on that day — do not pad with
# # invented events, but do not stop at 2-3 if more are findable.

# # Respond with ONLY a single JSON object containing gamesInfo, medalTable, focusCountry, and
# # todaysEvents. No prose, no markdown code fences, no commentary before or after the JSON.
# # """


# # def _call_gemini(prompt: str):
# #     try:
# #         return client.models.generate_content(
# #             model=GEMINI_MODEL,
# #             contents=prompt,
# #             config=types.GenerateContentConfig(
# #                 temperature=0.2,
# #                 max_output_tokens=16384,
# #                 tools=[types.Tool(google_search=types.GoogleSearch())],
# #             ),
# #         )
# #     except Exception as e:
# #         raise GenerationError(f"Gemini generation call failed: {e}") from e


# # def generate_games_hub(games_name: str, focus_country: str = "India"):
# #     prompt = _build_prompt(games_name, focus_country)
# #     response = _call_gemini(prompt)
# #     try:
# #         data = _extract_json(response.text, response_meta=response)
# #     except GenerationError:
# #         response = _call_gemini(prompt)
# #         data = _extract_json(response.text, response_meta=response)

# #     data = _sanitize(data, focus_country)
# #     data = _coerce_to_model(data, GamesHubDocument)

# #     data["games_id"] = _slugify(games_name)
# #     data["grounding_sources"] = _extract_grounding_sources(response)
# #     data["source"] = SourceRef(
# #         source_name="Gemini (search-grounded — unverified, needs human review)",
# #         source_url="https://sportsfan360.internal/llm-draft",
# #         fetched_at=datetime.now(timezone.utc).isoformat(),
# #     ).model_dump(mode="json")

# #     doc = GamesHubDocument.model_validate(data)
# #     return doc, doc.model_dump(mode="json")


# # def save_draft_to_dynamodb(doc_dict: dict, games_name: str) -> Optional[str]:
# #     """Same review-queue pattern as generate_athlete_content_llm.py's
# #     save_draft_to_dynamodb — writes to SportsData as status=pending_review
# #     via firebase_store.save_review_draft(). Non-fatal on failure since the
# #     local JSON save already succeeded."""
# #     draft = {
# #         "games_id": doc_dict["games_id"],
# #         "games_name": games_name,
# #         "trigger_reason": "llm_games_hub_generation",
# #         "proposed_data": doc_dict,
# #         "status": "pending_review",
# #     }
# #     try:
# #         return firebase_store.save_review_draft(draft)
# #     except Exception as e:
# #         print(f"⚠️ Failed to save draft to DynamoDB: {e}", file=sys.stderr)
# #         return None


# # def main():
# #     os.makedirs(OUTPUT_DIR, exist_ok=True)
# #     while True:
# #         games_name = input("\nGames name (e.g. 'Asian Games 2026', blank to quit): ").strip()
# #         if not games_name:
# #             print("Done.")
# #             break
# #         focus_country = input("Focus country [India]: ").strip() or "India"

# #         print(f"\nGenerating Games Hub draft for {games_name} ({focus_country})...")
# #         try:
# #             doc, doc_dict = generate_games_hub(games_name, focus_country)
# #         except (GenerationError, ValidationError) as e:
# #             print(f"FAILED: {e}", file=sys.stderr)
# #             continue

# #         out_path = os.path.join(OUTPUT_DIR, f"{doc.games_id}.json")
# #         with open(out_path, "w") as f:
# #             json.dump(doc_dict, f, indent=2, default=str)

# #         draft_id = save_draft_to_dynamodb(doc_dict, games_name)
# #         if draft_id:
# #             print(f"✅ Draft also saved to DynamoDB review queue: draft_id={draft_id}")
# #         else:
# #             print("⚠️ DynamoDB save failed — local JSON was still saved above.")

# #         print(f"Saved: {out_path}")
# #         print(json.dumps(doc_dict, indent=2, default=str))


# # if __name__ == "__main__":
# #     main()











# """
# generate_games_hub_llm.py

# Standalone content generator for SportsFan360's "Games Hub" screen — the
# multi-sport event overview shown for things like the Asian Games /
# Commonwealth Games / Olympics: a live MEDAL TABLE (ranked by gold, with a
# focus-country card highlighting rank/medals/improvement-vs-previous-edition)
# plus a TODAY'S EVENTS feed (per-sport schedule items with live status).

# Modeled directly on generate_athlete_content_llm.py's architecture —
# Gemini + Google Search grounding, NULL-AVOIDANCE prompting, the same
# generic schema-driven coercion engine (copied, not imported, same reason
# the athlete/match-center scripts each keep their own copy: avoids a
# cross-file import for what's a handful of small pure functions) — but
# this is a NEW, separate script/schema, sibling to
# generate_athlete_content_llm.py and generate_match_center_llm.py, not an
# extension of either.

# Output shape (see GamesHubDocument):
#   - gamesInfo: identity of the games edition (name, host city, dates, status)
#   - medalTable: list[MedalTableEntry], ranked, all participating countries
#     Gemini can find standings for
#   - focusCountry: MedalTableEntry-shaped highlight PLUS
#     previousEditionComparison (rank/medals at the prior edition + a plain
#     "improved" | "declined" | "same" verdict) — this is the top red/amber
#     card in the reference screenshots
#   - todaysEvents: list[GamesEvent] — one entry per notable event happening
#     "today" for the focus country, with sport, athlete/team name, event
#     name, time, and status (LIVE / UPCOMING / FINISHED, the last carrying
#     a result/medal outcome)

# NOTE: search-grounded LLM output can go stale within hours for a live
# multi-day games (medal counts and event status both change constantly).
# This script is a DRAFTING aid for the review queue — like the athlete and
# match-center scripts — not a live-scores feed. For genuinely real-time
# medal counts/event status, a live sports-data API is the right source;
# this script is for when no such feed is wired up yet, or for backfilling
# historical/completed games editions.

# Everything lives in this one file on purpose, matching the pattern of
# every other script in this pipeline (generate_athlete_content_llm.py,
# generate_match_center_llm.py, generate_records_explorer_llm.py).
# """

# import json
# import os
# import re
# import sys
# import typing
# from datetime import datetime, timezone
# from enum import Enum
# from typing import Optional

# from google import genai
# from google.genai import types
# from pydantic import BaseModel, Field, HttpUrl, ValidationError

# import firebase_store


# # ═══════════════════════════════════════════════════════════════════════
# # SCHEMAS
# # ═══════════════════════════════════════════════════════════════════════

# class EventStatus(str, Enum):
#     LIVE = "LIVE"
#     UPCOMING = "UPCOMING"
#     FINISHED = "FINISHED"


# class SourceRef(BaseModel):
#     source_name: str = Field(default="Gemini (Google Search grounding)")
#     source_url: Optional[HttpUrl] = None
#     fetched_at: str  # ISO8601 timestamp, set by the pipeline


# class GamesInfo(BaseModel):
#     name: str = Field(..., description="e.g. 'Asian Games 2026'")
#     edition: Optional[str] = Field(None, description="e.g. '20th Asian Games'")
#     hostCity: Optional[str] = Field(None, description="e.g. 'Aichi-Nagoya, Japan'")
#     startDate: Optional[str] = Field(None, description="ISO date YYYY-MM-DD if known")
#     endDate: Optional[str] = Field(None, description="ISO date YYYY-MM-DD if known")
#     status: Optional[str] = Field(None, description="'UPCOMING' | 'LIVE' | 'CONCLUDED'")

#     model_config = {"extra": "forbid"}


# class MedalTableEntry(BaseModel):
#     """
#     ALL-TIME cumulative medal counts for this country across every past
#     edition of these games UP THROUGH AND INCLUDING the current edition
#     (e.g. for "Asian Games 2026", the sum of every Asian Games a country
#     has ever competed in, 1951 through 2026) — NOT just the medals won at
#     the single edition named in gamesInfo. See _build_prompt's
#     ALL-TIME-CUMULATIVE instruction for how this is sourced.
#     """
#     rank: Optional[int] = Field(None, description="all-time cumulative rank, not this single edition's rank")
#     country: str
#     countryCode: Optional[str] = Field(None, description="2-letter ISO country code, e.g. 'IN', 'CN', 'JP'")
#     gold: int = 0
#     silver: int = 0
#     bronze: int = 0
#     total: int = 0

#     model_config = {"extra": "forbid"}


# class PreviousEditionComparison(BaseModel):
#     """
#     Compares the focus country's ALL-TIME cumulative rank/total as of the
#     PREVIOUS edition (i.e. cumulative through previousEdition, not that
#     edition's own single-games tally) against its all-time cumulative
#     rank/total now, through the current edition named in gamesInfo.
#     """
#     previousEdition: Optional[str] = Field(None, description="e.g. 'Hangzhou 2023'")
#     previousRank: Optional[int] = Field(None, description="all-time cumulative rank through previousEdition")
#     previousTotal: Optional[int] = Field(None, description="all-time cumulative medal total through previousEdition")
#     verdict: Optional[str] = Field(None, description="exactly one of: 'improved', 'declined', 'same'")
#     summary: Optional[str] = Field(None, description="e.g. 'Rank #4 · 107 medals total (all-time, through Hangzhou 2023)'")

#     model_config = {"extra": "forbid"}


# class FocusCountryCard(MedalTableEntry):
#     previousEditionComparison: Optional[PreviousEditionComparison] = None

#     model_config = {"extra": "forbid"}


# class GamesEvent(BaseModel):
#     sport: str = Field(..., description="exactly one of: 'Track & Field', 'Boxing', 'Kabaddi', 'Cricket', 'Football'")
#     eventName: str = Field(..., description="e.g. '10m Air Rifle Final', 'Men's Singles QF', 'IND vs KOR Quarter-Final'")
#     participant: str = Field(..., description="athlete name or team/country matchup, e.g. 'Saurabh Chaudhary' or 'India Men'")
#     date: Optional[str] = Field(None, description="ISO date YYYY-MM-DD this event actually takes/took place")
#     time: Optional[str] = Field(None, description="display time, e.g. '10:00 AM'")
#     status: EventStatus
#     result: Optional[str] = Field(
#         None, description="only for FINISHED events — medal/outcome summary, e.g. 'Gold', 'Won 3-1' — null for LIVE/UPCOMING"
#     )

#     model_config = {"extra": "forbid"}


# class GamesHubDocument(BaseModel):
#     games_id: str
#     gamesInfo: GamesInfo
#     medalTable: list[MedalTableEntry] = Field(default_factory=list)
#     focusCountry: Optional[FocusCountryCard] = None
#     todaysEvents: list[GamesEvent] = Field(default_factory=list)
#     grounding_sources: list[str] = Field(default_factory=list)
#     source: SourceRef

#     model_config = {"extra": "forbid"}


# # ═══════════════════════════════════════════════════════════════════════
# # GENERATION PIPELINE
# # ═══════════════════════════════════════════════════════════════════════

# api_key = os.getenv("GEMINI_API_KEY")
# if api_key:
#     client = genai.Client(api_key=api_key)
# else:
#     client = genai.Client(
#         vertexai=True,
#         project=os.getenv("GCP_PROJECT_ID", "fleet-gift-498306-p7"),
#         location=os.getenv("GCP_LOCATION", "us-central1"),
#     )

# GEMINI_MODEL = os.getenv("GAMES_HUB_EXTRACTION_MODEL", "gemini-2.5-flash")
# OUTPUT_DIR = "llm_games_hub_drafts"


# class GenerationError(Exception):
#     pass


# def _slugify(name: str) -> str:
#     return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


# def _unwrap_optional(annotation):
#     origin = typing.get_origin(annotation)
#     if origin is typing.Union:
#         args = [a for a in typing.get_args(annotation) if a is not type(None)]
#         if len(args) == 1:
#             return args[0], True
#     return annotation, False


# def _is_model(tp) -> bool:
#     return isinstance(tp, type) and issubclass(tp, BaseModel)


# def _generic_coerce_scalar(value, target_type):
#     if value is None:
#         return None
#     if target_type is str:
#         if isinstance(value, str):
#             return value
#         if isinstance(value, bool):
#             return str(value)
#         if isinstance(value, (int, float)):
#             return str(value)
#         if isinstance(value, dict):
#             return "; ".join(
#                 f"{k.replace('_', ' ').title()}: {v}" for k, v in value.items() if v is not None
#             ) or None
#         if isinstance(value, list):
#             return ", ".join(str(v) for v in value) or None
#         return str(value)
#     if target_type is int:
#         if isinstance(value, int) and not isinstance(value, bool):
#             return value
#         if isinstance(value, float):
#             return int(value)
#         if isinstance(value, str):
#             match = re.search(r"-?\d+", value)
#             return int(match.group()) if match else None
#         if isinstance(value, list):
#             return len(value)
#         return value
#     if target_type is float:
#         if isinstance(value, (int, float)) and not isinstance(value, bool):
#             return float(value)
#         if isinstance(value, str):
#             match = re.search(r"-?\d+\.?\d*", value)
#             return float(match.group()) if match else None
#         return value
#     return value


# def _coerce_to_model(data, model_cls, path=""):
#     """Same generic coercion engine as generate_athlete_content_llm.py —
#     auto-coerces fields toward the expected type, and backfills required
#     string fields the LLM omitted entirely with 'N/A' rather than letting
#     Pydantic raise a bare 'Field required' error."""
#     if not isinstance(data, dict):
#         return data

#     result = {}
#     for name, field in model_cls.model_fields.items():
#         if name not in data:
#             continue
#         value = data[name]
#         annotation, _ = _unwrap_optional(field.annotation)
#         origin = typing.get_origin(annotation)
#         field_path = f"{path}.{name}" if path else name

#         if origin in (list, typing.List):
#             item_types = typing.get_args(annotation)
#             item_type = item_types[0] if item_types else str
#             if value is None or isinstance(value, str):
#                 value = []
#             elif not isinstance(value, list):
#                 value = [value]
#             if _is_model(item_type):
#                 result[name] = [
#                     _coerce_to_model(item, item_type, path=field_path)
#                     for item in value
#                     if isinstance(item, dict)
#                 ]
#             else:
#                 result[name] = [
#                     _generic_coerce_scalar(v, item_type) if item_type in (str, int, float) else v
#                     for v in value
#                     if v is not None
#                 ]
#             continue

#         if _is_model(annotation):
#             if isinstance(value, str):
#                 value = None
#             result[name] = (
#                 _coerce_to_model(value, annotation, path=field_path) if isinstance(value, dict) else value
#             )
#             continue

#         if annotation in (str, int, float):
#             coerced = _generic_coerce_scalar(value, annotation)
#             if coerced is None and annotation is str and field.is_required():
#                 coerced = "N/A"
#             result[name] = coerced
#             continue

#         # Enum fields (EventStatus) — pass through, Pydantic validates.
#         result[name] = value

#     for name, field in model_cls.model_fields.items():
#         if name in result:
#             continue
#         if not field.is_required():
#             continue
#         annotation, _ = _unwrap_optional(field.annotation)
#         if annotation is str:
#             result[name] = "N/A"

#     return result


# def _sanitize(data: dict, focus_country: str) -> dict:
#     """Forces countryCode="IN" for the focus-country entries whenever
#     country is India, same reasoning/pattern as the athlete pipeline's
#     coreInfo.flag fix — don't trust Gemini's free-text country-code
#     guess for a value the pipeline already knows deterministically."""
#     def _fix(entry):
#         if isinstance(entry, dict) and isinstance(entry.get("country"), str):
#             if entry["country"].strip().lower() == focus_country.strip().lower():
#                 entry["countryCode"] = _COUNTRY_CODE_OVERRIDES.get(focus_country.strip().lower(), entry.get("countryCode"))
#         return entry

#     for e in data.get("medalTable") or []:
#         _fix(e)
#     if isinstance(data.get("focusCountry"), dict):
#         _fix(data["focusCountry"])
#     return data


# _COUNTRY_CODE_OVERRIDES = {"india": "IN"}


# def _extract_grounding_sources(response) -> list[str]:
#     urls: list[str] = []
#     try:
#         candidates = getattr(response, "candidates", None) or []
#         for candidate in candidates:
#             metadata = getattr(candidate, "grounding_metadata", None)
#             if not metadata:
#                 continue
#             chunks = getattr(metadata, "grounding_chunks", None) or []
#             for chunk in chunks:
#                 web = getattr(chunk, "web", None)
#                 uri = getattr(web, "uri", None) if web else None
#                 if uri:
#                     urls.append(uri)
#     except Exception:
#         pass
#     return list(dict.fromkeys(urls))


# def _extract_json(raw_text: str, response_meta=None) -> dict:
#     raw = (raw_text or "").strip()
#     start = raw.find("{")
#     end = raw.rfind("}") + 1
#     if start == -1 or end == 0:
#         diag = ""
#         try:
#             candidates = getattr(response_meta, "candidates", None) or []
#             if candidates:
#                 finish_reason = getattr(candidates[0], "finish_reason", None)
#                 diag = f" [finish_reason={finish_reason}]"
#             else:
#                 diag = " [no candidates returned]"
#         except Exception:
#             pass
#         raise GenerationError(f"Gemini response was empty or contained no JSON (length {len(raw)}){diag}")
#     try:
#         return json.loads(raw[start:end])
#     except json.JSONDecodeError as e:
#         raise GenerationError(f"Gemini response was not valid JSON: {e} [length {len(raw)}]") from e


# def _build_prompt(games_name: str, focus_country: str) -> str:
#     return f"""
# You are drafting a "Games Hub" content document for SportsFan360, a sports fan engagement
# platform. You have access to Google Search — use it to find CURRENT, accurate information
# about this multi-sport games event rather than relying only on what you already know.

# Games: {games_name}
# Focus country: {focus_country}

# Produce a single JSON object with exactly these top-level keys: gamesInfo, medalTable,
# focusCountry, todaysEvents.

# NULL-AVOIDANCE: use null only after genuinely searching and coming up empty. Medal counts
# and event schedules are exactly the kind of fact search grounding should nail down
# confidently — do not default to null or an empty list out of caution.

# gamesInfo: {{name, edition, hostCity, startDate, endDate, status}}. status is exactly one of
# "UPCOMING", "LIVE", "CONCLUDED" based on today's date vs the games' actual dates.

# medalTable — IMPORTANT, ALL-TIME CUMULATIVE, NOT this single edition: this must be each
# country's ALL-TIME TOTAL medal count across EVERY past edition of these games, summed up
# through and including the current edition named above (e.g. for "Asian Games 2026", the sum
# of every Asian Games a country has won medals at, from the very first edition through 2026)
# — NOT just the medals won at this one 2026 edition. Search for the games' all-time /
# historical / cumulative medal standings (often published as "all-time medal table" on
# Wikipedia or the games' official site) and add this edition's medals on top if the
# all-time table you find predates this edition. One entry per country: {{rank (all-time
# cumulative rank), country, countryCode (2-letter ISO code, e.g. "IN", "CN", "JP"), gold,
# silver, bronze, total}} — all four counts are ALL-TIME CUMULATIVE totals. Include every
# country you can find, not just the top few.

# focusCountry: the SAME all-time-cumulative shape as one medalTable entry for
# "{focus_country}" specifically, PLUS previousEditionComparison: {{previousEdition (name of
# the prior edition of these games, e.g. "Hangzhou 2023" for the Asian Games), previousRank
# (the all-time cumulative rank {focus_country} held AS OF that previous edition, before this
# current edition's medals were added), previousTotal (the all-time cumulative medal total as
# of that previous edition), verdict (exactly one of "improved" | "declined" | "same",
# comparing that previous all-time rank to the current all-time rank after this edition),
# summary (one short display string, e.g. "Rank #4 · 107 medals total (all-time, through
# Hangzhou 2023)")}}. Search specifically for {focus_country}'s all-time cumulative standing as
# of the previous edition to fill this — do not leave it null.

# todaysEvents: notable events for {focus_country} happening TODAY (or, if the games haven't
# started yet / have already concluded, the most recent/relevant day's schedule), LIMITED TO
# EXACTLY THESE 5 SPORTS: Track & Field, Boxing, Kabaddi, Cricket, Football — do not include
# events from any other sport, even if {focus_country} is competing in it today. List of
# {{sport, eventName, participant, date, time, status, result}}. sport MUST be exactly one of
# "Track & Field", "Boxing", "Kabaddi", "Cricket", "Football". date MUST be the real ISO date
# (YYYY-MM-DD) this event actually takes/took place — search for the actual competition
# schedule/results for these 5 sports specifically, do not guess or leave it null if the date
# is findable. time is the display time, e.g. "10:00 AM" — null only if genuinely unscheduled
# or unknown. status is exactly one of "LIVE", "UPCOMING", "FINISHED". participant is the
# specific athlete name for individual events (track & field, boxing) or a team/matchup string
# (e.g. "India Men", "IND vs KOR") for team events (kabaddi, cricket, football). result is
# ONLY filled for FINISHED events and should report the MEDAL won if any (e.g. "Gold",
# "Silver", "Bronze") or the match outcome for team sports (e.g. "Won 3-1", "Lost by 5
# wickets") — search specifically for whether {focus_country} medaled in each finished
# track & field / boxing / kabaddi event, since that's the headline fact for this field — null
# for LIVE/UPCOMING. Include as many real events as you can find across these 5 sports for
# {focus_country} on that day — do not pad with invented events, but do not stop at 2-3 if more
# are findable.

# Respond with ONLY a single JSON object containing gamesInfo, medalTable, focusCountry, and
# todaysEvents. No prose, no markdown code fences, no commentary before or after the JSON.
# """


# def _call_gemini(prompt: str):
#     try:
#         return client.models.generate_content(
#             model=GEMINI_MODEL,
#             contents=prompt,
#             config=types.GenerateContentConfig(
#                 temperature=0.2,
#                 max_output_tokens=16384,
#                 tools=[types.Tool(google_search=types.GoogleSearch())],
#             ),
#         )
#     except Exception as e:
#         raise GenerationError(f"Gemini generation call failed: {e}") from e


# def generate_games_hub(games_name: str, focus_country: str = "India"):
#     prompt = _build_prompt(games_name, focus_country)
#     response = _call_gemini(prompt)
#     try:
#         data = _extract_json(response.text, response_meta=response)
#     except GenerationError:
#         response = _call_gemini(prompt)
#         data = _extract_json(response.text, response_meta=response)

#     data = _sanitize(data, focus_country)
#     data = _coerce_to_model(data, GamesHubDocument)

#     data["games_id"] = _slugify(games_name)
#     data["grounding_sources"] = _extract_grounding_sources(response)
#     data["source"] = SourceRef(
#         source_name="Gemini (search-grounded — unverified, needs human review)",
#         source_url="https://sportsfan360.internal/llm-draft",
#         fetched_at=datetime.now(timezone.utc).isoformat(),
#     ).model_dump(mode="json")

#     doc = GamesHubDocument.model_validate(data)
#     return doc, doc.model_dump(mode="json")


# def save_draft_to_dynamodb(doc_dict: dict, games_name: str) -> Optional[str]:
#     """Same review-queue pattern as generate_athlete_content_llm.py's
#     save_draft_to_dynamodb — writes to SportsData as status=pending_review
#     via firebase_store.save_review_draft(). Non-fatal on failure since the
#     local JSON save already succeeded."""
#     draft = {
#         "games_id": doc_dict["games_id"],
#         "games_name": games_name,
#         "trigger_reason": "llm_games_hub_generation",
#         "proposed_data": doc_dict,
#         "status": "pending_review",
#     }
#     try:
#         return firebase_store.save_review_draft(draft)
#     except Exception as e:
#         print(f"⚠️ Failed to save draft to DynamoDB: {e}", file=sys.stderr)
#         return None


# def main():
#     os.makedirs(OUTPUT_DIR, exist_ok=True)
#     while True:
#         games_name = input("\nGames name (e.g. 'Asian Games 2026', blank to quit): ").strip()
#         if not games_name:
#             print("Done.")
#             break
#         focus_country = input("Focus country [India]: ").strip() or "India"

#         print(f"\nGenerating Games Hub draft for {games_name} ({focus_country})...")
#         try:
#             doc, doc_dict = generate_games_hub(games_name, focus_country)
#         except (GenerationError, ValidationError) as e:
#             print(f"FAILED: {e}", file=sys.stderr)
#             continue

#         out_path = os.path.join(OUTPUT_DIR, f"{doc.games_id}.json")
#         with open(out_path, "w") as f:
#             json.dump(doc_dict, f, indent=2, default=str)

#         draft_id = save_draft_to_dynamodb(doc_dict, games_name)
#         if draft_id:
#             print(f"✅ Draft also saved to DynamoDB review queue: draft_id={draft_id}")
#         else:
#             print("⚠️ DynamoDB save failed — local JSON was still saved above.")

#         print(f"Saved: {out_path}")
#         print(json.dumps(doc_dict, indent=2, default=str))


# if __name__ == "__main__":
#     main()






"""
generate_games_hub_llm.py

Standalone content generator for SportsFan360's "Games Hub" screen — the
multi-sport event overview shown for things like the Asian Games /
Commonwealth Games / Olympics: a live MEDAL TABLE (ranked by gold, with a
focus-country card highlighting rank/medals/improvement-vs-previous-edition)
plus a SCHEDULED EVENTS feed (per-sport schedule items with live status,
one card per named individual event/match) plus a phase-level SCHEDULE
BREAKDOWN for the multi-day, multi-session sports (Athletics, Boxing)
where a flat list of individual events either isn't findable yet (games
still weeks out) or is too granular to be useful on its own.

Modeled directly on generate_athlete_content_llm.py's architecture —
Gemini + Google Search grounding, NULL-AVOIDANCE prompting, the same
generic schema-driven coercion engine (copied, not imported, same reason
the athlete/match-center scripts each keep their own copy: avoids a
cross-file import for what's a handful of small pure functions) — but
this is a NEW, separate script/schema, sibling to
generate_athlete_content_llm.py and generate_match_center_llm.py, not an
extension of either.

Output shape (see GamesHubDocument):
  - gamesInfo: identity of the games edition (name, host city, dates, status)
  - medalTable: list[MedalTableEntry], ranked, all participating countries
    Gemini can find standings for
  - focusCountry: MedalTableEntry-shaped highlight PLUS
    previousEditionComparison (rank/medals at the prior edition + a plain
    "improved" | "declined" | "same" verdict) — this is the top red/amber
    card in the reference screenshots
  - scheduledEvents: list[ScheduledEvent] — one entry per notable
    individual event for the focus country (a named athlete's final, a
    specific bout, a specific match), each with a stable eventId,
    sport-specific discipline label, and participants as a list (a
    matchup like ["India Women", "Japan Women"] or a single athlete like
    ["Neeraj Chopra"]) — today only for Kabaddi/Cricket/Football, but
    today PLUS the rest of the games' upcoming schedule for
    Athletics/Boxing
  - scheduleBreakdown: list[ScheduleBreakdownEntry] — a SUPPLEMENT to
    scheduledEvents, scoped to Athletics and Boxing only, giving a
    higher-level phase-by-phase view across date ranges grouped by
    discipline (e.g. Athletics' Track/Field/Road/Combined groupings).
    This is populated whether or not individual-event data is findable —
    it's meant to always give a usable schedule overview for these two
    sports, since their full multi-day session structure is reliably
    published (official site / major sports-news schedule pages) even
    when a per-athlete entry list isn't yet.
  - metadata: source name + fetchedAt timestamp for the draft (plus an
    internal groundingSources list carried through from the Gemini API's
    search-grounding metadata, for review/debugging — not part of the
    Gemini-facing schema)

NOTE: search-grounded LLM output can go stale within hours for a live
multi-day games (medal counts and event status both change constantly).
This script is a DRAFTING aid for the review queue — like the athlete and
match-center scripts — not a live-scores feed. For genuinely real-time
medal counts/event status, a live sports-data API is the right source;
this script is for when no such feed is wired up yet, or for backfilling
historical/completed games editions.

Everything lives in this one file on purpose, matching the pattern of
every other script in this pipeline (generate_athlete_content_llm.py,
generate_match_center_llm.py, generate_records_explorer_llm.py).
"""

import json
import os
import re
import sys
import typing
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from google import genai
from google.genai import types
from pydantic import BaseModel, Field, ValidationError

import firebase_store


# ═══════════════════════════════════════════════════════════════════════
# SCHEMAS
# ═══════════════════════════════════════════════════════════════════════

class EventStatus(str, Enum):
    LIVE = "LIVE"
    UPCOMING = "UPCOMING"
    FINISHED = "FINISHED"


class Metadata(BaseModel):
    source: str = Field(default="Gemini Draft")
    fetchedAt: str  # ISO8601 timestamp, set by the pipeline
    groundingSources: list[str] = Field(
        default_factory=list,
        description="Internal only — URLs the Gemini search-grounding metadata cited, kept for human review/debugging.",
    )
    lowConfidence: bool = Field(
        default=False,
        description=(
            "Internal only. True when the main generation pass returned ZERO grounding "
            "sources even after a retry — a strong signal Gemini answered from its own "
            "(possibly wrong/hallucinated) knowledge instead of actually searching, despite "
            "the search tool being available. Drafts with this flag need extra-careful human "
            "review before publishing — specific facts (athlete names, dates, weight classes, "
            "medal counts) are NOT reliable in that case."
        ),
    )

    model_config = {"extra": "forbid"}


class GamesInfo(BaseModel):
    name: str = Field(..., description="e.g. 'Asian Games 2026'")
    edition: Optional[str] = Field(None, description="e.g. '20th Asian Games'")
    hostCity: Optional[str] = Field(None, description="e.g. 'Aichi-Nagoya, Japan'")
    startDate: Optional[str] = Field(None, description="ISO date YYYY-MM-DD if known")
    endDate: Optional[str] = Field(None, description="ISO date YYYY-MM-DD if known")
    status: Optional[str] = Field(None, description="'UPCOMING' | 'LIVE' | 'CONCLUDED'")

    model_config = {"extra": "forbid"}


class MedalTableEntry(BaseModel):
    """
    ALL-TIME cumulative medal counts for this country across every past
    edition of these games UP THROUGH AND INCLUDING the current edition
    (e.g. for "Asian Games 2026", the sum of every Asian Games a country
    has ever competed in, 1951 through 2026) — NOT just the medals won at
    the single edition named in gamesInfo. See _build_prompt's
    ALL-TIME-CUMULATIVE instruction for how this is sourced.
    """
    rank: Optional[int] = Field(None, description="all-time cumulative rank, not this single edition's rank")
    country: str
    countryCode: Optional[str] = Field(None, description="2-letter ISO country code, e.g. 'IN', 'CN', 'JP'")
    gold: int = 0
    silver: int = 0
    bronze: int = 0
    total: int = 0

    model_config = {"extra": "forbid"}


class PreviousEditionComparison(BaseModel):
    """
    Compares the focus country's ALL-TIME cumulative rank/total as of the
    PREVIOUS edition (i.e. cumulative through previousEdition, not that
    edition's own single-games tally) against its all-time cumulative
    rank/total now, through the current edition named in gamesInfo.
    """
    previousEdition: Optional[str] = Field(None, description="e.g. 'Hangzhou 2023'")
    previousRank: Optional[int] = Field(None, description="all-time cumulative rank through previousEdition")
    previousTotal: Optional[int] = Field(None, description="all-time cumulative medal total through previousEdition")
    verdict: Optional[str] = Field(None, description="exactly one of: 'improved', 'declined', 'same'")
    summary: Optional[str] = Field(None, description="e.g. 'Rank #4 · 107 medals total (all-time, through Hangzhou 2023)'")

    model_config = {"extra": "forbid"}


class FocusCountryCard(MedalTableEntry):
    previousEditionComparison: Optional[PreviousEditionComparison] = None

    model_config = {"extra": "forbid"}


class ScheduledEvent(BaseModel):
    """
    One named, individual event/match for the focus country. eventId is
    NOT set by Gemini — it's assigned deterministically by the pipeline
    post-generation (see _assign_event_ids), so it stays stable/sortable
    (EVT-<year>-<sportcode>-<seq>) regardless of what order Gemini
    returns entries in.
    """
    eventId: Optional[str] = Field(None, description="assigned by the pipeline, not by Gemini — leave null")
    sport: str = Field(..., description="exactly one of: 'Athletics', 'Boxing', 'Kabaddi', 'Cricket', 'Football'")
    discipline: str = Field(
        ...,
        description=(
            "Short free-text sub-category label, specific to the sport. Examples: Cricket → "
            "'T20'/'ODI'/'T20I'; Kabaddi → 'Standard Style'; Athletics → 'Track - Sprints' / "
            "'Track - Middle Distance' / 'Track - Long Distance' / 'Track - Relays' / "
            "'Field - Jumps' / 'Field - Throws' / 'Road - Marathon' / 'Road - Race Walking' "
            "/ 'Combined - Decathlon' / 'Combined - Heptathlon'; Boxing → the specific weight "
            "class, e.g. \"Men's Light Heavyweight (80kg)\"; Football → 'Men's Football' / "
            "'Women's Football'."
        ),
    )
    eventName: str = Field(..., description="e.g. 'Men's Javelin Throw Final', 'Women's 100m Final', 'Men's 80kg Preliminary', 'IND vs KOR Quarter-Final'")
    participants: list[str] = Field(
        default_factory=list,
        description="1-2 entries: a single athlete name for individual events (e.g. ['Neeraj Chopra']), or a two-team matchup for team events/matches (e.g. ['India Women', 'Japan Women']). Use ['TBD'] only if the specific athlete/team is genuinely not yet determined.",
    )
    scheduledDate: Optional[str] = Field(None, description="ISO date YYYY-MM-DD this event takes/took/will take place")
    scheduledTime: Optional[str] = Field(None, description="local time with UTC offset, e.g. '17:30:00+09:00' (host city's timezone) — null only if genuinely unscheduled/unknown")
    status: EventStatus
    result: Optional[str] = Field(
        None, description="only for FINISHED events — medal/outcome summary, e.g. 'Gold', 'Won 3-1' — null for LIVE/UPCOMING"
    )

    model_config = {"extra": "forbid"}


class ScheduleBreakdownEntry(BaseModel):
    """
    A phase-level view of a multi-day, multi-session sport's schedule,
    scoped to Athletics and Boxing only. Supplements scheduledEvents
    (which carries specific named events/athletes) with a coarser,
    always-available breakdown by date range, grouped by discipline
    category — e.g. Athletics' Track/Field/Road/Combined groupings, each
    spanning its own date range and phase.
    """
    sport: str = Field(..., description="exactly one of: 'Athletics', 'Boxing'")
    discipline: str = Field(
        ...,
        description=(
            "Discipline GROUP this phase belongs to (coarser than ScheduledEvent.discipline). "
            "For Athletics: 'Track Events', 'Field Events', 'Road Events', 'Combined Events'. "
            "For Boxing: a single group is fine, e.g. 'Amateur Boxing'."
        ),
    )
    dateRange: str = Field(..., description="e.g. 'Sept 23 – Sept 27' or a single date 'Oct 4'")
    phase: str = Field(..., description="short phase label, e.g. 'Sprints, Distance & Relays', 'Jumps & Throws', 'Marathon & Race Walking', 'Decathlon & Heptathlon', 'Preliminary to Finals'")
    highlights: str = Field(..., description="one sentence naming the specific events/rounds in this phase, e.g. 'Qualifications and finals for High Jump, Pole Vault, Long Jump, Triple Jump, Shot Put, Discus, Hammer, and Javelin.'")

    model_config = {"extra": "forbid"}


class GamesHubDocument(BaseModel):
    games_id: str
    gamesInfo: GamesInfo
    medalTable: list[MedalTableEntry] = Field(default_factory=list)
    focusCountry: Optional[FocusCountryCard] = None
    scheduledEvents: list[ScheduledEvent] = Field(default_factory=list)
    scheduleBreakdown: list[ScheduleBreakdownEntry] = Field(default_factory=list)
    metadata: Metadata

    model_config = {"extra": "forbid"}


# ═══════════════════════════════════════════════════════════════════════
# GENERATION PIPELINE
# ═══════════════════════════════════════════════════════════════════════

api_key = os.getenv("GEMINI_API_KEY")
if api_key:
    client = genai.Client(api_key=api_key)
else:
    client = genai.Client(
        vertexai=True,
        project=os.getenv("GCP_PROJECT_ID", "fleet-gift-498306-p7"),
        location=os.getenv("GCP_LOCATION", "us-central1"),
    )

GEMINI_MODEL = os.getenv("GAMES_HUB_EXTRACTION_MODEL", "gemini-2.5-flash")
OUTPUT_DIR = "llm_games_hub_drafts"

# sport -> short code used in generated eventIds, e.g. EVT-2026-ATH-001
_SPORT_CODES = {
    "Cricket": "CRIC",
    "Kabaddi": "KABD",
    "Athletics": "ATH",
    "Boxing": "BOX",
    "Football": "FOOT",
}


class GenerationError(Exception):
    pass


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def _unwrap_optional(annotation):
    origin = typing.get_origin(annotation)
    if origin is typing.Union:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
    return annotation, False


def _is_model(tp) -> bool:
    return isinstance(tp, type) and issubclass(tp, BaseModel)


def _generic_coerce_scalar(value, target_type):
    if value is None:
        return None
    if target_type is str:
        if isinstance(value, str):
            return value
        if isinstance(value, bool):
            return str(value)
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, dict):
            return "; ".join(
                f"{k.replace('_', ' ').title()}: {v}" for k, v in value.items() if v is not None
            ) or None
        if isinstance(value, list):
            return ", ".join(str(v) for v in value) or None
        return str(value)
    if target_type is int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            match = re.search(r"-?\d+", value)
            return int(match.group()) if match else None
        if isinstance(value, list):
            return len(value)
        return value
    if target_type is float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            match = re.search(r"-?\d+\.?\d*", value)
            return float(match.group()) if match else None
        return value
    return value


def _coerce_to_model(data, model_cls, path=""):
    """Same generic coercion engine as generate_athlete_content_llm.py —
    auto-coerces fields toward the expected type, and backfills required
    string fields the LLM omitted entirely with 'N/A' rather than letting
    Pydantic raise a bare 'Field required' error."""
    if not isinstance(data, dict):
        return data

    result = {}
    for name, field in model_cls.model_fields.items():
        if name not in data:
            continue
        value = data[name]
        annotation, _ = _unwrap_optional(field.annotation)
        origin = typing.get_origin(annotation)
        field_path = f"{path}.{name}" if path else name

        if origin in (list, typing.List):
            item_types = typing.get_args(annotation)
            item_type = item_types[0] if item_types else str
            if value is None or isinstance(value, str):
                value = []
            elif not isinstance(value, list):
                value = [value]
            if _is_model(item_type):
                result[name] = [
                    _coerce_to_model(item, item_type, path=field_path)
                    for item in value
                    if isinstance(item, dict)
                ]
            else:
                result[name] = [
                    _generic_coerce_scalar(v, item_type) if item_type in (str, int, float) else v
                    for v in value
                    if v is not None
                ]
            continue

        if _is_model(annotation):
            if isinstance(value, str):
                value = None
            result[name] = (
                _coerce_to_model(value, annotation, path=field_path) if isinstance(value, dict) else value
            )
            continue

        if annotation in (str, int, float):
            coerced = _generic_coerce_scalar(value, annotation)
            if coerced is None and annotation is str and field.is_required():
                coerced = "N/A"
            result[name] = coerced
            continue

        # Enum fields (EventStatus) — pass through, Pydantic validates.
        result[name] = value

    for name, field in model_cls.model_fields.items():
        if name in result:
            continue
        if not field.is_required():
            continue
        annotation, _ = _unwrap_optional(field.annotation)
        if annotation is str:
            result[name] = "N/A"

    return result


def _sanitize(data: dict, focus_country: str) -> dict:
    """Forces countryCode="IN" for the focus-country entries whenever
    country is India, same reasoning/pattern as the athlete pipeline's
    coreInfo.flag fix — don't trust Gemini's free-text country-code
    guess for a value the pipeline already knows deterministically."""
    def _fix(entry):
        if isinstance(entry, dict) and isinstance(entry.get("country"), str):
            if entry["country"].strip().lower() == focus_country.strip().lower():
                entry["countryCode"] = _COUNTRY_CODE_OVERRIDES.get(focus_country.strip().lower(), entry.get("countryCode"))
        return entry

    for e in data.get("medalTable") or []:
        _fix(e)
    if isinstance(data.get("focusCountry"), dict):
        _fix(data["focusCountry"])
    return data


_COUNTRY_CODE_OVERRIDES = {"india": "IN"}

# Real Olympic/Asian-Games-style amateur boxing weight classes (2021+
# World Boxing / IBA cutoffs). Gemini has repeatedly fabricated
# plausible-but-wrong classes (e.g. "Men's 55kg", "Men's 90kg" — not
# real cutoffs) instead of these actual ones, so any Boxing discipline
# value is checked against this list and flagged (not silently trusted)
# if it doesn't match.
_STANDARD_BOXING_WEIGHTS_MEN = ["51", "57", "63.5", "71", "80", "92", "+92"]
_STANDARD_BOXING_WEIGHTS_WOMEN = ["50", "54", "57", "60", "66", "75", "+75"]


def _flag_nonstandard_boxing_classes(data: dict) -> None:
    """Mutates scheduledEvents in place: for every Boxing entry, checks
    whether the weight figure inside `discipline` matches a real
    standard weight class. If not, prepends a "[UNVERIFIED WEIGHT CLASS]"
    marker so a reviewer can immediately see it needs a manual check,
    rather than trusting a fabricated-looking figure like '55kg' or
    '90kg' at face value."""
    valid = set(_STANDARD_BOXING_WEIGHTS_MEN) | set(_STANDARD_BOXING_WEIGHTS_WOMEN)
    for event in data.get("scheduledEvents") or []:
        if not isinstance(event, dict) or event.get("sport") != "Boxing":
            continue
        discipline = event.get("discipline") or ""
        match = re.search(r"\+?\d+(?:\.\d+)?(?=\s*kg)", discipline)
        weight = match.group() if match else None
        if weight is None or weight not in valid:
            if not discipline.startswith("[UNVERIFIED WEIGHT CLASS]"):
                event["discipline"] = f"[UNVERIFIED WEIGHT CLASS] {discipline}".strip()


def _assign_event_ids(data: dict, games_name: str) -> dict:
    """Assigns stable eventIds (EVT-<year>-<sportcode>-<seq>) to every
    scheduledEvents entry, in whatever order they currently sit in.
    Deliberately done in the pipeline rather than by Gemini, so IDs stay
    consistent/sortable regardless of what order the model returns
    entries in, and so retry-pass entries slot in without collisions."""
    year_match = re.search(r"(19|20)\d{2}", games_name)
    year = year_match.group() if year_match else str(datetime.now(timezone.utc).year)

    counters: dict[str, int] = {}
    for event in data.get("scheduledEvents") or []:
        if not isinstance(event, dict):
            continue
        sport = event.get("sport")
        code = _SPORT_CODES.get(sport, re.sub(r"[^A-Z]", "", (sport or "GEN").upper())[:4] or "GEN")
        counters[code] = counters.get(code, 0) + 1
        event["eventId"] = f"EVT-{year}-{code}-{counters[code]:03d}"
    return data


def _extract_grounding_sources(response) -> list[str]:
    urls: list[str] = []
    try:
        candidates = getattr(response, "candidates", None) or []
        for candidate in candidates:
            metadata = getattr(candidate, "grounding_metadata", None)
            if not metadata:
                continue
            chunks = getattr(metadata, "grounding_chunks", None) or []
            for chunk in chunks:
                web = getattr(chunk, "web", None)
                uri = getattr(web, "uri", None) if web else None
                if uri:
                    urls.append(uri)
    except Exception:
        pass
    return list(dict.fromkeys(urls))


def _extract_json(raw_text: str, response_meta=None) -> dict:
    raw = (raw_text or "").strip()
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start == -1 or end == 0:
        diag = ""
        try:
            candidates = getattr(response_meta, "candidates", None) or []
            if candidates:
                finish_reason = getattr(candidates[0], "finish_reason", None)
                diag = f" [finish_reason={finish_reason}]"
            else:
                diag = " [no candidates returned]"
        except Exception:
            pass
        raise GenerationError(f"Gemini response was empty or contained no JSON (length {len(raw)}){diag}")
    try:
        return json.loads(raw[start:end])
    except json.JSONDecodeError as e:
        raise GenerationError(f"Gemini response was not valid JSON: {e} [length {len(raw)}]") from e


def _build_prompt(games_name: str, focus_country: str) -> str:
    return f"""
You are drafting a "Games Hub" content document for SportsFan360, a sports fan engagement
platform. You have access to Google Search — you MUST actually issue search queries for the
specific facts below (medal standings, schedules, rankings) rather than answering from memory
alone; this document is explicitly marked as search-grounded, so ungrounded answers undermine
its purpose even if the facts happen to be correct.

Games: {games_name}
Focus country: {focus_country}

ACCURACY IS MORE IMPORTANT THAN COMPLETENESS. Do not guess a plausible-sounding athlete name,
date, weight class, or medal count if you have not actually found it via search. A specific
wrong fact (a made-up athlete, a fabricated boxing weight class, an invented date) is worse
than omitting that entry entirely — omitted entries can be filled in later by a human
reviewer, but a wrong fact presented with confidence can get published as-is. If, after
actually searching, you are not confident a specific detail is real, leave that field null or
omit that entry rather than inventing something reasonable-sounding. Boxing weight classes in
particular MUST match real amateur boxing cutoffs — men's: 51kg, 57kg, 63.5kg, 71kg, 80kg,
92kg, +92kg; women's: 50kg, 54kg, 57kg, 60kg, 66kg, 75kg, +75kg — never invent a class like
"55kg" or "90kg" that isn't one of these.

Produce a single JSON object with exactly these top-level keys: gamesInfo, medalTable,
focusCountry, scheduledEvents, scheduleBreakdown. Do NOT include an eventId field anywhere —
that is assigned by the pipeline afterward, not by you.

NULL-AVOIDANCE: use null only after genuinely searching and coming up empty. Medal counts
and event schedules are exactly the kind of fact search grounding should nail down
confidently — do not default to null or an empty list out of caution.

gamesInfo: {{name, edition, hostCity, startDate, endDate, status}}. status is exactly one of
"UPCOMING", "LIVE", "CONCLUDED" based on today's date vs the games' actual dates.

medalTable — IMPORTANT, ALL-TIME CUMULATIVE, NOT this single edition: this must be each
country's ALL-TIME TOTAL medal count across EVERY past edition of these games, summed up
through and including the current edition named above (e.g. for "Asian Games 2026", the sum
of every Asian Games a country has won medals at, from the very first edition through 2026)
— NOT just the medals won at this one 2026 edition. Search for the games' all-time /
historical / cumulative medal standings (often published as "all-time medal table" on
Wikipedia or the games' official site) and add this edition's medals on top if the
all-time table you find predates this edition. One entry per country: {{rank (all-time
cumulative rank), country, countryCode (exactly 2 letters, ISO 3166-1 ALPHA-2 — "IN" not
"IND", "CN" not "CHN", "JP" not "JPN", "KR" not "KOR" — NEVER the 3-letter Olympic/IOC-style
code), gold, silver, bronze, total}} — all four counts are ALL-TIME CUMULATIVE totals.
Include every country you can find, not just the top few.

focusCountry: the SAME all-time-cumulative shape as one medalTable entry for
"{focus_country}" specifically, PLUS previousEditionComparison: {{previousEdition (name of
the prior edition of these games, e.g. "Hangzhou 2023" for the Asian Games), previousRank
(the all-time cumulative rank {focus_country} held AS OF that previous edition, before this
current edition's medals were added), previousTotal (the all-time cumulative medal total as
of that previous edition), verdict (exactly one of "improved" | "declined" | "same",
comparing that previous all-time rank to the current all-time rank after this edition),
summary (one short display string, e.g. "Rank #4 · 107 medals total (all-time, through
Hangzhou 2023)")}}. Search specifically for {focus_country}'s all-time cumulative standing as
of the previous edition to fill this — do not leave it null.

scheduledEvents: notable INDIVIDUAL events/matches for {focus_country}, LIMITED TO EXACTLY
THESE 5 SPORTS: Athletics, Boxing, Kabaddi, Cricket, Football — do not include events from any
other sport, even if {focus_country} is competing in it today. Do NOT use vague placeholder
rows like "Athletics events begin" or "Boxing events begin" — if you can't find specific
named events/athletes for Athletics or Boxing, leave scheduledEvents to cover only the sports
you DO have specific events for, and rely on scheduleBreakdown (below) to cover
Athletics/Boxing at the phase level instead.

ATHLETICS SPECIFICALLY — this needs the SAME quality of individual, named entries you'd
produce for Boxing (e.g. "Men's 90kg Preliminary" · "Kapil"), not just a phase summary.
Actively search for {focus_country}'s specific entered/qualified athletes and their specific
scheduled events across all Athletics sub-categories — sprints (100m/200m/400m), middle
distance (800m/1500m/3000m Steeplechase), long distance (5000m/10000m/Marathon/Race Walk),
relays (4x100m/4x400m), jumps (Long Jump/Triple Jump/High Jump/Pole Vault), throws (Shot
Put/Discus/Hammer/Javelin) — and set each entry's discipline field to the matching "Track -
..." / "Field - ..." / "Road - ..." / "Combined - ..." label (see schema below). Search
queries like "{focus_country} athletics squad {games_name}", "{focus_country} athletics
entry list {games_name}", or the specific athlete names of known {focus_country} Athletics
competitors plus "{games_name} schedule" will surface named heats/finals — use them the same
way you already do for Boxing. Only fall back to leaving Athletics out of scheduledEvents
(relying on scheduleBreakdown alone) if named athletes/events are genuinely not findable
after actually searching for them.

IF the games' status is "LIVE" (already underway): for Kabaddi, Cricket, and Football, cover
TODAY's schedule only. For Athletics and Boxing, ALSO include the athlete's/team's UPCOMING
scheduled events across the REST OF THE GAMES' remaining duration (heats, qualification
rounds, semifinals, finals still to come), not just today.

IF the games' status is "UPCOMING" (has not started yet) or "CONCLUDED" (already over): do
NOT return an empty list just because there is no literal "today" within the games' dates.
Instead search for and return the EARLIEST FEW DAYS of {focus_country}'s published/announced
schedule across all 5 sports for an UPCOMING games (heats, qualifiers, opening fixtures —
status "UPCOMING" for every entry, since none of it has happened yet), or the FINAL DAYS'
results for a CONCLUDED games (status "FINISHED" for each, with the medal/result filled in).
Official schedules/entry lists/fixtures for a major games are typically published weeks or
months ahead of the opening ceremony and are findable via search even before the games start
— treat "the schedule isn't public yet" as requiring an actual failed search attempt, not an
assumption.

List of {{sport, discipline, eventName, participants, scheduledDate, scheduledTime, status,
result}}. sport MUST be exactly one of "Athletics", "Boxing", "Kabaddi", "Cricket",
"Football". discipline is a short free-text sub-category label specific to the sport — for
Cricket use the format ("T20"/"ODI"/"T20I"), for Kabaddi use "Standard Style", for Athletics
use one of "Track - Sprints" / "Track - Middle Distance" / "Track - Long Distance" / "Track -
Relays" / "Field - Jumps" / "Field - Throws" / "Road - Marathon" / "Road - Race Walking" /
"Combined - Decathlon" / "Combined - Heptathlon", for Boxing use the specific weight class
(e.g. "Men's Light Heavyweight (80kg)"), for Football use "Men's Football"/"Women's
Football". participants is a list of 1-2 strings: a single athlete name for individual events
(e.g. ["Neeraj Chopra"]), or a two-entry team/country matchup for team events (e.g. ["India
Women", "Japan Women"]) — use ["TBD"] only if genuinely undetermined yet, do not leave it
empty. scheduledDate MUST be the real ISO date (YYYY-MM-DD) this event actually takes/took
place or is scheduled for. scheduledTime is the LOCAL time at the host city WITH a UTC offset
in the format "HH:MM:SS+HH:MM" (e.g. "17:30:00+09:00" for Japan) — null only if genuinely
unscheduled or unknown, search for the host city's UTC offset if needed to construct this.
status is exactly one of "LIVE", "UPCOMING", "FINISHED". result is ONLY filled for FINISHED
events and should report the MEDAL won if any (e.g. "Gold", "Silver", "Bronze") or the match
outcome for team sports (e.g. "Won 3-1", "Lost by 5 wickets") — null for LIVE/UPCOMING.

scheduleBreakdown — ALWAYS populate this for Athletics and Boxing specifically (these are the
two multi-day, multi-session sports where a phase-level view is more useful than a flat event
list, and where the full session schedule is reliably published even before individual
athlete entry lists are). Search for "{games_name} Athletics schedule" and "{games_name}
Boxing schedule". Group Athletics into its natural discipline groups — "Track Events", "Field
Events", "Road Events", "Combined Events" — and Boxing into a single "Amateur Boxing" group
(or split further only if the real schedule naturally does), each spanning its own date range
at these games, with:
  {{sport (exactly "Athletics" or "Boxing"), discipline (the group name above), dateRange
  (e.g. "Sept 23 – Oct 2" or a single date), phase (a short label, e.g. "Sprints, Distance &
  Relays", "Jumps & Throws", "Marathon & Race Walking", "Decathlon & Heptathlon",
  "Preliminary to Finals"), highlights (one sentence naming the actual events/rounds/finals
  happening in that phase/group)}}.
CRITICAL for highlights: name the SPECIFIC individual events/disciplines happening in that
phase — not a generic phrase like "field event finals" or "various disciplines". For example,
Track Events → "100m, 200m, 400m, hurdles, steeplechase heats and finals, culminating in the
4x100m & 4x400m relay finals."; Field Events → "Qualifications and finals for High Jump, Pole
Vault, Long Jump, Triple Jump, Shot Put, Discus, Hammer, and Javelin."; Road Events → "20km &
35km Race Walking finals and Men's & Women's Marathon finals."; Combined Events → "Men's
Decathlon (10 events across 2 days) and Women's Heptathlon (7 events across 2 days)." For
Boxing, name the actual weight categories/round stages present in that phase, or a summary
like "Preliminary rounds leading to semi-finals and gold medal finals" with real dates for
each stage. Search results for the sport's session-by-session schedule (often a PDF or table
on the official site) will have this level of detail — use it; do not fall back to a vague
one-line summary if the specific event names are findable.
Use as many phase/group entries as the real schedule naturally breaks into (typically 3-6 per
sport) — do not force an arbitrary count, and do not invent phases if the sport genuinely
isn't part of this games' program (omit it entirely in that case). This is independent of and
supplements scheduledEvents — populate both where you can, but scheduleBreakdown should not
be left empty for Athletics/Boxing just because scheduledEvents lacks named individual events.

Respond with ONLY a single JSON object containing gamesInfo, medalTable, focusCountry,
scheduledEvents, and scheduleBreakdown. No prose, no markdown code fences, no commentary
before or after the JSON.
"""


_ALL_TARGET_SPORTS = ["Athletics", "Boxing", "Kabaddi", "Cricket", "Football"]

_BREAKDOWN_SPORTS = ["Athletics", "Boxing"]


def _build_missing_sports_prompt(games_name: str, focus_country: str, missing_sports: list[str], games_status: str | None) -> str:
    sports_list = ", ".join(missing_sports)
    naming_note = ""
    if "Athletics" in missing_sports:
        anchor_hint = ""
        if focus_country.strip().lower() == "india":
            anchor_hint = (
                " As a concrete example of the specificity expected: India's Javelin Throw is "
                "genuinely represented by Neeraj Chopra and Kishore Jena — an entry like "
                "{\"sport\": \"Athletics\", \"discipline\": \"Field - Throws\", \"eventName\": "
                "\"Men's Javelin Throw Qualification\", \"participants\": [\"Neeraj Chopra\", "
                "\"Kishore Jena\"]} is exactly the kind of REAL, verifiable entry this needs — "
                "not a vague summary and not an invented name. Apply the same standard "
                "(genuinely well-known, currently-competing athletes you are confident about, "
                "verified via search) across every Athletics sub-category, not just throws."
            )
        naming_note = (
            " Search for named, individual entries across Athletics' sub-categories — "
            "sprints, middle distance, long distance, relays, jumps, throws — the same "
            "specificity you would give Boxing (named athlete + named event, not a vague "
            "summary), and set each entry's discipline field to the matching \"Track - ...\" "
            "/ \"Field - ...\" / \"Road - ...\" / \"Combined - ...\" label."
            f"{anchor_hint} Do not return zero Athletics entries just because this is harder "
            "than the other sports — search harder (athlete-name-specific queries, national "
            "federation squad announcements, past-championship medalists likely to compete "
            "again) before concluding nothing is findable."
        )
    status_hint = ""
    if games_status == "UPCOMING":
        status_hint = (
            " Since these games haven't started yet, this means the earliest few days of "
            f"{focus_country}'s published schedule in {sports_list} — status \"UPCOMING\" for "
            "every entry."
        )
    elif games_status == "CONCLUDED":
        status_hint = (
            f" Since these games have already concluded, this means {focus_country}'s final "
            f"results in {sports_list} — status \"FINISHED\" with the medal/result filled in."
        )
    return f"""
You are drafting the events feed for a "Games Hub" content document for SportsFan360. You have
access to Google Search — you MUST actually issue search queries for this, not answer from
memory.

Games: {games_name}
Focus country: {focus_country}

A previous pass produced NO events at all for these specific sports: {sports_list}. This is
almost certainly a gap, not a genuine absence — {focus_country} very likely has scheduled or
completed events in {sports_list} at these games. Search specifically for
"{focus_country} {games_name} {sports_list} schedule" and similar queries, and return
everything findable. Only include SPECIFIC named events/athletes here — do not invent a vague
placeholder row just to fill the list.{status_hint}{naming_note}

Respond with ONLY a JSON object with a single key "scheduledEvents", a list of
{{sport, discipline, eventName, participants, scheduledDate, scheduledTime, status, result}}
— sport MUST be exactly one of {sports_list} (nothing else). discipline is a short free-text
sub-category label appropriate for the sport (see the main prompt's per-sport format).
participants is a list of 1-2 strings (athlete name, or a 2-team matchup) — use ["TBD"] only
if genuinely undetermined. scheduledDate is ISO YYYY-MM-DD. scheduledTime is local host-city
time with UTC offset, e.g. "17:30:00+09:00" — null if genuinely unknown. status is exactly
one of "LIVE", "UPCOMING", "FINISHED". result is only filled for FINISHED events (the medal
won, e.g. "Gold", or match outcome for team sports) — null otherwise. Do NOT include an
eventId field. If, after genuinely searching, truly nothing specific is findable for a given
sport in this list, omit it rather than inventing events — but do not return an entirely
empty list without having actually searched.

No prose, no markdown code fences, no commentary — just the JSON object.
"""


def _build_schedule_breakdown_prompt(games_name: str, sports: list[str]) -> str:
    sports_list = ", ".join(sports)
    return f"""
You are drafting a phase-level schedule breakdown for a "Games Hub" content document for
SportsFan360. You have access to Google Search — you MUST actually issue search queries, not
answer from memory.

Games: {games_name}

A previous pass produced NO schedule breakdown for: {sports_list}, OR produced one with only
vague, generic highlights (e.g. "field event finals", "various disciplines") instead of
naming the specific events. Search for "{games_name} {sports_list} schedule" and similar
queries. Group Athletics into its natural discipline groups — "Track Events", "Field Events",
"Road Events", "Combined Events" — and Boxing into "Amateur Boxing" (or split further only if
the real schedule naturally does), each spanning its own date range at these games (typically
3-6 phase/group entries total per sport).

CRITICAL for highlights: name the SPECIFIC individual events/disciplines in that phase/group
— e.g. Track Events → specific sprint/middle-distance/long-distance/hurdles/relay events;
Field Events → specific jump/throw events; Road Events → Marathon/Race Walk with real dates;
Combined Events → Decathlon/Heptathlon with real dates. For Boxing, name the actual weight
categories/round stages present. Search results for the sport's session-by-session schedule
will have this detail — use it.

Respond with ONLY a JSON object with a single key "scheduleBreakdown", a list of
{{sport, discipline, dateRange, phase, highlights}} — sport MUST be exactly one of
{sports_list}. discipline is the group name (e.g. "Track Events", "Field Events", "Road
Events", "Combined Events", "Amateur Boxing"). dateRange is a display string like "Sept 23 –
Oct 2" or a single date. phase is a short label. highlights is one sentence naming the actual
specific events/rounds/finals in that phase/group. If a sport genuinely isn't part of this
games' program, omit it entirely rather than inventing phases.

No prose, no markdown code fences, no commentary — just the JSON object.
"""


def _call_gemini(prompt: str):
    try:
        return client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=16384,
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )
    except Exception as e:
        raise GenerationError(f"Gemini generation call failed: {e}") from e


def generate_games_hub(games_name: str, focus_country: str = "India", max_passes: int = 4):
    prompt = _build_prompt(games_name, focus_country)
    response = _call_gemini(prompt)
    try:
        data = _extract_json(response.text, response_meta=response)
    except GenerationError:
        response = _call_gemini(prompt)
        data = _extract_json(response.text, response_meta=response)

    data = _sanitize(data, focus_country)
    data = _coerce_to_model(data, GamesHubDocument)

    data["games_id"] = _slugify(games_name)
    grounding_sources = _extract_grounding_sources(response)

    # CRITICAL: the search tool being available does NOT force Gemini to
    # actually call it — the model can (and does) answer from its own
    # knowledge instead, which for a future/niche games program means
    # confidently wrong specifics (invented athlete names, non-standard
    # weight classes, wrong dates) dressed up as "search-grounded". Zero
    # grounding sources on the main pass is a strong signal that
    # happened. Retry the whole main pass once before giving up — if it
    # STILL comes back empty, proceed but flag the draft as low
    # confidence (see Metadata.lowConfidence) instead of silently
    # treating it as reliable.
    if not grounding_sources:
        print("⚠️ Main generation pass returned zero grounding sources — retrying once...", file=sys.stderr)
        try:
            retry_main_response = _call_gemini(prompt)
            retry_main_data = _extract_json(retry_main_response.text, response_meta=retry_main_response)
            retry_grounding = _extract_grounding_sources(retry_main_response)
            if retry_grounding:
                # The retry actually grounded -- use its result instead.
                data = _sanitize(retry_main_data, focus_country)
                data = _coerce_to_model(data, GamesHubDocument)
                data["games_id"] = _slugify(games_name)
                grounding_sources = retry_grounding
            else:
                print("⚠️ Retry also returned zero grounding sources — proceeding with lowConfidence=True", file=sys.stderr)
        except GenerationError as e:
            print(f"⚠️ Grounding retry failed ({e}) — proceeding with lowConfidence=True", file=sys.stderr)

    # Targeted retry: the model consistently produces good coverage for
    # some of the 5 target sports while silently skipping others
    # entirely (e.g. Cricket/Kabaddi present, Athletics/Boxing
    # completely absent) rather than genuinely finding nothing for
    # them. Re-prompt specifically for whichever sports have zero
    # entries in scheduledEvents, up to max_passes - 1 extra attempts,
    # and merge in anything found. Sports that already have at least
    # one entry are left alone -- this only fills genuine gaps.
    games_status = ((data.get("gamesInfo") or {}).get("status"))
    for _pass in range(max_passes - 1):
        present_sports = {e.get("sport") for e in (data.get("scheduledEvents") or []) if isinstance(e, dict)}
        missing_sports = [s for s in _ALL_TARGET_SPORTS if s not in present_sports]
        if not missing_sports:
            break

        retry_prompt = _build_missing_sports_prompt(games_name, focus_country, missing_sports, games_status)
        try:
            retry_response = _call_gemini(retry_prompt)
            retry_data = _extract_json(retry_response.text, response_meta=retry_response)
        except GenerationError as e:
            print(f"Games Hub missing-sports retry failed: {e}", file=sys.stderr)
            break

        new_events = retry_data.get("scheduledEvents")
        if not isinstance(new_events, list) or not new_events:
            # Retry genuinely found nothing new -- stop rather than
            # looping on an empty result.
            break

        coerced_events = _coerce_to_model({"scheduledEvents": new_events}, GamesHubDocument).get("scheduledEvents") or []
        # Only keep entries whose sport was actually one of the ones we
        # asked for this pass, in case the model wanders.
        coerced_events = [e for e in coerced_events if isinstance(e, dict) and e.get("sport") in missing_sports]
        if not coerced_events:
            break

        data.setdefault("scheduledEvents", [])
        data["scheduledEvents"].extend(coerced_events)
        grounding_sources.extend(_extract_grounding_sources(retry_response))

    # Same targeted-retry pattern for scheduleBreakdown, scoped to
    # Athletics / Boxing only -- these two should essentially always
    # end up with a phase breakdown even if scheduledEvents can't find
    # named individual events for them.
    for _pass in range(max_passes - 1):
        present_breakdown_sports = {
            e.get("sport") for e in (data.get("scheduleBreakdown") or []) if isinstance(e, dict)
        }
        missing_breakdown_sports = [s for s in _BREAKDOWN_SPORTS if s not in present_breakdown_sports]
        if not missing_breakdown_sports:
            break

        retry_prompt = _build_schedule_breakdown_prompt(games_name, missing_breakdown_sports)
        try:
            retry_response = _call_gemini(retry_prompt)
            retry_data = _extract_json(retry_response.text, response_meta=retry_response)
        except GenerationError as e:
            print(f"Games Hub schedule-breakdown retry failed: {e}", file=sys.stderr)
            break

        new_breakdown = retry_data.get("scheduleBreakdown")
        if not isinstance(new_breakdown, list) or not new_breakdown:
            break

        coerced_breakdown = _coerce_to_model(
            {"scheduleBreakdown": new_breakdown}, GamesHubDocument
        ).get("scheduleBreakdown") or []
        coerced_breakdown = [
            e for e in coerced_breakdown if isinstance(e, dict) and e.get("sport") in missing_breakdown_sports
        ]
        if not coerced_breakdown:
            break

        data.setdefault("scheduleBreakdown", [])
        data["scheduleBreakdown"].extend(coerced_breakdown)
        grounding_sources.extend(_extract_grounding_sources(retry_response))

    data = _assign_event_ids(data, games_name)
    _flag_nonstandard_boxing_classes(data)

    data["metadata"] = Metadata(
        source="Gemini Draft",
        fetchedAt=datetime.now(timezone.utc).isoformat(),
        groundingSources=list(dict.fromkeys(grounding_sources)),
        lowConfidence=not grounding_sources,
    ).model_dump(mode="json")

    doc = GamesHubDocument.model_validate(data)
    return doc, doc.model_dump(mode="json")


def save_draft_to_dynamodb(doc_dict: dict, games_name: str) -> Optional[str]:
    """Same review-queue pattern as generate_athlete_content_llm.py's
    save_draft_to_dynamodb — writes to SportsData as status=pending_review
    via firebase_store.save_review_draft(). Non-fatal on failure since the
    local JSON save already succeeded."""
    draft = {
        "games_id": doc_dict["games_id"],
        "games_name": games_name,
        "trigger_reason": "llm_games_hub_generation",
        "proposed_data": doc_dict,
        "status": "pending_review",
    }
    try:
        return firebase_store.save_review_draft(draft)
    except Exception as e:
        print(f"⚠️ Failed to save draft to DynamoDB: {e}", file=sys.stderr)
        return None


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    while True:
        games_name = input("\nGames name (e.g. 'Asian Games 2026', blank to quit): ").strip()
        if not games_name:
            print("Done.")
            break
        focus_country = input("Focus country [India]: ").strip() or "India"

        print(f"\nGenerating Games Hub draft for {games_name} ({focus_country})...")
        try:
            doc, doc_dict = generate_games_hub(games_name, focus_country)
        except (GenerationError, ValidationError) as e:
            print(f"FAILED: {e}", file=sys.stderr)
            continue

        out_path = os.path.join(OUTPUT_DIR, f"{doc.games_id}.json")
        with open(out_path, "w") as f:
            json.dump(doc_dict, f, indent=2, default=str)

        if doc_dict.get("metadata", {}).get("lowConfidence"):
            print(
                "🚨 LOW CONFIDENCE DRAFT: Gemini returned zero search-grounding sources — "
                "athlete names, dates, weight classes, and medal counts in this draft were "
                "likely NOT actually verified via search and may be hallucinated. Do not "
                "approve without manually checking against official sources.",
                file=sys.stderr,
            )

        draft_id = save_draft_to_dynamodb(doc_dict, games_name)
        if draft_id:
            print(f"✅ Draft also saved to DynamoDB review queue: draft_id={draft_id}")
        else:
            print("⚠️ DynamoDB save failed — local JSON was still saved above.")

        print(f"Saved: {out_path}")
        print(json.dumps(doc_dict, indent=2, default=str))


if __name__ == "__main__":
    main()