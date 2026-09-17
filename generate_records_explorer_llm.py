"""
generate_records_explorer_llm.py

Standalone content generator for SportsFan360's RECORDS EXPLORER — separate
from generate_athlete_content_llm.py (individual athlete profiles) and
generate_match_center_llm.py (match-center documents). This one drafts a
single records-explorer/categories/{recordCategoryId} document per run.

Structure (per the user's schema docs):
    records-explorer/
    └── categories/
          └── {recordCategoryId}

Examples of recordCategoryId:
    athletics-men-100m, athletics-javelin-men, athletics-women-200m
    cricket-men-odi-batting, cricket-men-test-bowling, cricket-women-t20-team
    football-men-premier-league-scoring, football-men-la-liga-goalkeeping,
    football-women-world-cup-team

Supports ATHLETICS, CRICKET, and FOOTBALL, each grounded directly in the
reference schemas the user provided (field names/shapes copied verbatim).

Same architecture as generate_athlete_content_llm.py /
generate_match_center_llm.py on purpose (one file, schema-driven generic
coercion engine, Gemini + Google Search grounding, NULL-AVOIDANCE prompt
instruction) so all three scripts stay consistent to work on side by side.
Single generation pass per document (no retry-fill yet, same as the
match-center script) — add it the same way the athlete script does if
records drafts turn out to have the same field-inconsistency problem.

Run it, pick a sport + identifying details (event/gender for athletics;
gender/format/category for cricket) at a time. Each run prints the
validated JSON, saves it to ./llm_records_explorer_drafts/<recordCategoryId>.json,
and writes it into the DynamoDB review queue (SportsData, status=pending_review)
via firebase_store.save_review_draft() — same pattern
generate_athlete_content_llm.py uses.

NOTE: search-grounded LLM output can still be wrong or out of date — this
is a content-drafting aid for the review queue, not a source-of-truth
stats feed. Every draft still needs human review before publishing.
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

class Sport(str, Enum):
    ATHLETICS = "athletics"
    CRICKET = "cricket"
    FOOTBALL = "football"


class KeyStat(BaseModel):
    label: str
    value: str
    color: Optional[str] = Field(None, description="hex color, e.g. '#CD620E'")

    model_config = {"extra": "forbid"}


class Story(BaseModel):
    """The article/story block — same shape across sports."""
    title: Optional[str] = None
    description: Optional[str] = None
    author: Optional[str] = None
    publishedDate: Optional[str] = None
    location: Optional[str] = None
    quote: Optional[str] = None
    quoteAuthor: Optional[str] = None
    gradient: Optional[str] = Field(None, description="not LLM-drafted, left null for editors — a CSS gradient string")
    content: list[str] = Field(default_factory=list, description="paragraphs of the story")
    keyStats: list[KeyStat] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class Milestone(BaseModel):
    year: str
    title: str
    description: Optional[str] = None

    model_config = {"extra": "forbid"}


class AthleticsGapPoint(BaseModel):
    year: str
    gap: Optional[float] = Field(None, description="numeric gap in the event's native unit, e.g. seconds behind the world record")

    model_config = {"extra": "forbid"}


class CricketGapPoint(BaseModel):
    year: str
    gap: Optional[int] = Field(None, description="numeric gap in the record's native unit, e.g. runs behind the world record")

    model_config = {"extra": "forbid"}


class AthleticsProgress(BaseModel):
    gapData: list[AthleticsGapPoint] = Field(default_factory=list)
    milestones: list[Milestone] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class CricketProgress(BaseModel):
    gapData: list[CricketGapPoint] = Field(default_factory=list)
    milestones: list[Milestone] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class HighlightRef(BaseModel):
    """Not LLM-drafted — content IDs come from the platform's own media
    library, not search. Left as an empty list by the pipeline; included
    in the schema only so a human editor has somewhere to attach them."""
    contentId: str
    contentType: str = Field(..., description="e.g. 'video', 'image', 'article'")

    model_config = {"extra": "forbid"}


# ── Athletics ─────────────────────────────────────────────────────────

class AthleticsRecordEntry(BaseModel):
    type: str = Field(..., description="one of the category's benchmarkType values, e.g. 'National' | 'Olympic' | 'World'")
    athleteId: str = Field(..., description="lowercase-hyphenated slug, e.g. 'usain-bolt'")
    performance: str = Field(..., description="display mark in the event's native unit, e.g. '9.58s', '90.23m'")
    numericValue: float = Field(..., description="the same mark as a plain number, no unit suffix")
    championship: Optional[str] = Field(None, description="event/meet name, e.g. 'World Championships'")
    venue: Optional[str] = None
    date: Optional[str] = Field(None, description="ISO8601 date, e.g. '2009-08-16'")
    color: Optional[str] = Field(None, description="hex color for this tier — pipeline-set, not researched")

    model_config = {"extra": "forbid"}


class AthleticsTrendPoint(BaseModel):
    year: str
    national: Optional[float] = None
    olympic: Optional[float] = None
    world: Optional[float] = None

    model_config = {"extra": "forbid"}


class AthleticsRecordCategory(BaseModel):
    recordCategoryId: str
    sportId: str = "athletics"
    category: str = Field(..., description="'Sprints' | 'Middle Distance' | 'Long Distance' | 'Throws' | 'Jumps' | 'Relays'")
    event: str = Field(..., description="e.g. '100m', 'Javelin Throw'")
    gender: str = Field(..., description="'Men' | 'Women'")
    metricType: str = Field(..., description="'time' | 'distance' | 'points'")
    unit: str = Field(..., description="'seconds' | 'meters' | 'points'")
    benchmarkType: list[str] = Field(default_factory=lambda: ["National", "Olympic", "World"])
    records: list[AthleticsRecordEntry] = Field(default_factory=list)
    trend: list[AthleticsTrendPoint] = Field(default_factory=list)
    story: Story = Field(default_factory=Story)
    progress: AthleticsProgress = Field(default_factory=AthleticsProgress)
    featuredContent: list[HighlightRef] = Field(default_factory=list)
    updatedAt: str

    model_config = {"extra": "forbid"}


# ── Cricket ───────────────────────────────────────────────────────────

class CricketRecordEntry(BaseModel):
    type: str = Field(..., description="one of the category's benchmarkType values, e.g. 'National' | 'ICC' | 'World'")
    holderType: str = Field(..., description="'Player' | 'Team'")
    holderId: str = Field(..., description="lowercase-hyphenated slug — athleteId for Player, teamId for Team")
    performance: str = Field(..., description="display mark, e.g. '14085 Runs', '498/4', 'ICC Rating 935'")
    numericValue: float = Field(..., description="the same mark as a plain number, no unit/label suffix")
    competition: Optional[str] = Field(None, description="e.g. 'ODI Career', 'ICC ODI Rankings', 'England vs Netherlands'")
    opponent: Optional[str] = Field(None, description="opponent team, if this record was set in a specific match; null for career records")
    venue: Optional[str] = None
    date: Optional[str] = Field(None, description="ISO8601 date the record was set/reached")
    color: Optional[str] = Field(None, description="hex color for this tier — pipeline-set, not researched")

    model_config = {"extra": "forbid"}


class CricketTrendPoint(BaseModel):
    year: str
    national: Optional[float] = None
    icc: Optional[float] = None
    world: Optional[float] = None

    model_config = {"extra": "forbid"}


class CricketRecordCategory(BaseModel):
    recordCategoryId: str
    sportId: str = "cricket"
    gender: str = Field(..., description="'Men' | 'Women'")
    format: str = Field(..., description="'Test' | 'ODI' | 'T20'")
    category: str = Field(..., description="'Batting' | 'Bowling' | 'Fielding' | 'Team'")
    metricType: str = Field(..., description="e.g. 'runs', 'wickets', 'rating'")
    unit: str = Field(..., description="e.g. 'runs', 'wickets'")
    benchmarkType: list[str] = Field(default_factory=lambda: ["National", "ICC", "World"])
    records: list[CricketRecordEntry] = Field(default_factory=list)
    trend: list[CricketTrendPoint] = Field(default_factory=list)
    story: Story = Field(default_factory=Story)
    progress: CricketProgress = Field(default_factory=CricketProgress)
    featuredContent: list[HighlightRef] = Field(default_factory=list)
    updatedAt: str

    model_config = {"extra": "forbid"}


# ── Football ──────────────────────────────────────────────────────────

class FootballRecordEntry(BaseModel):
    type: str = Field(..., description="one of the category's benchmarkType values, e.g. 'Competition' | 'National' | 'World'")
    holderType: str = Field(..., description="'Player' | 'Team'")
    holderId: str = Field(..., description="lowercase-hyphenated slug — athleteId for Player, teamId for Team")
    performance: str = Field(..., description="display mark, e.g. '260 Goals', '100 Points'")
    numericValue: float = Field(..., description="the same mark as a plain number, no unit/label suffix")
    competition: Optional[str] = Field(
        None, description="what the record refers to, e.g. 'Premier League', 'Career', or a specific season like 'Premier League 2017-18'"
    )
    club: Optional[str] = Field(None, description="club(s) the record was set with, e.g. 'Blackburn Rovers / Newcastle United'; null if not applicable")
    date: Optional[str] = Field(None, description="ISO8601 date the record was set/reached")
    color: Optional[str] = Field(None, description="hex color for this tier — pipeline-set, not researched")

    model_config = {"extra": "forbid"}


class FootballTrendPoint(BaseModel):
    year: str
    competition: Optional[float] = None
    national: Optional[float] = None
    world: Optional[float] = None

    model_config = {"extra": "forbid"}


class FootballGapPoint(BaseModel):
    year: str
    gap: Optional[float] = Field(None, description="numeric gap in the record's native unit, e.g. goals behind the world record")

    model_config = {"extra": "forbid"}


class FootballProgress(BaseModel):
    gapData: list[FootballGapPoint] = Field(default_factory=list)
    milestones: list[Milestone] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class FootballRecordCategory(BaseModel):
    recordCategoryId: str
    sportId: str = "football"
    gender: str = Field(..., description="'Men' | 'Women'")
    competition: str = Field(
        ..., description="e.g. 'Premier League', 'La Liga', 'Bundesliga', 'Serie A', 'Ligue 1', 'UEFA Champions League', 'FIFA World Cup', 'UEFA Euro', 'Copa America'"
    )
    category: str = Field(..., description="'Scoring' | 'Playmaking' | 'Goalkeeping' | 'Defending' | 'Team'")
    metricType: str = Field(..., description="e.g. 'goals', 'assists', 'clean_sheets', 'points'")
    unit: str = Field(..., description="e.g. 'goals', 'assists', 'clean sheets', 'points'")
    benchmarkType: list[str] = Field(default_factory=lambda: ["Competition", "National", "World"])
    records: list[FootballRecordEntry] = Field(default_factory=list)
    trend: list[FootballTrendPoint] = Field(default_factory=list)
    story: Story = Field(default_factory=Story)
    progress: FootballProgress = Field(default_factory=FootballProgress)
    featuredContent: list[HighlightRef] = Field(default_factory=list)
    updatedAt: str

    model_config = {"extra": "forbid"}


def get_schema_for_sport(sport: Sport) -> type[BaseModel]:
    return {
        Sport.ATHLETICS: AthleticsRecordCategory,
        Sport.CRICKET: CricketRecordCategory,
        Sport.FOOTBALL: FootballRecordCategory,
    }[sport]


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

GEMINI_MODEL = os.getenv("RECORDS_EXPLORER_EXTRACTION_MODEL", "gemini-2.5-flash")
OUTPUT_DIR = "llm_records_explorer_drafts"


class GenerationError(Exception):
    pass


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")


# ── Generic schema-driven coercion engine — same approach as the other
# two scripts, reused verbatim so any nested shape here (records, trend,
# story, progress) gets the same shape-mismatch protection automatically
# instead of a hand-written patch. ──────────────────────────────────────

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

        result[name] = value

    return result


def _extract_json(raw_text: str, response_meta=None) -> dict:
    raw = (raw_text or "").strip()
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start == -1 or end == 0:
        raise GenerationError(f"Gemini response did not contain valid JSON: {raw[:300]}")
    try:
        return json.loads(raw[start:end])
    except json.JSONDecodeError as e:
        hint = ""
        try:
            candidates = getattr(response_meta, "candidates", None) or []
            finish_reason = getattr(candidates[0], "finish_reason", None) if candidates else None
            if finish_reason and str(finish_reason).upper() != "STOP":
                hint = f" (finish_reason={finish_reason} — likely truncated)"
        except Exception:
            pass
        raise GenerationError(f"Gemini response was not valid JSON: {e}{hint}") from e


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


def _generate(prompt: str) -> dict:
    response = _call_gemini(prompt)
    try:
        return _extract_json(response.text, response_meta=response)
    except GenerationError:
        response = _call_gemini(prompt)
        return _extract_json(response.text, response_meta=response)


_NULL_AVOIDANCE = """
NULL-AVOIDANCE: use null (JSON null) only after you have genuinely searched for a field and
could not find or confirm it. Do not guess or invent numbers — but also do not default to
null out of caution for facts that are realistically findable (record holders, marks, dates,
venues for well-known National/Olympic/World or National/ICC/World records are almost always
documented). Accuracy matters more than completeness, and a human editor will review this
draft, but an all-null draft is not more "safe" than a well-researched one — it just means
the search wasn't tried hard enough.
"""

_STORY_PROGRESS_NOTES = """
story: {title, description, author, publishedDate, location, quote, quoteAuthor, content,
keyStats}. (gradient is not drafted — leave null, a human editor/designer sets that.)
- title/description: a short editorial framing for this record category, e.g. "India's
  Fastest 100m" / "Journey behind the national record".
