# """
# generate_player_content_llm.py

# Gemini (search-grounded) content generator for individual cricket TEST
# PLAYER profiles — same architecture as generate_team_content_llm.py:
#   - google.genai client (API key or Vertex AI fallback, same env vars)
#   - Pydantic schema models (coreInfo / record_highlight / analytics)
#   - generic coercion engine (_coerce_to_model / _generic_coerce_scalar)
#   - a small domain _sanitize() layer (flag lookup, format pinning)
#   - retry-fill pass targeting historically-flaky fields (max_passes=3),
#     with a _retry_log attached to the saved JSON
#   - local JSON output to ./llm_player_drafts/, AND a DynamoDB review-queue
#     write via firebase_store.save_review_draft(), same pending_review
#     pattern as the team/athlete scripts

# Run it and enter player names one at a time — same interactive flow as
# generate_team_content_llm.py's `main()`.

# Requires GEMINI_API_KEY (or Vertex AI project/location) in the environment
# — same as the team/athlete scripts, already configured in this environment.
# """

# import json
# import os
# import re
# import sys
# import typing
# from datetime import datetime, timezone
# from typing import Optional

# from google import genai
# from google.genai import types
# from pydantic import BaseModel, Field, ValidationError

# import firebase_store
# from player_image_lookup import get_player_photo_url


# # ═══════════════════════════════════════════════════════════════════════
# # SCHEMA
# # ═══════════════════════════════════════════════════════════════════════

# class CoreInfo(BaseModel):
#     playerId: str
#     name: str
#     country: Optional[str] = None
#     flag: Optional[str] = Field(None, description="ISO country code, e.g. 'IN', 'LK'")
#     role: Optional[str] = Field(None, description="Batter | Bowler | All-rounder | Wicketkeeper")
#     battingStyle: Optional[str] = None
#     bowlingStyle: Optional[str] = None
#     dateOfBirth: Optional[str] = None
#     birthPlace: Optional[str] = None
#     heightCm: Optional[float] = None
#     jerseyNo: Optional[str] = None
#     debutDate: Optional[str] = Field(None, description="their Test debut date")
#     profileImage: Optional[str] = None
#     bio: Optional[str] = Field(None, description="2-4 factual sentences, no editorializing")

#     model_config = {"extra": "forbid"}


# class ProgressPoint(BaseModel):
#     year: str = Field(..., description="e.g. '2023' — chart label, kept as a string")
#     value: float = Field(
#         ...,
#         description=(
#             "the record stat's own value for that year (e.g. running highest-score-so-far, "
#             "or best bowling figures converted to a single comparable number) — the same "
#             "metric named in record_highlight.category, tracked year over year"
#         ),
#     )

#     model_config = {"extra": "forbid"}


# class Benchmark(BaseModel):
#     label: str = Field(..., description="exactly one of: 'Personal', 'National', 'World'")
#     value: Optional[str] = None
#     holder: Optional[str] = None
#     date: Optional[str] = None
#     venue: Optional[str] = None

#     model_config = {"extra": "forbid"}


# _BENCHMARK_LABELS = ["Personal", "National", "World"]


# class RecordHighlight(BaseModel):
#     category: Optional[str] = None
#     result: Optional[str] = None
#     type: Optional[str] = Field(None, description="'PR' (Personal Record) | 'NR' (National Record) | 'WR' (World Record)")
#     typeFull: Optional[str] = None
#     opponent: Optional[str] = None
#     venue: Optional[str] = None
#     date: Optional[str] = None
#     aiInsight: Optional[str] = Field(None, description="1-2 factual sentences on why it matters")
#     progressData: list[ProgressPoint] = Field(default_factory=list)
#     benchmarks: list[Benchmark] = Field(
#         default_factory=lambda: [Benchmark(label=l) for l in _BENCHMARK_LABELS],
#         description="always exactly 3 entries: Personal/National/World",
#     )

#     model_config = {"extra": "forbid"}


# class BattingStats(BaseModel):
#     matches: Optional[int] = None
#     innings: Optional[int] = None
#     runs: Optional[int] = None
#     average: Optional[float] = None
#     strikeRate: Optional[float] = None
#     hundreds: Optional[int] = None
#     fifties: Optional[int] = None
#     highScore: Optional[str] = None

#     model_config = {"extra": "forbid"}


# class BowlingStats(BaseModel):
#     matches: Optional[int] = None
#     innings: Optional[int] = None
#     wickets: Optional[int] = None
#     average: Optional[float] = None
#     economy: Optional[float] = None
#     strikeRate: Optional[float] = None
#     bestBowling: Optional[str] = None

#     model_config = {"extra": "forbid"}


# class SeasonalPoint(BaseModel):
#     year: str
#     matches: Optional[int] = None
#     runs: Optional[int] = None
#     wickets: Optional[int] = None
#     average: Optional[float] = None

#     model_config = {"extra": "forbid"}


# class Analytics(BaseModel):
#     battingStats: BattingStats = Field(default_factory=BattingStats)
#     bowlingStats: BowlingStats = Field(default_factory=BowlingStats)
#     seasonalData: list[SeasonalPoint] = Field(default_factory=list)

#     model_config = {"extra": "forbid"}


# class SourceRef(BaseModel):
#     source_name: str = "Gemini (search-grounded — unverified, needs human review)"
#     source_url: str = "https://sportsfan360.internal/llm-draft"
#     fetched_at: str = ""

#     model_config = {"extra": "forbid"}


# class PlayerDocument(BaseModel):
#     playerId: str
#     sportId: str = "cricket"
#     format: str  # "Test" — script currently only targets Test
#     currentClubId: Optional[str] = None
#     coreInfo: CoreInfo
#     record_highlight: Optional[RecordHighlight] = None
#     analytics: Analytics = Field(default_factory=Analytics)
#     grounding_sources: list[str] = Field(default_factory=list)
#     source: SourceRef = Field(default_factory=SourceRef)

#     model_config = {"extra": "forbid"}


# # ═══════════════════════════════════════════════════════════════════════
# # GEMINI CLIENT — same setup as generate_team_content_llm.py
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

