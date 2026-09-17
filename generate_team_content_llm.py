"""
generate_team_content_llm.py

Gemini (search-grounded) content generator for national CRICKET TEAM
profiles — same architecture as generate_athlete_content_llm.py:
  - google.genai client (API key or Vertex AI fallback, same env vars)
  - Pydantic schema models (coreInfo / record_highlight / analytics / squad)
  - generic coercion engine (_coerce_to_model / _generic_coerce_scalar)
  - a small domain _sanitize() layer (flag lookup, format pinning)
  - retry-fill pass targeting historically-flaky fields (max_passes=3),
    with a _retry_log attached to the saved JSON
  - local JSON output to ./llm_team_drafts/, AND a DynamoDB review-queue
    write via firebase_store.save_review_draft(), same pending_review
    pattern as the athlete script (entity distinguished by team_id)
  - coreInfo.logoUrl / coreInfo.teamPhotoUrl are fetched directly via
    team_image_lookup.py (Wikipedia infobox + Wikimedia Commons), same
    "never let the LLM invent an image URL" reasoning as the athlete
    script's coreInfo.profileImage / coreInfo.welcomeVideoUrl

Run it and enter a team name — same interactive flow as
generate_athlete_content_llm.py's `main()`.

Requires GEMINI_API_KEY (or Vertex AI project/location) in the environment
— same as the athlete script, already configured in this environment.
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
from team_image_lookup import get_team_logo_url, get_team_photo_url


# ═══════════════════════════════════════════════════════════════════════
# SCHEMA
# ═══════════════════════════════════════════════════════════════════════

class CoreInfo(BaseModel):
    teamName: str
    country: Optional[str] = None
    flag: Optional[str] = Field(None, description="ISO country code, e.g. 'IN', 'LK'")
    shortName: Optional[str] = Field(None, description="e.g. 'IND', 'SL'")
    captain: Optional[str] = None
    viceCaptain: Optional[str] = None
    headCoach: Optional[str] = None
    homeGround: Optional[str] = None
    founded: Optional[str] = Field(None, description="year of first Test, e.g. '1932'")
    logoUrl: Optional[str] = None
    teamPhotoUrl: Optional[str] = None
    bio: Optional[str] = Field(None, description="2-4 factual sentences, no editorializing")

    model_config = {"extra": "forbid"}


class ProgressPoint(BaseModel):
    year: str = Field(..., description="e.g. '2022' — chart label, kept as a string")
    value: float = Field(
        ...,
        description=(
            "the team's official ICC year-end Test Championship RATING for that year "
            "(same 0-130ish scale as analytics.stats.rating) — NOT a locally-computed "
            "win percentage, NOT points per match"
        ),
    )

    model_config = {"extra": "forbid"}


class Benchmark(BaseModel):
    label: str = Field(..., description="exactly one of: 'Team', 'Regional', 'World'")
    value: Optional[str] = None
    holder: Optional[str] = None
    date: Optional[str] = None
    venue: Optional[str] = None

    model_config = {"extra": "forbid"}


_BENCHMARK_LABELS = ["Team", "Regional", "World"]


class RecordHighlight(BaseModel):
    category: Optional[str] = Field(None, description="e.g. 'Highest Team Total', 'Most Consecutive Wins'")
    result: Optional[str] = None
    type: Optional[str] = Field(None, description="'TR' (Team Record) | 'WR' (World Record)")
    typeFull: Optional[str] = None
    opponent: Optional[str] = None
    venue: Optional[str] = None
    date: Optional[str] = None
    prevRecord: Optional[str] = None
    prevRecordDate: Optional[str] = None
    improvement: Optional[str] = None
    aiInsight: Optional[str] = Field(
        None, description="2-3 factual sentences on what made this notable, no unfounded editorializing"
    )
    progressData: list[ProgressPoint] = Field(default_factory=list)
    benchmarks: list[Benchmark] = Field(
        default_factory=lambda: [Benchmark(label=l) for l in _BENCHMARK_LABELS],
        description="always exactly 3 entries: Team/Regional/World",
    )

    model_config = {"extra": "forbid"}


class TeamStats(BaseModel):
    worldRank: Optional[str] = None
    rating: Optional[str] = None
    totalMatches: Optional[int] = None
    wins: Optional[int] = None
    losses: Optional[int] = None
    draws: Optional[int] = None
    winPercentage: Optional[float] = None
    worldTestChampionships: Optional[int] = None

    model_config = {"extra": "forbid"}


class SeasonalPoint(BaseModel):
    year: str
    matches: int = 0
    wins: int = 0
    losses: int = 0
    draws: int = 0
    rating: Optional[int] = None
    highlight: bool = False
    glow: bool = False

    model_config = {"extra": "forbid"}


class SeriesPoint(BaseModel):
    year: str
    seriesPlayed: Optional[int] = None
    seriesWon: Optional[int] = None
    seriesLost: Optional[int] = None
    seriesDrawn: Optional[int] = None
    notableOpponent: Optional[str] = None
    result: Optional[str] = None

    model_config = {"extra": "forbid"}


class RadarPoint(BaseModel):
    metric: str
    value: Optional[float] = Field(None, description="0-100 editorial estimate")
    fullMark: float = 100

    model_config = {"extra": "forbid"}


class ConsistencyPoint(BaseModel):
    range: str
    count: Optional[int] = None
    percentage: Optional[float] = None
    peak: bool = False

    model_config = {"extra": "forbid"}


class HeadToHeadPoint(BaseModel):
    opponent: str
    played: Optional[int] = None
    won: Optional[int] = None
    lost: Optional[int] = None
    drawn: Optional[int] = None
    lastResult: Optional[str] = None
    lastMet: Optional[str] = None

    model_config = {"extra": "forbid"}


class CoachImpactPoint(BaseModel):
    year: int
    value: Optional[float] = None
    period: str  # "before" | "after"

    model_config = {"extra": "forbid"}


class Analytics(BaseModel):
    heroLabel: str = "ICC Test Ranking"
    heroStat: Optional[str] = None
    metricLabel: str = "Rating"
    achievementLabel: Optional[str] = None
    stats: Optional[TeamStats] = None
    seasonalData: list[SeasonalPoint] = Field(default_factory=list)
    seriesData: list[SeriesPoint] = Field(default_factory=list)
    radarData: list[RadarPoint] = Field(default_factory=list)
    consistencyBarLabel: str = "matches"
    consistencyData: list[ConsistencyPoint] = Field(default_factory=list)
    consistencyNote: Optional[str] = None
    headToHeadData: list[HeadToHeadPoint] = Field(default_factory=list)
    coachImpactData: list[CoachImpactPoint] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class SquadMember(BaseModel):
    playerId: Optional[str] = None
    name: str
    role: Optional[str] = Field(None, description="Batter | Bowler | All-rounder | Wicketkeeper")
    battingStyle: Optional[str] = None
    bowlingStyle: Optional[str] = None
    testCaps: Optional[int] = None
    isCaptain: bool = False

    model_config = {"extra": "forbid"}


class SourceRef(BaseModel):
    source_name: str = "Gemini (search-grounded — unverified, needs human review)"
    source_url: str = "https://sportsfan360.internal/llm-draft"
    fetched_at: str = ""

    model_config = {"extra": "forbid"}


class TeamDocument(BaseModel):
    team_id: str
    sportId: str = "cricket"
    format: str  # "Test" — script currently only targets Test
    coreInfo: CoreInfo
    record_highlight: Optional[RecordHighlight] = None
    analytics: Analytics = Field(default_factory=Analytics)
    squad: list[SquadMember] = Field(default_factory=list)
    grounding_sources: list[str] = Field(default_factory=list)
    source: SourceRef = Field(default_factory=SourceRef)

    model_config = {"extra": "forbid"}


# ═══════════════════════════════════════════════════════════════════════
# GEMINI CLIENT — same setup as generate_athlete_content_llm.py
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

GEMINI_MODEL = os.getenv("TEAM_EXTRACTION_MODEL", "gemini-2.5-flash")
OUTPUT_DIR = "llm_team_drafts"


class GenerationError(Exception):
    pass


# ═══════════════════════════════════════════════════════════════════════
# HELPERS (mirrors athlete script's _is_empty / _get_path / _set_path)
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
    "coreInfo.captain",
    "coreInfo.headCoach",
    "coreInfo.founded",
    "record_highlight",
    "record_highlight.benchmarks",
    "record_highlight.progressData",
    "analytics.stats",
    "analytics.seasonalData",
    "analytics.seriesData",
    "analytics.headToHeadData",
    "squad",
]

MAX_RETRY_PASSES = 3


def _tiered_list_has_data(entries: list, key_names: tuple) -> bool:
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        for k in key_names:
            if not _is_empty(e.get(k)):
                return True
    return False


def _head_to_head_has_missing_subfields(entries: list) -> bool:
    if not entries:
        return True
    for e in entries:
        if not isinstance(e, dict):
            continue
        for k in ("played", "won", "lost", "drawn"):
            if _is_empty(e.get(k)):
                return True
    return False


_RECORD_HIGHLIGHT_CORE_FIELDS = ("category", "result", "date")


def _record_highlight_core_is_empty(record_highlight) -> bool:
    if not isinstance(record_highlight, dict):
        return True
    return all(_is_empty(record_highlight.get(f)) for f in _RECORD_HIGHLIGHT_CORE_FIELDS)


def _missing_retryable_paths(data: dict) -> list[str]:
    missing = []
    for p in RETRYABLE_PATHS:
        if p == "record_highlight.benchmarks":
            benches = _get_path(data, p) or []
            if not _tiered_list_has_data(benches, ("value", "holder")):
                missing.append(p)
            continue
        if p == "analytics.headToHeadData":
            if _head_to_head_has_missing_subfields(_get_path(data, p)):
                missing.append(p)
            continue
        if p == "record_highlight":
            if _record_highlight_core_is_empty(_get_path(data, p)):
                missing.append(p)
            continue
        if _is_empty(_get_path(data, p)):
            missing.append(p)
    return missing


# ═══════════════════════════════════════════════════════════════════════
# COERCION ENGINE (trimmed version of the athlete script's approach)
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


def _sanitize(data: dict, team_name: str, fmt: str) -> dict:
    # Force flag from a lookup table rather than trusting Gemini, same
    # defense-in-depth pattern as the athlete script's India-flag fix.
    key = team_name.strip().lower()
    core_info = data.setdefault("coreInfo", {})
    if key in _FLAG_BY_COUNTRY:
        core_info["flag"] = _FLAG_BY_COUNTRY[key]

    # Pin format post-generation regardless of what Gemini returns — same
    # fix as the athlete script's cricket-format non-determinism bug.
    data["format"] = fmt

    if not isinstance(data.get("record_highlight"), dict):
        data["record_highlight"] = {}
    data["record_highlight"] = _normalize_benchmarks(data["record_highlight"])

    return data


# ═══════════════════════════════════════════════════════════════════════
# PROMPTING
# ═══════════════════════════════════════════════════════════════════════

def _build_prompt(team_name: str, fmt: str, opponent: Optional[str], focus_fields: list[str] | None = None) -> str:
    focus = f"\n\nBias your head-to-head research toward: {opponent}." if opponent else ""

    base = f"""