- author: a plausible byline, e.g. "Athletics Federation", "SportsFan Editorial" — not a
  specific real journalist's name unless you find one actually attached to a source article.
- quote / quoteAuthor: only include a real, attributable quote if you find one via search;
  otherwise leave both null rather than inventing one.
- content: 2-4 short paragraphs of factual narrative about this record category's history
  and significance.
- keyStats: 3-5 {label, value, color} highlight stats pulled from the records you found
  (e.g. the record mark, an average, a career total) — color is a hex string, pick distinct
  colors per stat.

progress: {gapData, milestones}.
- gapData: list of {year, gap} — the numeric gap between the National tier and the World
  tier for that year (National mark minus World mark, in the record's native unit), for as
  many years as you can find data points for (aim for 2-5 points spanning a meaningful
  period). Reflects how the gap has closed or widened over time.
- milestones: list of {year, title, description} — 1-4 genuinely notable moments in this
  record category's history (a record being broken, a milestone reached), factual and
  dated.
"""


def _build_athletics_prompt(category: str, event: str, gender: str) -> str:
    return f"""
You are drafting a Records Explorer category document for SportsFan360's athletics section.
You have access to Google Search — use it to look up current, accurate record data rather
than relying only on what you already know.

Event: {event}
Category: {category}
Gender: {gender}

Produce a single JSON object with keys: category, event, gender, metricType, unit,
benchmarkType, records, trend, story, progress.
- category MUST be exactly "{category}".
- event MUST be exactly "{event}".
- gender MUST be exactly "{gender}".
- metricType: "time" for track events, "distance" for field events, "points" for combined
  events (e.g. decathlon).
- unit: "seconds" | "meters" | "points" matching metricType.
- benchmarkType: exactly ["National", "Olympic", "World"] — always these 3 tiers, in this
  order, mapped to India-national / Olympic-record / outright-world-record for this event.

records: list of exactly 3 entries, one per benchmarkType tier, each
{{type, athleteId, performance, numericValue, championship, venue, date, color}}.
- type: exactly "National" | "Olympic" | "World", matching benchmarkType order.
- athleteId: lowercase-hyphenated slug of the record holder's name (e.g. "usain-bolt").
- performance: display mark in the event's native unit (e.g. "9.58s", "90.23m").
- numericValue: the same mark as a plain number, no unit suffix.
- championship/venue/date: where and when this specific record was set — well-documented,
  stable facts for National/Olympic/World tiers of well-known events; search confidently.
- color: leave null — a human editor/designer assigns these.
These are the event's own top records, not any one athlete's personal best — find the actual
current record holder for each tier even if that's 3 different people.

trend: list of {{year, national, olympic, world}} — the National/Olympic/World record marks
AS THEY STOOD at several points in time (aim for 3-6 data points spanning at least a decade,
including the current year), showing how each tier's record has progressed. Use plain numbers
(no unit suffix) for national/olympic/world.

{_STORY_PROGRESS_NOTES}
{_NULL_AVOIDANCE}
Respond with ONLY the JSON object. No prose, no markdown code fences.
"""


def _build_cricket_prompt(gender: str, fmt: str, category: str) -> str:
    return f"""
You are drafting a Records Explorer category document for SportsFan360's cricket section.
You have access to Google Search — use it to look up current, accurate record data rather
than relying only on what you already know.

Gender: {gender}
Format: {fmt}
Category: {category}

Produce a single JSON object with keys: gender, format, category, metricType, unit,
benchmarkType, records, trend, story, progress.
- gender MUST be exactly "{gender}".
- format MUST be exactly "{fmt}".
- category MUST be exactly "{category}".
- metricType/unit: pick what actually fits this category — e.g. "runs"/"runs" for Batting,
  "wickets"/"wickets" for Bowling, "dismissals"/"dismissals" for Fielding, a team total
  metric (e.g. "runs"/"runs" or "wickets"/"wickets" depending on the record) for Team.
- benchmarkType: exactly ["National", "ICC", "World"] — always these 3 tiers, in this order.
  "National" = the Indian national {fmt} record for this category, "ICC" = the relevant
  official ICC ranking/rating record (e.g. peak ICC rating) OR an ICC-tournament record if
  no rating concept applies to this category, "World" = the outright {fmt} world record for
  this category, any nation.

records: list of exactly 3 entries, one per benchmarkType tier, each
{{type, holderType, holderId, performance, numericValue, competition, opponent, venue, date,
color}}.
- type: exactly "National" | "ICC" | "World", matching benchmarkType order.
- holderType: "Player" for individual batting/bowling/fielding records, "Team" for team
  records (e.g. highest team total).
- holderId: lowercase-hyphenated slug (player name for Player, team name for Team, e.g.
  "virat-kohli" or "england").
- performance: display mark, e.g. "14085 Runs", "498/4", "ICC Rating 935".
- numericValue: the same mark as a plain number (no unit/label suffix).
- competition: what the record refers to, e.g. "{fmt} Career", "ICC {fmt} Rankings", or the
  specific match if it's a single-innings/match record.
- opponent: the opposing team if this record was set in one specific match; null for
  career-spanning records.
- venue/date: where and when set — well-documented facts for headline cricket records;
  search confidently rather than defaulting to null.
- color: leave null — a human editor/designer assigns these.
These are the category's own top records, not any one player's personal stats — find the
actual current record holder for each tier even if that's 3 different people/teams.

trend: list of {{year, national, icc, world}} — the National/ICC/World record values AS THEY
STOOD at several points in time (aim for 3-6 data points spanning at least a decade, including
the current year). Use plain numbers (no unit suffix) for national/icc/world; null for a tier
at a given year if the concept doesn't apply that far back (e.g. ICC ratings didn't exist
before a certain year).