# GEMINI_MODEL = os.getenv("PLAYER_EXTRACTION_MODEL", "gemini-2.5-flash")
# OUTPUT_DIR = "llm_player_drafts"


# class GenerationError(Exception):
#     pass


# # ═══════════════════════════════════════════════════════════════════════
# # HELPERS
# # ═══════════════════════════════════════════════════════════════════════

# def _is_empty(value) -> bool:
#     if value is None:
#         return True
#     if isinstance(value, str):
#         return value.strip() == "" or value.strip().upper() == "N/A"
#     if isinstance(value, (list, dict)):
#         return len(value) == 0
#     return False


# def _get_path(data: dict, path: str):
#     cur = data
#     for part in path.split("."):
#         if not isinstance(cur, dict):
#             return None
#         cur = cur.get(part)
#     return cur


# def _set_path(data: dict, path: str, value) -> None:
#     parts = path.split(".")
#     cur = data
#     for part in parts[:-1]:
#         cur = cur.setdefault(part, {})
#     cur[parts[-1]] = value


# def _slugify(name: str) -> str:
#     return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


# RETRYABLE_PATHS = [
#     "coreInfo.dateOfBirth",
#     "coreInfo.birthPlace",
#     "coreInfo.heightCm",
#     "coreInfo.debutDate",
#     "coreInfo.role",
#     "record_highlight",
#     "record_highlight.progressData",
#     "record_highlight.benchmarks",
#     "analytics.battingStats",
#     "analytics.bowlingStats",
#     "analytics.seasonalData",
# ]

# MAX_RETRY_PASSES = 3

# _RECORD_HIGHLIGHT_CORE_FIELDS = ("category", "result", "date")


# def _record_highlight_core_is_empty(record_highlight) -> bool:
#     if not isinstance(record_highlight, dict):
#         return True
#     return all(_is_empty(record_highlight.get(f)) for f in _RECORD_HIGHLIGHT_CORE_FIELDS)


# def _stats_block_is_empty(stats: dict, meaningful_keys: tuple) -> bool:
#     if not isinstance(stats, dict):
#         return True
#     return all(_is_empty(stats.get(k)) for k in meaningful_keys)


# def _tiered_list_has_data(entries: list, key_names: tuple) -> bool:
#     for e in entries or []:
#         if not isinstance(e, dict):
#             continue
#         for k in key_names:
#             if not _is_empty(e.get(k)):
#                 return True
#     return False


# def _missing_retryable_paths(data: dict) -> list[str]:
#     missing = []
#     for p in RETRYABLE_PATHS:
#         if p == "record_highlight":
#             if _record_highlight_core_is_empty(_get_path(data, p)):
#                 missing.append(p)
#             continue
#         if p == "record_highlight.benchmarks":
#             benches = _get_path(data, p) or []
#             if not _tiered_list_has_data(benches, ("value", "holder")):
#                 missing.append(p)
#             continue
#         if p == "analytics.battingStats":
#             if _stats_block_is_empty(_get_path(data, p), ("runs", "average", "matches")):
#                 missing.append(p)
#             continue
#         if p == "analytics.bowlingStats":
#             if _stats_block_is_empty(_get_path(data, p), ("wickets", "average", "matches")):
#                 missing.append(p)
#             continue
#         if _is_empty(_get_path(data, p)):
#             missing.append(p)
#     return missing


# # ═══════════════════════════════════════════════════════════════════════
# # COERCION ENGINE (same trimmed approach as generate_team_content_llm.py)
# # ═══════════════════════════════════════════════════════════════════════

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
#         if isinstance(value, (int, float, bool)):
#             return str(value)
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
#         return value
#     if target_type is float:
#         if isinstance(value, (int, float)) and not isinstance(value, bool):
#             return float(value)
#         if isinstance(value, str):
#             match = re.search(r"-?\d+\.?\d*", value)
#             return float(match.group()) if match else None
#         return value
#     if target_type is bool:
#         if isinstance(value, str):
#             return value.strip().lower() in ("true", "yes", "1")
#         return bool(value)
#     return value


# def _coerce_to_model(data, model_cls, path=""):
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
#                 coerced_items = []
#                 for item in value:
#                     if isinstance(item, dict):
#                         coerced_items.append(_coerce_to_model(item, item_type, path=field_path))
#                 result[name] = coerced_items
#             else:
#                 result[name] = [
#                     _generic_coerce_scalar(v, item_type) if item_type in (str, int, float, bool) else v
#                     for v in value
#                     if v is not None
#                 ]
#             continue

#         if _is_model(annotation):
#             result[name] = (
#                 _coerce_to_model(value, annotation, path=field_path) if isinstance(value, dict) else None
#             )
#             continue

#         if annotation in (str, int, float, bool):
#             result[name] = _generic_coerce_scalar(value, annotation)
#             continue

#         result[name] = value

#     # Backfill required string fields the LLM omitted entirely.
#     for name, field in model_cls.model_fields.items():
#         if name in result:
#             continue
#         if not field.is_required():
#             continue
#         annotation, _ = _unwrap_optional(field.annotation)
#         if annotation is str:
#             result[name] = "N/A"

#     return result


# # ═══════════════════════════════════════════════════════════════════════
# # DOMAIN SANITIZE LAYER
# # ═══════════════════════════════════════════════════════════════════════

# _FLAG_BY_COUNTRY = {
#     "india": "IN",
#     "sri lanka": "LK",
#     "australia": "AU",
#     "england": "EN",
#     "pakistan": "PK",
#     "new zealand": "NZ",
#     "south africa": "SA",
#     "west indies": "WI",
#     "bangladesh": "BD",
#     "afghanistan": "AF",
#     "zimbabwe": "ZW",
#     "ireland": "IE",
# }


# def _normalize_benchmarks(record_highlight):
#     if not isinstance(record_highlight, dict):
#         return record_highlight
#     raw = record_highlight.get("benchmarks")
#     by_label = {}
#     if isinstance(raw, list):
#         for entry in raw:
#             if isinstance(entry, dict) and isinstance(entry.get("label"), str):
#                 by_label[entry["label"].strip().lower()] = entry
#     record_highlight["benchmarks"] = [
#         {**by_label.get(label.lower(), {}), "label": label} for label in _BENCHMARK_LABELS
#     ]
#     return record_highlight