You are drafting a structured national cricket TEAM document for SportsFan360, a sports fan
engagement platform. You have access to Google Search — use it to look up current, accurate
facts about this team rather than relying only on what you already know.

Team: {team_name}
Format: {fmt}{focus}

Produce a single JSON object with exactly these top-level keys: coreInfo, record_highlight,
analytics, squad.

NULL-AVOIDANCE: use null only after you have genuinely searched for a field and could not
find or confirm it. Do not default to null out of caution for facts that are realistically
findable (current captain, head coach, ICC ranking, head-to-head records).

coreInfo: teamName, country, flag, shortName, captain, viceCaptain, headCoach, homeGround,
founded, bio (2-4 factual sentences, no editorializing).

record_highlight: the team's single most notable, still-relevant {fmt} record (e.g. highest
team total, most consecutive wins, biggest win margin) — {{category, result, type, typeFull,
opponent, venue, date, prevRecord, prevRecordDate, improvement, aiInsight, progressData,
benchmarks}}.
    - type MUST be exactly "TR" (Team Record) or "WR" (World Record)
    - progressData: list of {{"year": "2022", "value": <ICC year-end Test rating>}} objects,
      up to 6 most recent years. CRITICAL: value MUST be the team's official ICC year-end Test
      Championship RATING for that year (same 0-130ish scale used in analytics.stats.rating),
      NOT a locally-computed win percentage and NOT points per match. Search "<team> ICC Test
      ranking rating <year>" for each year; if a specific year-end figure isn't found, use the
      most recent rating known to be in effect that year rather than leaving it null.
    - benchmarks: always exactly 3 entries, label "Team"/"Regional"/"World" in that order —
      "Team" = this team's own record for the category, "Regional" = the best mark by a team
      from the same cricket region/confederation, "World" = the outright Test record for the
      category. Fill value/holder/date/venue for each; these are well-documented facts for
      cricket's headline team records — avoid leaving them null.