{_STORY_PROGRESS_NOTES}
{_NULL_AVOIDANCE}
Respond with ONLY the JSON object. No prose, no markdown code fences.
"""


def _build_football_prompt(gender: str, competition: str, category: str) -> str:
    return f"""
You are drafting a Records Explorer category document for SportsFan360's football section.
You have access to Google Search — use it to look up current, accurate record data rather
than relying only on what you already know.

Gender: {gender}
Competition: {competition}
Category: {category}

Produce a single JSON object with keys: gender, competition, category, metricType, unit,
benchmarkType, records, trend, story, progress.
- gender MUST be exactly "{gender}".
- competition MUST be exactly "{competition}".
- category MUST be exactly "{category}".
- metricType/unit: pick what actually fits this category — e.g. "goals"/"goals" for Scoring,
  "assists"/"assists" for Playmaking, "clean_sheets"/"clean sheets" for Goalkeeping, a
  defensive metric (e.g. "tackles"/"tackles" or "clearances"/"clearances") for Defending, a
  team metric (e.g. "points"/"points" or "goals"/"goals") for Team.
- benchmarkType: exactly ["Competition", "National", "World"] — always these 3 tiers, in
  this order. "Competition" = the all-time record for this exact competition ({competition})
  and category, "National" = the equivalent record for the relevant nation's national team
  (e.g. England's national-team scoring record, for an England-relevant search) or the
  nation most relevant to this competition/category, "World" = the outright global record for
  this category, any competition/nation, typically a career total.

records: list of exactly 3 entries, one per benchmarkType tier, each
{{type, holderType, holderId, performance, numericValue, competition, club, date, color}}.
- type: exactly "Competition" | "National" | "World", matching benchmarkType order.
- holderType: "Player" for individual scoring/playmaking/goalkeeping/defending records,
  "Team" for team records (e.g. most points in a season).
- holderId: lowercase-hyphenated slug (player name for Player, team name for Team, e.g.
  "alan-shearer" or "manchester-city").
- performance: display mark, e.g. "260 Goals", "100 Points".
- numericValue: the same mark as a plain number (no unit/label suffix).
- competition (this field, inside each record entry): what the record specifically refers
  to, e.g. "{competition}", "Career", or a specific season like "Premier League 2017-18" for
  a single-season team record.
- club: the club(s) the record was set with, e.g. "Blackburn Rovers / Newcastle United";
  null if not applicable (e.g. for a national-team record).
- date: when set/reached — well-documented facts for headline football records; search
  confidently rather than defaulting to null.
- color: leave null — a human editor/designer assigns these.
These are the category's own top records, not any one player's personal stats — find the
actual current record holder for each tier even if that's 3 different people/teams.

trend: list of {{year, competition, national, world}} — the Competition/National/World
record values AS THEY STOOD at several points in time (aim for 3-6 data points spanning at
least a decade, including the current year). Use plain numbers (no unit suffix) for
competition/national/world; null for a tier at a given year if not meaningfully determinable
that far back.

{_STORY_PROGRESS_NOTES}
{_NULL_AVOIDANCE}
Respond with ONLY the JSON object. No prose, no markdown code fences.
"""


def generate_athletics_record_category(category: str, event: str, gender: str) -> tuple[AthleticsRecordCategory, dict]:
    data = _generate(_build_athletics_prompt(category, event, gender))
    data = _coerce_to_model(data, AthleticsRecordCategory)
    data["sportId"] = "athletics"
    data["category"] = category
    data["event"] = event
    data["gender"] = gender
    data.setdefault("benchmarkType", ["National", "Olympic", "World"])
    data["recordCategoryId"] = f"athletics-{_slugify(gender)}-{_slugify(event)}"
    data["updatedAt"] = datetime.now(timezone.utc).isoformat()
    obj = AthleticsRecordCategory.model_validate(data)
    return obj, obj.model_dump(mode="json")


def generate_cricket_record_category(gender: str, fmt: str, category: str) -> tuple[CricketRecordCategory, dict]:
    data = _generate(_build_cricket_prompt(gender, fmt, category))
    data = _coerce_to_model(data, CricketRecordCategory)
    data["sportId"] = "cricket"
    data["gender"] = gender
    data["format"] = fmt
    data["category"] = category
    data.setdefault("benchmarkType", ["National", "ICC", "World"])
    data["recordCategoryId"] = f"cricket-{_slugify(gender)}-{_slugify(fmt)}-{_slugify(category)}"
    data["updatedAt"] = datetime.now(timezone.utc).isoformat()
    obj = CricketRecordCategory.model_validate(data)
    return obj, obj.model_dump(mode="json")


def generate_football_record_category(gender: str, competition: str, category: str) -> tuple[FootballRecordCategory, dict]:
    data = _generate(_build_football_prompt(gender, competition, category))
    data = _coerce_to_model(data, FootballRecordCategory)
    data["sportId"] = "football"
    data["gender"] = gender
    data["competition"] = competition
    data["category"] = category
    data.setdefault("benchmarkType", ["Competition", "National", "World"])
    data["recordCategoryId"] = f"football-{_slugify(gender)}-{_slugify(competition)}-{_slugify(category)}"
    data["updatedAt"] = datetime.now(timezone.utc).isoformat()
    obj = FootballRecordCategory.model_validate(data)
    return obj, obj.model_dump(mode="json")


def save_draft_to_dynamodb(obj_dict: dict, sport: Sport) -> str | None:
    """
    Writes this LLM-generated records-explorer category into the DynamoDB
    review queue (SportsData, entityId="REVIEW#<draft_id>") via
    firebase_store.save_review_draft(), same pending_review pattern the
    other two generation scripts use.

    Returns the new draft_id, or None if the write failed (e.g. AWS
    creds/network not available) -- callers should treat that as
    non-fatal since the local JSON save already succeeded.
    """
    draft = {
        "athlete_id": obj_dict["recordCategoryId"],  # reused field name to match firebase_store's existing draft shape
        "record_category_id": obj_dict["recordCategoryId"],
        "sport": sport.value,
        "trigger_reason": "llm_records_explorer_generation",
        "proposed_data": obj_dict,
        "status": "pending_review",
    }

    try:
        draft_id = firebase_store.save_review_draft(draft)
    except Exception as e:
        print(f"⚠️ Failed to save draft to DynamoDB: {e}", file=sys.stderr)
        return None

    return draft_id


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

def _prompt_choice(label: str, options: list[str]) -> str:
    print(f"\n{label}:")
    for i, o in enumerate(options, 1):
        print(f"  {i}. {o}")
    while True:
        choice = input("Choose a number: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            return options[int(choice) - 1]
        print("Invalid choice, try again.")


_ATHLETICS_CATEGORIES = ["Sprints", "Middle Distance", "Long Distance", "Throws", "Jumps", "Relays"]
_CRICKET_FORMATS = ["Test", "ODI", "T20"]
_CRICKET_CATEGORIES = ["Batting", "Bowling", "Fielding", "Team"]
_FOOTBALL_COMPETITIONS = [
    "Premier League", "La Liga", "Bundesliga", "Serie A", "Ligue 1",
    "UEFA Champions League", "FIFA World Cup", "UEFA Euro", "Copa America",
]
_FOOTBALL_CATEGORIES = ["Scoring", "Playmaking", "Goalkeeping", "Defending", "Team"]
_GENDERS = ["Men", "Women"]


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    while True:
        do_another = input("\nGenerate a records-explorer category? (blank to quit): ").strip()
        if not do_another:
            print("Done.")
            break

        sport = Sport(_prompt_choice("Sport", [s.value for s in Sport]))

        try:
            if sport == Sport.ATHLETICS:
                category = _prompt_choice("Category", _ATHLETICS_CATEGORIES)
                event = input("Event (e.g. '100m', 'Javelin Throw'): ").strip()
                gender = _prompt_choice("Gender", _GENDERS)
                obj, obj_dict = generate_athletics_record_category(category, event, gender)
            elif sport == Sport.CRICKET:
                gender = _prompt_choice("Gender", _GENDERS)
                fmt = _prompt_choice("Format", _CRICKET_FORMATS)
                category = _prompt_choice("Category", _CRICKET_CATEGORIES)
                obj, obj_dict = generate_cricket_record_category(gender, fmt, category)
            else:  # FOOTBALL
                gender = _prompt_choice("Gender", _GENDERS)
                competition = _prompt_choice("Competition", _FOOTBALL_COMPETITIONS)
                category = _prompt_choice("Category", _FOOTBALL_CATEGORIES)
                obj, obj_dict = generate_football_record_category(gender, competition, category)
        except (GenerationError, ValidationError) as e:
            print(f"FAILED: {e}", file=sys.stderr)
            continue

        out_path = os.path.join(OUTPUT_DIR, f"{obj.recordCategoryId}.json")
        with open(out_path, "w") as f:
            json.dump(obj_dict, f, indent=2, default=str)

        draft_id = save_draft_to_dynamodb(obj_dict, sport)
        if draft_id:
            print(f"✅ Draft also saved to DynamoDB review queue: draft_id={draft_id}")
        else:
            print("⚠️ DynamoDB save failed — local JSON was still saved above.")

        print(f"Saved: {out_path}")
        print(json.dumps(obj_dict, indent=2, default=str))


if __name__ == "__main__":
    main()