# def _sanitize(data: dict, fmt: str, club_id: Optional[str]) -> dict:
#     core_info = data.setdefault("coreInfo", {})

#     # Force flag from a lookup table rather than trusting Gemini, same
#     # defense-in-depth pattern as the team/athlete scripts' flag fix.
#     country = (core_info.get("country") or "").strip().lower()
#     if country in _FLAG_BY_COUNTRY:
#         core_info["flag"] = _FLAG_BY_COUNTRY[country]

#     # Pin format + club post-generation regardless of what Gemini returns.
#     data["format"] = fmt
#     data["currentClubId"] = club_id

#     if not isinstance(data.get("record_highlight"), dict):
#         data["record_highlight"] = {}
#     data["record_highlight"] = _normalize_benchmarks(data["record_highlight"])

#     return data


# # ═══════════════════════════════════════════════════════════════════════
# # PROMPTING
# # ═══════════════════════════════════════════════════════════════════════

# def _build_prompt(name: str, fmt: str, club_hint: Optional[str], focus_fields: list[str] | None = None) -> str:
#     club_line = f"\n\nThey currently play for {club_hint}." if club_hint else ""

#     base = f"""
# You are drafting a structured {fmt}-format cricket PLAYER document for SportsFan360, a sports
# fan engagement platform. You have access to Google Search — use it to look up current,
# accurate facts about this player rather than relying only on what you already know.

# Player: {name}
# Format: {fmt}{club_line}

# Produce a single JSON object with exactly these top-level keys: coreInfo, record_highlight,
# analytics.

# NULL-AVOIDANCE: use null only after you have genuinely searched for a field and could not
# find or confirm it. Do not default to null out of caution for facts that are realistically
# findable (date of birth, birthplace, role, career stats).

# coreInfo: playerId (lowercase_snake_case of the name), name, country, flag, role
# ("Batter"|"Bowler"|"All-rounder"|"Wicketkeeper"), battingStyle, bowlingStyle, dateOfBirth
# (YYYY-MM-DD), birthPlace, heightCm, jerseyNo, debutDate (their {fmt} debut date), bio
# (2-4 factual sentences, no editorializing).

# record_highlight: the player's single most notable, still-relevant {fmt} record (e.g. highest
# individual score, best bowling figures, fastest century) — {{category, result, type, typeFull,
# opponent, venue, date, aiInsight, progressData, benchmarks}}.
#     - type MUST be exactly one of "PR" (Personal Record), "NR" (National Record), "WR" (World
#       Record).
#     - progressData: list of {{"year": "2023", "value": <the record stat's own value that
#       year>}} objects, up to 6 most recent years, tracking the SAME metric named in category
#       (e.g. if category is "Highest Individual Score", value is that year's highest score they
#       made; if "Best Bowling Figures", convert the figures to a single comparable number such
#       as wickets taken). Search "<player> <record type> progression by year" if needed; if a
#       real year-by-year trend isn't findable, it is fine to return [].
#     - benchmarks: always exactly 3 entries, label "Personal"/"National"/"World" in that order —
#       "Personal" = this player's own best mark for the category, "National" = the national
#       record for the category (may be held by someone else), "World" = the outright Test
#       record for the category. Fill value/holder/date/venue for each where findable.

# analytics (derive from facts you actually find via search — avoid leaving these null when
# realistically findable; only use null/[] after a genuine search attempt comes up empty):
# - analytics.battingStats: {{matches, innings, runs, average, strikeRate, hundreds, fifties,
#   highScore}} — this player's career {fmt} batting numbers.
# - analytics.bowlingStats: {{matches, innings, wickets, average, economy, strikeRate,
#   bestBowling}} — this player's career {fmt} bowling numbers. If this player is a specialist
#   batter who rarely or never bowls in {fmt} cricket, it is correct to leave these null rather
#   than inventing zeros — do not force numbers that don't exist.
# - analytics.seasonalData: list of {{year, matches, runs, wickets, average}} objects, one per
#   year across the last ~5 years (most recent last), summarizing that year's {fmt} form.

# Respond with ONLY a single JSON object containing coreInfo, record_highlight, and analytics.
# No prose, no markdown code fences, no commentary before or after the JSON.
# """

#     if not focus_fields:
#         return base

#     focus_list = ", ".join(focus_fields)
#     return base + f"""

# IMPORTANT — TARGETED RETRY:
# A previous search pass could not find reliable data for these specific fields: {focus_list}

# For THIS pass, prioritize finding data for exactly these fields, trying different search
# angles — e.g. "{name} date of birth", "{name} {fmt} career stats", "{name} Test debut date".
# Only return null for a field in this list if, after genuinely attempting these searches, no
# reliable source has the information.

# Still return the FULL set of keys, reusing whatever solid data you already have for the rest.
# """


# # ═══════════════════════════════════════════════════════════════════════
# # GEMINI CALL
# # ═══════════════════════════════════════════════════════════════════════

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
#         raise GenerationError(
#             f"Gemini response was empty or contained no JSON (response length: {len(raw)} chars){diag}"
#         )
#     try:
#         return json.loads(raw[start:end])
#     except json.JSONDecodeError as e:
#         raise GenerationError(f"Gemini response was not valid JSON: {e} [response length: {len(raw)} chars]") from e


# def _call_gemini(prompt: str):
#     try:
#         return client.models.generate_content(
#             model=GEMINI_MODEL,
#             contents=prompt,
#             config=types.GenerateContentConfig(
#                 temperature=0.2,
#                 max_output_tokens=8192,
#                 tools=[types.Tool(google_search=types.GoogleSearch())],
#             ),
#         )
#     except Exception as e:
#         raise GenerationError(f"Gemini generation call failed: {e}") from e


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


# def _generate_single_pass(name: str, fmt: str, club_hint: Optional[str], focus_fields: list[str] | None = None):
#     prompt = _build_prompt(name, fmt, club_hint, focus_fields=focus_fields)
#     response = _call_gemini(prompt)
#     try:
#         data = _extract_json(response.text, response_meta=response)
#     except GenerationError:
#         response = _call_gemini(prompt)
#         data = _extract_json(response.text, response_meta=response)
#     return data, response