analytics fields (derive from facts you actually find via search — avoid leaving these null
when realistically findable; only use []/{{}}/null after a genuine search attempt comes up
empty):
- analytics.heroStat: current ICC Test rating as a display string, e.g. "118".
- analytics.achievementLabel: one short badge phrase, e.g. "World Test Championship Winners
  2023" — the single most notable recent achievement.
- analytics.stats: {{worldRank, rating, totalMatches, wins, losses, draws, winPercentage,
  worldTestChampionships}} — all searchable facts.
- analytics.seasonalData: list of {{year, matches, wins, losses, draws, rating, highlight,
  glow}} objects, one per year across the last ~5 years (most recent last) — highlight=true
  for standout years, glow=true for the single best year.
- analytics.seriesData: list of {{year, seriesPlayed, seriesWon, seriesLost, seriesDrawn,
  notableOpponent, result}} objects, one per year, summarizing that year's Test series
  outcomes.
- analytics.radarData: list of exactly 5 {{metric, value, fullMark}} objects, metric being
  each of "Batting", "Bowling", "Fielding", "Away Form", "Squad Depth" IN THAT ORDER, fullMark
  always 100, value your 0-100 editorial estimate based on what your research actually showed
  — vary these based on findings, don't default to a flat score.
- analytics.consistencyData: list of {{range, count, percentage, peak}} objects — a rough
  histogram of match results (e.g. "Win by innings", "Win by runs/wkts", "Narrow loss",
  "Heavy loss"), percentage summing to ~100, peak=true for the bucket with the highest count.