# # ═══════════════════════════════════════════════════════════════════════
# # GENERATION PIPELINE
# # ═══════════════════════════════════════════════════════════════════════

# def generate_player_profile(name: str, fmt: str = "Test", club_id: Optional[str] = None,
#                              club_hint: Optional[str] = None, max_passes: int = MAX_RETRY_PASSES):
#     data, response = _generate_single_pass(name, fmt, club_hint)
#     data = _sanitize(data, fmt, club_id)
#     data = _coerce_to_model(data, PlayerDocument)

#     data["playerId"] = _slugify(name)
#     data["sportId"] = "cricket"
#     if isinstance(data.get("coreInfo"), dict):
#         data["coreInfo"]["playerId"] = data["playerId"]
#         data["coreInfo"]["name"] = name
#         # Gemini is not asked to draft profileImage — same "never let the
#         # LLM invent an image URL" reasoning as coreInfo.logoUrl/
#         # teamPhotoUrl in generate_team_content_llm.py. Resolved separately
#         # from a real source instead.
#         data["coreInfo"]["profileImage"] = get_player_photo_url(name)

#     data["source"] = SourceRef(fetched_at=datetime.now(timezone.utc).isoformat()).model_dump(mode="json")

#     grounding_sources = _extract_grounding_sources(response)
#     retry_log = []

#     for pass_num in range(2, max_passes + 1):
#         missing = _missing_retryable_paths(data)
#         if not missing:
#             break

#         try:
#             retry_data, retry_response = _generate_single_pass(name, fmt, club_hint, focus_fields=missing)
#         except GenerationError as e:
#             print(f"[retry pass {pass_num}] failed: {e}", file=sys.stderr)
#             break

#         retry_data = _sanitize(retry_data, fmt, club_id)
#         retry_data = _coerce_to_model(retry_data, PlayerDocument)

#         filled_this_pass = []
#         for p in missing:
#             if p == "record_highlight":
#                 candidate = _get_path(retry_data, p)
#                 if not _record_highlight_core_is_empty(candidate):
#                     existing = data.get("record_highlight") or {}
#                     for field_name, field_value in candidate.items():
#                         if field_name == "benchmarks":
#                             continue
#                         if not _is_empty(field_value):
#                             existing[field_name] = field_value
#                     data["record_highlight"] = existing
#                     filled_this_pass.append(p)
#                 continue
#             if p == "record_highlight.benchmarks":
#                 candidate = _get_path(retry_data, p)
#                 if isinstance(candidate, list) and _tiered_list_has_data(candidate, ("value", "holder")):
#                     _set_path(data, p, candidate)
#                     filled_this_pass.append(p)
#                 continue
#             if p == "record_highlight.progressData":
#                 candidate = _get_path(retry_data, p)
#                 if isinstance(candidate, list) and candidate:
#                     _set_path(data, p, candidate)
#                     filled_this_pass.append(p)
#                 continue
#             if p in ("analytics.battingStats", "analytics.bowlingStats"):
#                 candidate = _get_path(retry_data, p) or {}
#                 existing = _get_path(data, p) or {}
#                 for field_name, field_value in candidate.items():
#                     if not _is_empty(field_value):
#                         existing[field_name] = field_value
#                 _set_path(data, p, existing)
#                 if not _stats_block_is_empty(existing, tuple(existing.keys()) or ("matches",)):
#                     filled_this_pass.append(p)
#                 continue
#             candidate = _get_path(retry_data, p)
#             if not _is_empty(candidate):
#                 _set_path(data, p, candidate)
#                 filled_this_pass.append(p)

#         retry_log.append({"pass": pass_num, "targeted": missing, "filled": filled_this_pass})

#         if filled_this_pass:
#             grounding_sources.extend(_extract_grounding_sources(retry_response))
#         else:
#             break

#     data = _sanitize(data, fmt, club_id)  # re-apply in case a retry pass touched coreInfo/format
#     data["grounding_sources"] = list(dict.fromkeys(grounding_sources))

#     profile = PlayerDocument.model_validate(data)
#     profile_dict = profile.model_dump(mode="json")
#     profile_dict["_retry_log"] = retry_log
#     return profile, profile_dict


# # ═══════════════════════════════════════════════════════════════════════
# # DYNAMODB REVIEW-QUEUE WRITE — same pattern as generate_team_content_llm.py
# # ═══════════════════════════════════════════════════════════════════════

# def save_draft_to_dynamodb(profile_dict: dict, name: str) -> str | None:
#     """
#     Writes this LLM-generated player profile into the DynamoDB review queue
#     via firebase_store.save_review_draft(), same pending_review pattern as
#     the team/athlete scripts — entity distinguished by playerId, plus
#     trigger_reason so reviewers can tell player drafts apart in the queue.

#     Returns the new draft_id, or None if the write failed — non-fatal,
#     since the local JSON save already succeeded.
#     """
#     proposed_data = {k: v for k, v in profile_dict.items() if k != "_retry_log"}

#     draft = {
#         "athlete_id": profile_dict["playerId"],  # reuses the review-queue's existing key name
#         "athlete_name": name,
#         "sport": "cricket",
#         "trigger_reason": "llm_player_content_generation",
#         "proposed_data": proposed_data,
#         "status": "pending_review",
#     }

#     try:
#         return firebase_store.save_review_draft(draft)
#     except Exception as e:
#         print(f"⚠️ Failed to save player draft to DynamoDB: {e}", file=sys.stderr)
#         return None


# # ═══════════════════════════════════════════════════════════════════════
# # CLI
# # ═══════════════════════════════════════════════════════════════════════

# def main():
#     os.makedirs(OUTPUT_DIR, exist_ok=True)

#     club_id = None
#     club_hint = None
#     fmt = "Test"  # only format currently supported

#     while True:
#         name = input("\nPlayer name (blank to quit): ").strip()
#         if not name:
#             print("Done.")
#             break

#         print(f"\nGenerating draft for {name} ({fmt})...")
#         try:
#             profile, profile_dict = generate_player_profile(name, fmt, club_id, club_hint)
#         except (GenerationError, ValidationError) as e:
#             print(f"FAILED: {e}", file=sys.stderr)
#             continue

#         out_path = os.path.join(OUTPUT_DIR, f"{profile.playerId}.json")
#         with open(out_path, "w") as f:
#             json.dump(profile_dict, f, indent=2, default=str)

#         draft_id = save_draft_to_dynamodb(profile_dict, name)
#         if draft_id:
#             print(f"✅ Draft also saved to DynamoDB review queue: draft_id={draft_id}")
#         else:
#             print("⚠️ DynamoDB save failed — local JSON was still saved above.")

#         if profile_dict.get("_retry_log"):
#             print("\nRetry log:")
#             for entry in profile_dict["_retry_log"]:
#                 print(
#                     f"  pass {entry['pass']}: targeted {entry['targeted']} "
#                     f"-> filled {entry['filled'] or '(nothing new)'}"
#                 )

#         print(f"Saved: {out_path}")
#         print(json.dumps(profile_dict, indent=2, default=str))


# if __name__ == "__main__":
#     main()





"""
generate_player_content_llm.py

Gemini (search-grounded) content generator for individual cricket TEST
PLAYER profiles — same architecture as generate_team_content_llm.py:
  - google.genai client (API key or Vertex AI fallback, same env vars)
  - Pydantic schema models (coreInfo / record_highlight / analytics)
  - generic coercion engine (_coerce_to_model / _generic_coerce_scalar)
  - a small domain _sanitize() layer (flag lookup, format pinning)
  - retry-fill pass targeting historically-flaky fields (max_passes=3),
    with a _retry_log attached to the saved JSON
  - local JSON output to ./llm_player_drafts/, AND a DynamoDB review-queue
    write via firebase_store.save_review_draft(), same pending_review
    pattern as the team/athlete scripts

Run it and enter player names one at a time — same interactive flow as
generate_team_content_llm.py's `main()`.

Requires GEMINI_API_KEY (or Vertex AI project/location) in the environment
— same as the team/athlete scripts, already configured in this environment.
"""

import json
import os
import re
import sys
import typing
from datetime import datetime, timezone
from typing import Optional

from google import genai
from google.genai import types
from pydantic import BaseModel, Field, ValidationError

import firebase_store
from player_image_lookup import get_player_photo_url


# ═══════════════════════════════════════════════════════════════════════
# SCHEMA
# ═══════════════════════════════════════════════════════════════════════

class CoreInfo(BaseModel):
    playerId: str
    name: str
    country: Optional[str] = None
    flag: Optional[str] = Field(None, description="ISO country code, e.g. 'IN', 'LK'")
    role: Optional[str] = Field(None, description="Batter | Bowler | All-rounder | Wicketkeeper")
    battingStyle: Optional[str] = None
    bowlingStyle: Optional[str] = None
    dateOfBirth: Optional[str] = None
    birthPlace: Optional[str] = None
    heightCm: Optional[float] = None
    jerseyNo: Optional[str] = None
    debutDate: Optional[str] = Field(None, description="their Test debut date")
    profileImage: Optional[str] = None
    bio: Optional[str] = Field(None, description="2-4 factual sentences, no editorializing")

    model_config = {"extra": "forbid"}


class ProgressPoint(BaseModel):
    year: str = Field(..., description="e.g. '2023' — chart label, kept as a string")
    value: float = Field(
        ...,
        description=(
            "the record stat's own value for that year (e.g. running highest-score-so-far, "
            "or best bowling figures converted to a single comparable number) — the same "
            "metric named in record_highlight.category, tracked year over year"
        ),
    )
    event: Optional[str] = Field(
        None,
        description="the specific match/innings this year's value came from, e.g. 'vs England, Edgbaston'",
    )

    model_config = {"extra": "forbid"}


class Benchmark(BaseModel):
    label: str = Field(..., description="exactly one of: 'Personal', 'National', 'World'")
    value: Optional[str] = None
    holder: Optional[str] = None
    date: Optional[str] = None
    venue: Optional[str] = None

    model_config = {"extra": "forbid"}


_BENCHMARK_LABELS = ["Personal", "National", "World"]


class RecordHighlight(BaseModel):
    category: Optional[str] = None
    result: Optional[str] = None
    type: Optional[str] = Field(None, description="'PR' (Personal Record) | 'NR' (National Record) | 'WR' (World Record)")
    typeFull: Optional[str] = None
    opponent: Optional[str] = None
    venue: Optional[str] = None
    date: Optional[str] = None
    aiInsight: Optional[str] = Field(None, description="1-2 factual sentences on why it matters")
    progressData: list[ProgressPoint] = Field(default_factory=list)
    benchmarks: list[Benchmark] = Field(
        default_factory=lambda: [Benchmark(label=l) for l in _BENCHMARK_LABELS],
        description="always exactly 3 entries: Personal/National/World",
    )

    model_config = {"extra": "forbid"}


class BattingStats(BaseModel):
    matches: Optional[int] = None
    innings: Optional[int] = None
    runs: Optional[int] = None
    average: Optional[float] = None
    strikeRate: Optional[float] = None
    hundreds: Optional[int] = None
    fifties: Optional[int] = None
    highScore: Optional[str] = None

    model_config = {"extra": "forbid"}


class BowlingStats(BaseModel):
    matches: Optional[int] = None
    innings: Optional[int] = None
    wickets: Optional[int] = None
    average: Optional[float] = None
    economy: Optional[float] = None
    strikeRate: Optional[float] = None
    bestBowling: Optional[str] = None

    model_config = {"extra": "forbid"}


class SeasonalPoint(BaseModel):
    year: str
    matches: Optional[int] = None
    runs: Optional[int] = None
    wickets: Optional[int] = None
    average: Optional[float] = None

    model_config = {"extra": "forbid"}