- analytics.consistencyNote: 1 factual sentence on the team's consistency pattern (e.g. home
  vs away form gap).
- analytics.headToHeadData: list of {{opponent, played, won, lost, drawn, lastResult,
  lastMet}} objects for this team's 3 most significant Test rivals. CRITICAL: played/won/
  lost/drawn MUST be actual integers found via search (e.g. "{team_name} vs <opponent> Test
  head to head record"), never left at 0/placeholder if a real record exists.
- analytics.coachImpactData: only fill if you find a clear, specific before/after comparison
  tied to a documented head-coach change — [] is the expected/correct answer for most teams,
  not a miss.

squad: list of up to 15 current {fmt} squad members — {{playerId, name, role, battingStyle,
bowlingStyle, testCaps, isCaptain}}. role is exactly one of "Batter" | "Bowler" |
"All-rounder" | "Wicketkeeper". Mark exactly one player isCaptain=true.

If a list field has no data, use an empty list [], never null.

Respond with ONLY a single JSON object containing coreInfo, record_highlight, analytics, and
squad. No prose, no markdown code fences, no commentary before or after the JSON.
"""

    if not focus_fields:
        return base

    focus_list = ", ".join(focus_fields)
    return base + f"""

IMPORTANT — TARGETED RETRY:
A previous search pass could not find reliable data for these specific fields: {focus_list}

For THIS pass, prioritize finding data for exactly these fields, trying different search
angles — e.g. "{team_name} squad {fmt} 2026", "{team_name} head coach", "{team_name} vs
<opponent> Test head to head record". Only return null for a field in this list if, after
genuinely attempting these searches, no reliable source has the information.

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
                max_output_tokens=16384,
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


def _generate_single_pass(team_name: str, fmt: str, opponent: Optional[str], focus_fields: list[str] | None = None):
    prompt = _build_prompt(team_name, fmt, opponent, focus_fields=focus_fields)
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

def generate_team_profile(team_name: str, fmt: str = "Test", opponent: Optional[str] = None,
                           max_passes: int = MAX_RETRY_PASSES):
    data, response = _generate_single_pass(team_name, fmt, opponent)
    data = _sanitize(data, team_name, fmt)
    data = _coerce_to_model(data, TeamDocument)

    data["team_id"] = f"{_slugify(team_name)}_test"
    data["sportId"] = "cricket"
    if isinstance(data.get("coreInfo"), dict):
        data["coreInfo"]["teamName"] = team_name
        # Gemini is not asked to draft logoUrl/teamPhotoUrl (same reasoning
        # as coreInfo.profileImage in the athlete script -- these need a
        # real fetched image URL, not something an LLM should be guessing
        # at or hallucinating a plausible-looking link for).
        data["coreInfo"]["logoUrl"] = get_team_logo_url(team_name)
        data["coreInfo"]["teamPhotoUrl"] = get_team_photo_url(team_name)

    data["source"] = SourceRef(fetched_at=datetime.now(timezone.utc).isoformat()).model_dump(mode="json")

    grounding_sources = _extract_grounding_sources(response)
    retry_log = []

    for pass_num in range(2, max_passes + 1):
        missing = _missing_retryable_paths(data)
        if not missing:
            break

        try:
            retry_data, retry_response = _generate_single_pass(team_name, fmt, opponent, focus_fields=missing)
        except GenerationError as e:
            print(f"[retry pass {pass_num}] failed: {e}", file=sys.stderr)
            break

        retry_data = _sanitize(retry_data, team_name, fmt)
        retry_data = _coerce_to_model(retry_data, TeamDocument)

        filled_this_pass = []
        for p in missing:
            if p == "record_highlight.benchmarks":
                candidate = _get_path(retry_data, p)
                if isinstance(candidate, list) and _tiered_list_has_data(candidate, ("value", "holder")):
                    _set_path(data, p, candidate)
                    filled_this_pass.append(p)
                continue
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
            candidate = _get_path(retry_data, p)
            if not _is_empty(candidate):
                _set_path(data, p, candidate)
                filled_this_pass.append(p)

        retry_log.append({"pass": pass_num, "targeted": missing, "filled": filled_this_pass})

        if filled_this_pass:
            grounding_sources.extend(_extract_grounding_sources(retry_response))
        else:
            break

    data = _sanitize(data, team_name, fmt)  # re-apply in case a retry pass touched coreInfo/format
    data["grounding_sources"] = list(dict.fromkeys(grounding_sources))

    profile = TeamDocument.model_validate(data)
    profile_dict = profile.model_dump(mode="json")
    profile_dict["_retry_log"] = retry_log
    return profile, profile_dict


# ═══════════════════════════════════════════════════════════════════════
# DYNAMODB REVIEW-QUEUE WRITE — same pattern as the athlete script
# ═══════════════════════════════════════════════════════════════════════

def save_draft_to_dynamodb(profile_dict: dict, team_name: str) -> str | None:
    """
    Writes this LLM-generated team profile into the DynamoDB review queue
    via firebase_store.save_review_draft(), same pending_review pattern
    as the athlete script — entity distinguished by team_id (already
    prefixed like "india_test") plus trigger_reason so reviewers can tell
    team drafts apart from athlete drafts in the queue.

    Returns the new draft_id, or None if the write failed — non-fatal,
    since the local JSON save already succeeded.
    """
    proposed_data = {k: v for k, v in profile_dict.items() if k != "_retry_log"}

    draft = {
        "athlete_id": profile_dict["team_id"],  # reuses the review-queue's existing key name
        "athlete_name": team_name,
        "sport": "cricket",
        "trigger_reason": "llm_team_content_generation",
        "proposed_data": proposed_data,
        "status": "pending_review",
    }

    try:
        return firebase_store.save_review_draft(draft)
    except Exception as e:
        print(f"⚠️ Failed to save team draft to DynamoDB: {e}", file=sys.stderr)
        return None


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    while True:
        team_name = input("\nTeam name (blank to quit): ").strip()
        if not team_name:
            print("Done.")
            break

        opponent = input("Opponent to focus head-to-head on (optional, Enter to skip): ").strip() or None
        fmt = "Test"  # only format currently supported

        print(f"\nGenerating draft for {team_name} ({fmt})...")
        try:
            profile, profile_dict = generate_team_profile(team_name, fmt, opponent)
        except (GenerationError, ValidationError) as e:
            print(f"FAILED: {e}", file=sys.stderr)
            continue

        out_path = os.path.join(OUTPUT_DIR, f"{profile.team_id}.json")
        with open(out_path, "w") as f:
            json.dump(profile_dict, f, indent=2, default=str)

        draft_id = save_draft_to_dynamodb(profile_dict, team_name)
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