class Analytics(BaseModel):
    battingStats: BattingStats = Field(default_factory=BattingStats)
    bowlingStats: BowlingStats = Field(default_factory=BowlingStats)
    seasonalData: list[SeasonalPoint] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class SourceRef(BaseModel):
    source_name: str = "Gemini (search-grounded — unverified, needs human review)"
    source_url: str = "https://sportsfan360.internal/llm-draft"
    fetched_at: str = ""

    model_config = {"extra": "forbid"}


class PlayerDocument(BaseModel):
    playerId: str
    sportId: str = "cricket"
    format: str  # "Test" — script currently only targets Test
    currentClubId: Optional[str] = None
    coreInfo: CoreInfo
    record_highlight: Optional[RecordHighlight] = None
    analytics: Analytics = Field(default_factory=Analytics)
    grounding_sources: list[str] = Field(default_factory=list)
    source: SourceRef = Field(default_factory=SourceRef)

    model_config = {"extra": "forbid"}


# ═══════════════════════════════════════════════════════════════════════
# GEMINI CLIENT — same setup as generate_team_content_llm.py
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

GEMINI_MODEL = os.getenv("PLAYER_EXTRACTION_MODEL", "gemini-2.5-flash")
OUTPUT_DIR = "llm_player_drafts"


class GenerationError(Exception):
    pass


# ═══════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════

def _is_empty(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == "" or value.strip().upper() == "N/A"
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def _get_path(data: dict, path: str):
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _set_path(data: dict, path: str, value) -> None:
    parts = path.split(".")
    cur = data
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


RETRYABLE_PATHS = [
    "coreInfo.dateOfBirth",
    "coreInfo.birthPlace",
    "coreInfo.heightCm",
    "coreInfo.debutDate",
    "coreInfo.role",
    "record_highlight",
    "record_highlight.progressData",
    "record_highlight.benchmarks",
    "analytics.battingStats",
    "analytics.bowlingStats",
    "analytics.seasonalData",
]

MAX_RETRY_PASSES = 3

_RECORD_HIGHLIGHT_CORE_FIELDS = ("category", "result", "date")


def _record_highlight_core_is_empty(record_highlight) -> bool:
    if not isinstance(record_highlight, dict):
        return True
    return all(_is_empty(record_highlight.get(f)) for f in _RECORD_HIGHLIGHT_CORE_FIELDS)


def _stats_block_is_empty(stats: dict, meaningful_keys: tuple) -> bool:
    if not isinstance(stats, dict):
        return True
    return all(_is_empty(stats.get(k)) for k in meaningful_keys)


def _tiered_list_has_data(entries: list, key_names: tuple) -> bool:
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        for k in key_names:
            if not _is_empty(e.get(k)):
                return True
    return False


def _missing_retryable_paths(data: dict) -> list[str]:
    missing = []
    for p in RETRYABLE_PATHS:
        if p == "record_highlight":
            if _record_highlight_core_is_empty(_get_path(data, p)):
                missing.append(p)
            continue
        if p == "record_highlight.benchmarks":
            benches = _get_path(data, p) or []
            if not _tiered_list_has_data(benches, ("value", "holder")):
                missing.append(p)
            continue
        if p == "analytics.battingStats":
            if _stats_block_is_empty(_get_path(data, p), ("runs", "average", "matches")):
                missing.append(p)
            continue
        if p == "analytics.bowlingStats":
            if _stats_block_is_empty(_get_path(data, p), ("wickets", "average", "matches")):
                missing.append(p)
            continue
        if _is_empty(_get_path(data, p)):
            missing.append(p)
    return missing


# ═══════════════════════════════════════════════════════════════════════
# COERCION ENGINE (same trimmed approach as generate_team_content_llm.py)
# ═══════════════════════════════════════════════════════════════════════

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
        if isinstance(value, (int, float, bool)):
            return str(value)
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
        return value
    if target_type is float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            match = re.search(r"-?\d+\.?\d*", value)
            return float(match.group()) if match else None
        return value
    if target_type is bool:
        if isinstance(value, str):
            return value.strip().lower() in ("true", "yes", "1")
        return bool(value)
    return value


def _coerce_to_model(data, model_cls, path=""):
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
                coerced_items = []
                for item in value:
                    if isinstance(item, dict):
                        coerced_items.append(_coerce_to_model(item, item_type, path=field_path))
                result[name] = coerced_items
            else:
                result[name] = [
                    _generic_coerce_scalar(v, item_type) if item_type in (str, int, float, bool) else v
                    for v in value
                    if v is not None
                ]
            continue

        if _is_model(annotation):
            result[name] = (
                _coerce_to_model(value, annotation, path=field_path) if isinstance(value, dict) else None
            )
            continue

        if annotation in (str, int, float, bool):
            result[name] = _generic_coerce_scalar(value, annotation)
            continue

        result[name] = value

    # Backfill required string fields the LLM omitted entirely.
    for name, field in model_cls.model_fields.items():
        if name in result:
            continue
        if not field.is_required():
            continue
        annotation, _ = _unwrap_optional(field.annotation)
        if annotation is str:
            result[name] = "N/A"

    return result


# ═══════════════════════════════════════════════════════════════════════
# DOMAIN SANITIZE LAYER
# ═══════════════════════════════════════════════════════════════════════

_FLAG_BY_COUNTRY = {
    "india": "IN",
    "sri lanka": "LK",
    "australia": "AU",
    "england": "EN",
    "pakistan": "PK",
    "new zealand": "NZ",
    "south africa": "SA",
    "west indies": "WI",
    "bangladesh": "BD",
    "afghanistan": "AF",
    "zimbabwe": "ZW",
    "ireland": "IE",
}


def _normalize_benchmarks(record_highlight):
    if not isinstance(record_highlight, dict):
        return record_highlight
    raw = record_highlight.get("benchmarks")
    by_label = {}
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict) and isinstance(entry.get("label"), str):
                by_label[entry["label"].strip().lower()] = entry
    record_highlight["benchmarks"] = [
        {**by_label.get(label.lower(), {}), "label": label} for label in _BENCHMARK_LABELS
    ]
    return record_highlight


def _sanitize(data: dict, fmt: str, club_id: Optional[str]) -> dict:
    core_info = data.setdefault("coreInfo", {})

    # Force flag from a lookup table rather than trusting Gemini, same
    # defense-in-depth pattern as the team/athlete scripts' flag fix.
    country = (core_info.get("country") or "").strip().lower()
    if country in _FLAG_BY_COUNTRY:
        core_info["flag"] = _FLAG_BY_COUNTRY[country]

    # Pin format + club post-generation regardless of what Gemini returns.
    data["format"] = fmt
    data["currentClubId"] = club_id

    if not isinstance(data.get("record_highlight"), dict):
        data["record_highlight"] = {}
    data["record_highlight"] = _normalize_benchmarks(data["record_highlight"])

    return data


# ═══════════════════════════════════════════════════════════════════════
# PROMPTING
# ═══════════════════════════════════════════════════════════════════════

def _build_prompt(name: str, fmt: str, club_hint: Optional[str], focus_fields: list[str] | None = None) -> str:
    club_line = f"\n\nThey currently play for {club_hint}." if club_hint else ""

    base = f"""
You are drafting a structured {fmt}-format cricket PLAYER document for SportsFan360, a sports
fan engagement platform. You have access to Google Search — use it to look up current,
accurate facts about this player rather than relying only on what you already know.

Player: {name}
Format: {fmt}{club_line}

Produce a single JSON object with exactly these top-level keys: coreInfo, record_highlight,
analytics.

NULL-AVOIDANCE: use null only after you have genuinely searched for a field and could not
find or confirm it. Do not default to null out of caution for facts that are realistically
findable (date of birth, birthplace, role, career stats).

coreInfo: playerId (lowercase_snake_case of the name), name, country, flag, role
("Batter"|"Bowler"|"All-rounder"|"Wicketkeeper"), battingStyle, bowlingStyle, dateOfBirth
(YYYY-MM-DD), birthPlace, heightCm, jerseyNo, debutDate (their {fmt} debut date), bio
(2-4 factual sentences, no editorializing).

record_highlight: the player's single most notable, still-relevant {fmt} record (e.g. highest
individual score, best bowling figures, fastest century) — {{category, result, type, typeFull,
opponent, venue, date, aiInsight, progressData, benchmarks}}.
    - type MUST be exactly one of "PR" (Personal Record), "NR" (National Record), "WR" (World
      Record).
    - progressData: list of {{"year": "2023", "value": <the record stat's own value that
      year>, "event": "<the specific match/innings this value is from, e.g. 'vs England,
      Edgbaston'>"}} objects, up to 6 most recent years, tracking the SAME metric named in
      category (e.g. if category is "Highest Individual Score", value is that year's highest
      score they made, and event is which match it happened in; if "Best Bowling Figures",
      convert the figures to a single comparable number such as wickets taken, with event
      naming that match). Search "<player> <record type> progression by year" if needed; if a
      real year-by-year trend isn't findable, it is fine to return []; event may be null if the
      specific match can't be confirmed even when the value itself is known.
    - benchmarks: always exactly 3 entries, label "Personal"/"National"/"World" in that order —
      "Personal" = this player's own best mark for the category, "National" = the national
      record for the category (may be held by someone else), "World" = the outright Test
      record for the category. Fill value/holder/date/venue for each where findable.

analytics (derive from facts you actually find via search — avoid leaving these null when
realistically findable; only use null/[] after a genuine search attempt comes up empty):
- analytics.battingStats: {{matches, innings, runs, average, strikeRate, hundreds, fifties,
  highScore}} — this player's career {fmt} batting numbers.
- analytics.bowlingStats: {{matches, innings, wickets, average, economy, strikeRate,
  bestBowling}} — this player's career {fmt} bowling numbers. If this player is a specialist
  batter who rarely or never bowls in {fmt} cricket, it is correct to leave these null rather
  than inventing zeros — do not force numbers that don't exist.
- analytics.seasonalData: list of {{year, matches, runs, wickets, average}} objects, one per
  year across the last ~5 years (most recent last), summarizing that year's {fmt} form.

Respond with ONLY a single JSON object containing coreInfo, record_highlight, and analytics.
No prose, no markdown code fences, no commentary before or after the JSON.
"""

    if not focus_fields:
        return base

    focus_list = ", ".join(focus_fields)
    return base + f"""

IMPORTANT — TARGETED RETRY:
A previous search pass could not find reliable data for these specific fields: {focus_list}

For THIS pass, prioritize finding data for exactly these fields, trying different search
angles — e.g. "{name} date of birth", "{name} {fmt} career stats", "{name} Test debut date".
Only return null for a field in this list if, after genuinely attempting these searches, no
reliable source has the information.

Still return the FULL set of keys, reusing whatever solid data you already have for the rest.
"""


# ═══════════════════════════════════════════════════════════════════════
# GEMINI CALL
# ═══════════════════════════════════════════════════════════════════════

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
        raise GenerationError(
            f"Gemini response was empty or contained no JSON (response length: {len(raw)} chars){diag}"
        )
    try:
        return json.loads(raw[start:end])
    except json.JSONDecodeError as e:
        raise GenerationError(f"Gemini response was not valid JSON: {e} [response length: {len(raw)} chars]") from e


def _call_gemini(prompt: str):
    try:
        return client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=8192,
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )
    except Exception as e:
        raise GenerationError(f"Gemini generation call failed: {e}") from e


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


def _generate_single_pass(name: str, fmt: str, club_hint: Optional[str], focus_fields: list[str] | None = None):
    prompt = _build_prompt(name, fmt, club_hint, focus_fields=focus_fields)
    response = _call_gemini(prompt)
    try:
        data = _extract_json(response.text, response_meta=response)
    except GenerationError:
        response = _call_gemini(prompt)
        data = _extract_json(response.text, response_meta=response)
    return data, response


# ═══════════════════════════════════════════════════════════════════════
# GENERATION PIPELINE
# ═══════════════════════════════════════════════════════════════════════

def generate_player_profile(name: str, fmt: str = "Test", club_id: Optional[str] = None,
                             club_hint: Optional[str] = None, max_passes: int = MAX_RETRY_PASSES):
    data, response = _generate_single_pass(name, fmt, club_hint)
    data = _sanitize(data, fmt, club_id)
    data = _coerce_to_model(data, PlayerDocument)

    data["playerId"] = _slugify(name)
    data["sportId"] = "cricket"
    if isinstance(data.get("coreInfo"), dict):
        data["coreInfo"]["playerId"] = data["playerId"]
        data["coreInfo"]["name"] = name
        # Gemini is not asked to draft profileImage — same "never let the
        # LLM invent an image URL" reasoning as coreInfo.logoUrl/
        # teamPhotoUrl in generate_team_content_llm.py. Resolved separately
        # from a real source instead.
        data["coreInfo"]["profileImage"] = get_player_photo_url(name)

    data["source"] = SourceRef(fetched_at=datetime.now(timezone.utc).isoformat()).model_dump(mode="json")

    grounding_sources = _extract_grounding_sources(response)
    retry_log = []

    for pass_num in range(2, max_passes + 1):
        missing = _missing_retryable_paths(data)
        if not missing:
            break

        try:
            retry_data, retry_response = _generate_single_pass(name, fmt, club_hint, focus_fields=missing)
        except GenerationError as e:
            print(f"[retry pass {pass_num}] failed: {e}", file=sys.stderr)
            break

        retry_data = _sanitize(retry_data, fmt, club_id)
        retry_data = _coerce_to_model(retry_data, PlayerDocument)

        filled_this_pass = []
        for p in missing:
            if p == "record_highlight":
                candidate = _get_path(retry_data, p)
                if not _record_highlight_core_is_empty(candidate):
                    existing = data.get("record_highlight") or {}
                    for field_name, field_value in candidate.items():
                        if field_name == "benchmarks":
                            continue
                        if not _is_empty(field_value):
                            existing[field_name] = field_value
                    data["record_highlight"] = existing
                    filled_this_pass.append(p)
                continue
            if p == "record_highlight.benchmarks":
                candidate = _get_path(retry_data, p)
                if isinstance(candidate, list) and _tiered_list_has_data(candidate, ("value", "holder")):
                    _set_path(data, p, candidate)
                    filled_this_pass.append(p)
                continue
            if p == "record_highlight.progressData":
                candidate = _get_path(retry_data, p)
                if isinstance(candidate, list) and candidate:
                    _set_path(data, p, candidate)
                    filled_this_pass.append(p)
                continue
            if p in ("analytics.battingStats", "analytics.bowlingStats"):
                candidate = _get_path(retry_data, p) or {}
                existing = _get_path(data, p) or {}
                for field_name, field_value in candidate.items():
                    if not _is_empty(field_value):
                        existing[field_name] = field_value
                _set_path(data, p, existing)
                if not _stats_block_is_empty(existing, tuple(existing.keys()) or ("matches",)):
                    filled_this_pass.append(p)
                continue
            candidate = _get_path(retry_data, p)
            if not _is_empty(candidate):
                _set_path(data, p, candidate)
                filled_this_pass.append(p)

        retry_log.append({"pass": pass_num, "targeted": missing, "filled": filled_this_pass})

        if filled_this_pass:
            grounding_sources.extend(_extract_grounding_sources(retry_response))
        else:
            break

    data = _sanitize(data, fmt, club_id)  # re-apply in case a retry pass touched coreInfo/format
    data["grounding_sources"] = list(dict.fromkeys(grounding_sources))

    profile = PlayerDocument.model_validate(data)
    profile_dict = profile.model_dump(mode="json")
    profile_dict["_retry_log"] = retry_log
    return profile, profile_dict


# ═══════════════════════════════════════════════════════════════════════
# DYNAMODB REVIEW-QUEUE WRITE — same pattern as generate_team_content_llm.py
# ═══════════════════════════════════════════════════════════════════════

def save_draft_to_dynamodb(profile_dict: dict, name: str) -> str | None:
    """
    Writes this LLM-generated player profile into the DynamoDB review queue
    via firebase_store.save_review_draft(), same pending_review pattern as
    the team/athlete scripts — entity distinguished by playerId, plus
    trigger_reason so reviewers can tell player drafts apart in the queue.

    Returns the new draft_id, or None if the write failed — non-fatal,
    since the local JSON save already succeeded.
    """
    proposed_data = {k: v for k, v in profile_dict.items() if k != "_retry_log"}

    draft = {
        "athlete_id": profile_dict["playerId"],  # reuses the review-queue's existing key name
        "athlete_name": name,
        "sport": "cricket",
        "trigger_reason": "llm_player_content_generation",
        "proposed_data": proposed_data,
        "status": "pending_review",
    }

    try:
        return firebase_store.save_review_draft(draft)
    except Exception as e:
        print(f"⚠️ Failed to save player draft to DynamoDB: {e}", file=sys.stderr)
        return None


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    club_id = None
    club_hint = None
    fmt = "Test"  # only format currently supported

    while True:
        name = input("\nPlayer name (blank to quit): ").strip()
        if not name:
            print("Done.")
            break

        print(f"\nGenerating draft for {name} ({fmt})...")
        try:
            profile, profile_dict = generate_player_profile(name, fmt, club_id, club_hint)
        except (GenerationError, ValidationError) as e:
            print(f"FAILED: {e}", file=sys.stderr)
            continue

        out_path = os.path.join(OUTPUT_DIR, f"{profile.playerId}.json")
        with open(out_path, "w") as f:
            json.dump(profile_dict, f, indent=2, default=str)

        draft_id = save_draft_to_dynamodb(profile_dict, name)
        if draft_id:
            print(f"✅ Draft also saved to DynamoDB review queue: draft_id={draft_id}")
        else:
            print("⚠️ DynamoDB save failed — local JSON was still saved above.")

        if profile_dict.get("_retry_log"):
            print("\nRetry log:")
            for entry in profile_dict["_retry_log"]:
                print(
                    f"  pass {entry['pass']}: targeted {entry['targeted']} "
                    f"-> filled {entry['filled'] or '(nothing new)'}"
                )

        print(f"Saved: {out_path}")
        print(json.dumps(profile_dict, indent=2, default=str))


if __name__ == "__main__":
    main()