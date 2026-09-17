"""
generate_match_center_llm.py

Standalone content generator for SportsFan360's MATCH CENTER — separate
from generate_athlete_content_llm.py (which drafts individual athlete
profiles). This one drafts the 5 match-center document types, each scoped
to one of the 3 priority sports (athletics, cricket, football):

    athletes/rankings/{rankingId}        -- Rankings
    match-center/{sportId}/dashboard/    -- Dashboard (home)
    match-center/{sportId}/competitions/{competitionId}  -- Competition
    match-center/{sportId}/matches/{matchId}             -- Match / Event
    match-center/{sportId}/schedules/{date}              -- Schedule

Schemas for athletics and football are grounded directly in the two
reference docs the user provided (field names/shapes copied verbatim
where given). Cricket has no reference doc yet — its shapes below
(CricketSportDetails, cricket's competition table / top-scorer
equivalents) are a best-effort inference from the football/athletics
patterns, not confirmed against a real export. Flag anything cricket-
specific that looks off so it can be corrected once a reference is
available, the same way cricket/football athlete `performance` fields
were grounded in real stats JSON in generate_athlete_content_llm.py.

Same architecture as generate_athlete_content_llm.py on purpose (one
file, schema-driven generic coercion engine, Gemini + Google Search
grounding, NULL-AVOIDANCE prompt instruction) so the two scripts stay
consistent to work on side by side. Retry-fill (the athlete script's
multi-pass targeted re-search) is NOT included here yet — v1 is a single
generation pass per document; add it the same way if match-center drafts
turn out to have the same field-inconsistency problem athlete profiles
did.

Run it, pick a sport + document type + identifying details (competition
name, match participants, date, etc.) at a time. Each run prints the
validated JSON and saves it to ./llm_match_center_drafts/<doc_id>.json.

NOTE: search-grounded LLM output can still be wrong or out of date — this
is a content-drafting aid for the review queue, not a live-scores feed.
Every draft still needs human review before publishing, and none of this
is meant to replace a real live-scoring integration for in-progress
matches (currentMinute, live score, etc. will be stale the moment the
draft is saved).
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


# ═══════════════════════════════════════════════════════════════════════
# SCHEMAS
# ═══════════════════════════════════════════════════════════════════════

class Sport(str, Enum):
    ATHLETICS = "athletics"
    CRICKET = "cricket"
    FOOTBALL = "football"


class DocType(str, Enum):
    RANKING = "ranking"
    DASHBOARD = "dashboard"
    COMPETITION = "competition"
    MATCH = "match"
    SCHEDULE = "schedule"


class Venue(BaseModel):
    name: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None

    model_config = {"extra": "forbid"}


class HighlightRef(BaseModel):
    """Not LLM-drafted — content IDs come from the platform's own media
    library, not search. Left as an empty list by the pipeline; included
    in the schema only so a human editor has somewhere to attach them."""
    contentId: str
    contentType: str = Field(..., description="e.g. 'video', 'image', 'article'")

    model_config = {"extra": "forbid"}


# ── Rankings (shared shape across all 3 sports; athletics uses `score`,
# football/cricket use `rating` — both kept optional on one RankingEntry
# rather than 3 near-identical entry models) ─────────────────────────────

class RankingEntry(BaseModel):
    rank: int
    athleteId: str
    country: Optional[str] = None
    club: Optional[str] = Field(None, description="football/cricket only — the athlete's club/franchise")
    score: Optional[float] = Field(None, description="athletics rankings-points style score")
    rating: Optional[float] = Field(None, description="football/cricket power-rating style score")
    trend: Optional[str] = Field(None, description="'up' | 'down' | 'same'")

    model_config = {"extra": "forbid"}


class Ranking(BaseModel):
    rankingId: str
    sportId: str
    rankingAuthority: str = Field(..., description="e.g. 'World Athletics', 'ICC', 'SportsFan360'")
    rankingType: str = Field(..., description="e.g. 'World', 'Player', 'Team'")
    category: Optional[str] = Field(None, description="e.g. 'Sprints' (athletics), 'Overall' (football)")
    event: Optional[str] = Field(None, description="athletics only, e.g. '100m'")
    gender: Optional[str] = Field(None, description="'Men' | 'Women', when applicable")
    season: str
    updatedAt: str
    rankings: list[RankingEntry] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


# ── Dashboard — one shape covers all 3 sports; the sport-specific
# featured-list fields are simply left [] for sports where they don't
# apply (athletics: featuredEvents/featuredAthletes; football/cricket:
# featuredMatches). quickStats is a free dict since its key set genuinely
# differs per sport (liveEvents/gamesRecords for athletics vs.
# liveMatches/rankedPlayers for football) and typing it per-sport would
# just be 3 near-identical models. ────────────────────────────────────────

class Dashboard(BaseModel):
    sportId: str
    featuredCompetitionId: Optional[str] = None
    quickStats: dict = Field(default_factory=dict)
    featuredCompetitions: list[str] = Field(default_factory=list)
    featuredEvents: list[str] = Field(default_factory=list, description="athletics only")
    featuredAthletes: list[str] = Field(default_factory=list, description="athletics only")
    featuredMatches: list[str] = Field(default_factory=list, description="football/cricket only")
    featuredContent: list[HighlightRef] = Field(default_factory=list)
    updatedAt: str

    model_config = {"extra": "forbid"}


# ── Competition ───────────────────────────────────────────────────────

class CompetitionTableEntry(BaseModel):
    """Athletics medal table."""
    rank: int
    countryId: str
    gold: int = 0
    silver: int = 0
    bronze: int = 0
    total: int = 0

    model_config = {"extra": "forbid"}


class LeagueTableEntry(BaseModel):
    """Football league table."""
    position: int
    teamId: str
    played: int = 0
    won: int = 0
    draw: int = 0
    lost: int = 0
    goalsFor: int = 0
    goalsAgainst: int = 0
    goalDifference: int = 0
    points: int = 0

    model_config = {"extra": "forbid"}


class PointsTableEntry(BaseModel):
    """Cricket points table — INFERRED shape (no reference doc yet),
    modeled on the football leagueTable pattern plus cricket-specific
    net-run-rate. Adjust once a real cricket export is available."""
    position: int
    teamId: str
    played: int = 0
    won: int = 0
    lost: int = 0
    tied: int = 0
    noResult: int = 0
    points: int = 0
    netRunRate: Optional[float] = None

    model_config = {"extra": "forbid"}


class TopScorerEntry(BaseModel):
    """Football topScorers/topAssists."""
    athleteId: str
    goals: Optional[int] = None
    assists: Optional[int] = None

    model_config = {"extra": "forbid"}


class TopCricketPerformerEntry(BaseModel):
    """Cricket topRunScorers/topWicketTakers — INFERRED, no reference doc."""
    athleteId: str
    runs: Optional[int] = None
    wickets: Optional[int] = None

    model_config = {"extra": "forbid"}


class Competition(BaseModel):
    competitionId: str
    sportId: str
    name: str
    shortName: Optional[str] = None
    competitionType: str = Field(
        ..., description="e.g. 'MULTI_SPORT_EVENT', 'LEAGUE', 'TOURNAMENT', 'SERIES' (cricket)"
    )
    season: str
    status: str = Field(..., description="'UPCOMING' | 'LIVE' | 'COMPLETED'")
    hostCity: Optional[str] = None
    hostCountry: Optional[str] = None
    logo: Optional[str] = None  # not LLM-drafted, left null for editors
    banner: Optional[str] = None  # not LLM-drafted, left null for editors
    description: Optional[str] = None
    startDate: Optional[str] = None
    endDate: Optional[str] = None

    # athletics
    competitionTable: list[CompetitionTableEntry] = Field(default_factory=list)

    # football
    teams: list[str] = Field(default_factory=list)
    leagueTable: list[LeagueTableEntry] = Field(default_factory=list)
    topScorers: list[TopScorerEntry] = Field(default_factory=list)
    topAssists: list[TopScorerEntry] = Field(default_factory=list)

    # cricket (inferred)
    pointsTable: list[PointsTableEntry] = Field(default_factory=list)
    topRunScorers: list[TopCricketPerformerEntry] = Field(default_factory=list)
    topWicketTakers: list[TopCricketPerformerEntry] = Field(default_factory=list)

    createdAt: str
    updatedAt: str

    model_config = {"extra": "forbid"}


# ── Match / Event ─────────────────────────────────────────────────────

class Participant(BaseModel):
    athleteId: Optional[str] = Field(None, description="athletics — individual participant")
    teamId: Optional[str] = Field(None, description="football/cricket — team participant")
    type: Optional[str] = Field(None, description="football/cricket: 'team'")

    model_config = {"extra": "forbid"}


class AthleticsResult(BaseModel):
    position: int
    athleteId: str
    performance: Optional[str] = Field(None, description="e.g. '9.85s', '88.13m'")
    medal: Optional[str] = Field(None, description="'Gold' | 'Silver' | 'Bronze' | null")

    model_config = {"extra": "forbid"}


class AthleticsSportDetails(BaseModel):
    category: str = Field(..., description="'Sprints' | 'Middle Distance' | 'Long Distance' | 'Throws' | 'Jumps' | 'Relays'")
    event: str = Field(..., description="e.g. '100m', 'Javelin Throw'")
    round: Optional[str] = Field(None, description="e.g. 'Final', 'Heat 1', 'Semifinal'")
    windSpeed: Optional[str] = Field(None, description="e.g. '+0.8', only applicable to sprints/jumps")
    results: list[AthleticsResult] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class HalfScore(BaseModel):
    home: Optional[int] = None
    away: Optional[int] = None

    model_config = {"extra": "forbid"}


class SidedStat(BaseModel):
    home: Optional[float] = None
    away: Optional[float] = None

    model_config = {"extra": "forbid"}


class GoalEvent(BaseModel):
    athleteId: str
    minute: int

    model_config = {"extra": "forbid"}


class FootballSportDetails(BaseModel):
    matchWeek: Optional[int] = None
    competitionRound: Optional[str] = Field(None, description="e.g. 'Regular Season', 'Quarterfinal'")
    currentMinute: Optional[int] = Field(None, description="only for LIVE matches; null otherwise")
    homeScore: Optional[int] = None
    awayScore: Optional[int] = None
    halfTimeScore: Optional[HalfScore] = None
    fullTimeScore: Optional[HalfScore] = None
    possession: Optional[SidedStat] = None
    shots: Optional[SidedStat] = None
    shotsOnTarget: Optional[SidedStat] = None
    corners: Optional[SidedStat] = None
    fouls: Optional[SidedStat] = None
    yellowCards: Optional[SidedStat] = None
    redCards: Optional[SidedStat] = None
    goalScorers: list[GoalEvent] = Field(default_factory=list)
    assists: list[GoalEvent] = Field(default_factory=list)
    playerOfTheMatch: Optional[str] = None
    result: Optional[str] = Field(None, description="short result summary once the match is COMPLETED, e.g. '2-1 Arsenal win'")

    model_config = {"extra": "forbid"}


class InningsScore(BaseModel):
    """Cricket — INFERRED, no reference doc yet."""
    team: str
    runs: Optional[int] = None
    wickets: Optional[int] = None
    overs: Optional[float] = None

    model_config = {"extra": "forbid"}


class CricketSportDetails(BaseModel):
    """INFERRED shape (no reference doc for cricket match-center yet) —
    modeled on the football sportDetails pattern plus cricket-specific
    toss/innings/format concepts. Adjust once a real export is available,
    the same way AthleticsSportDetails/FootballSportDetails are grounded
    in the user's reference docs."""
    matchType: Optional[str] = Field(None, description="'Test' | 'ODI' | 'T20'")
    tossWinner: Optional[str] = None
    tossDecision: Optional[str] = Field(None, description="'Bat' | 'Bowl'")
    currentInnings: Optional[str] = Field(None, description="only for LIVE matches; null otherwise")
    inningsScores: list[InningsScore] = Field(default_factory=list)
    target: Optional[int] = None
    playerOfTheMatch: Optional[str] = None
    result: Optional[str] = Field(None, description="short result summary once the match is COMPLETED, e.g. 'India won by 6 wickets'")

    model_config = {"extra": "forbid"}


class Match(BaseModel):
    matchId: str
    sportId: str
    competitionId: str
    title: str
    shortTitle: Optional[str] = None
    status: str = Field(..., description="'UPCOMING' | 'LIVE' | 'COMPLETED'")
    stage: Optional[str] = None
    venue: Optional[Venue] = None
    scheduledAt: Optional[str] = None
    startedAt: Optional[str] = None
    endedAt: Optional[str] = None
    participants: list[Participant] = Field(default_factory=list)
    winnerId: Optional[str] = None
    featuredAthletes: list[str] = Field(default_factory=list)
    highlights: list[HighlightRef] = Field(default_factory=list)
    recordReferences: list[str] = Field(default_factory=list)
    createdAt: str
    updatedAt: str

    model_config = {"extra": "forbid"}


class AthleticsMatch(Match):
    sportDetails: AthleticsSportDetails


class FootballMatch(Match):
    sportDetails: FootballSportDetails


class CricketMatch(Match):
    sportDetails: CricketSportDetails


def get_match_schema_for_sport(sport: Sport) -> type[Match]:
    return {
        Sport.ATHLETICS: AthleticsMatch,
        Sport.FOOTBALL: FootballMatch,
        Sport.CRICKET: CricketMatch,
    }[sport]


# ── Schedule ──────────────────────────────────────────────────────────

class Schedule(BaseModel):
    date: str
    sportId: str
    events: list[str] = Field(default_factory=list, description="athletics only — list of matchIds")
    matches: list[str] = Field(default_factory=list, description="football/cricket only — list of matchIds")

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

GEMINI_MODEL = os.getenv("MATCH_CENTER_EXTRACTION_MODEL", "gemini-2.5-flash")
OUTPUT_DIR = "llm_match_center_drafts"


class GenerationError(Exception):
    pass


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")


# ── Generic schema-driven coercion engine — same approach as
# generate_athlete_content_llm.py, reused verbatim so any nested shape
# here (sportDetails, competitionTable, etc.) gets the same shape-
# mismatch protection automatically instead of a hand-written patch. ────

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

        # bools, dict (quickStats), etc. — leave for Pydantic to validate directly.
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


_NULL_AVOIDANCE = """
NULL-AVOIDANCE: use null (JSON null) only after you have genuinely searched for a field and
could not find or confirm it. Do not guess or invent numbers — but also do not default to
null out of caution for facts that are realistically findable. Accuracy matters more than
completeness, and a human editor will review this draft, but an all-null draft is not more
"safe" than a well-researched one — it just means the search wasn't tried hard enough.
"""


def _build_ranking_prompt(sport: Sport, event_or_category: str, gender: Optional[str], season: str) -> str:
    return f"""
You are drafting a Rankings document for SportsFan360's match center. You have access to
Google Search — use it to look up current, accurate rankings rather than relying only on
what you already know.

Sport: {sport.value}
{'Event/category: ' + event_or_category if event_or_category else ''}
{'Gender: ' + gender if gender else ''}
Season: {season}

Produce a single JSON object with keys: rankingId, sportId, rankingAuthority, rankingType,
category, event, gender, season, updatedAt, rankings.
- rankingAuthority: the real body that publishes this ranking (e.g. "World Athletics", "ICC",
  "FIFA") or "SportsFan360" if this is a platform-computed power ranking, not an official one.
- rankings: list of {{rank, athleteId, country, club, score, rating, trend}}, top 10-20
  entries in rank order. athleteId should be a lowercase-hyphenated slug of the athlete's
  name (e.g. "gurindervir-singh"). Use `score` for points-based rankings (athletics), `rating`
  for power-rating-style rankings (football/cricket). trend is "up" | "down" | "same" if you
  can determine it from recent movement, otherwise null.
{_NULL_AVOIDANCE}
Respond with ONLY the JSON object. No prose, no markdown code fences.
"""


def _build_dashboard_prompt(sport: Sport, featured_competition: Optional[str]) -> str:
    return f"""
You are drafting a Dashboard (home) document for SportsFan360's {sport.value} match center.
You have access to Google Search — use it to look up current, accurate information about
what's actually happening in {sport.value} right now.

{'Featured competition to center the dashboard on: ' + featured_competition if featured_competition else 'Pick the most prominent currently-live or upcoming competition in this sport.'}

Produce a single JSON object with keys: sportId, featuredCompetitionId, quickStats,
featuredCompetitions, featuredEvents, featuredAthletes, featuredMatches, updatedAt.
- quickStats: a dict of current counts relevant to {sport.value} right now — for athletics use
  keys like liveEvents, eventsToday, activeCompetitions, worldRecords, gamesRecords,
  highlights; for football/cricket use keys like liveMatches, matchesToday,
  activeCompetitions, rankedPlayers, highlights. Use real current counts if findable,
  otherwise a reasonable estimate based on what's actually happening — not a placeholder 0.
- featuredCompetitions: list of 1-3 competitionId slugs (lowercase-hyphenated) for the most
  prominent live/upcoming competitions.
- featuredEvents (athletics only — [] for other sports): 1-3 matchId slugs for marquee
  upcoming/recent events.
- featuredAthletes (athletics only — [] for other sports): 1-3 athleteId slugs for the
  sport's most prominent current athletes.
- featuredMatches (football/cricket only — [] for athletics): 1-3 matchId slugs for marquee
  upcoming/live matches.
- featuredContent: leave as [] — this is platform media library content, not a research field.
- updatedAt: current UTC timestamp in ISO8601 format.
{_NULL_AVOIDANCE}
Respond with ONLY the JSON object. No prose, no markdown code fences.
"""


def _build_competition_prompt(sport: Sport, competition_name: str) -> str:
    sport_fields = {
        Sport.ATHLETICS: """
- competitionTable: the medal table — list of {rank, countryId, gold, silver, bronze, total},
  countryId lowercase-hyphenated (e.g. "india"). Leave teams/leagueTable/topScorers/
  topAssists/pointsTable/topRunScorers/topWicketTakers as [].""",
        Sport.FOOTBALL: """
- teams: list of teamId slugs (lowercase-hyphenated) competing in this competition.
- leagueTable: list of {position, teamId, played, won, draw, lost, goalsFor, goalsAgainst,
  goalDifference, points} if this is a league format; [] if it's a knockout/cup format
  without a table.
- topScorers / topAssists: list of {athleteId, goals} / {athleteId, assists}, top 5-10.
  Leave competitionTable/pointsTable/topRunScorers/topWicketTakers as [].""",
        Sport.CRICKET: """
- pointsTable: list of {position, teamId, played, won, lost, tied, noResult, points,
  netRunRate} if this is a league/group-stage format; [] if it's a pure knockout format.
- topRunScorers / topWicketTakers: list of {athleteId, runs} / {athleteId, wickets}, top 5-10.
  Leave competitionTable/teams/leagueTable/topScorers/topAssists as [].""",
    }[sport]

    return f"""
You are drafting a Competition document for SportsFan360's {sport.value} match center. You
have access to Google Search — use it to look up current, accurate details about this
competition rather than relying only on what you already know.

Competition: {competition_name}
Sport: {sport.value}

Produce a single JSON object with keys: competitionId, sportId, name, shortName,
competitionType, season, status, hostCity, hostCountry, description, startDate, endDate,
competitionTable, teams, leagueTable, topScorers, topAssists, pointsTable, topRunScorers,
topWicketTakers, createdAt, updatedAt. (logo/banner are not drafted — leave null, a human
editor sets those.)
- competitionId: lowercase-hyphenated slug (e.g. "cwg-2026").
- competitionType: e.g. "MULTI_SPORT_EVENT", "LEAGUE", "TOURNAMENT", "SERIES", "CUP" — pick
  whichever actually describes this competition's format.
- status: "UPCOMING" | "LIVE" | "COMPLETED" based on today's date.
- startDate/endDate: ISO8601 dates if findable.
- createdAt/updatedAt: current UTC timestamp in ISO8601 format for both.

Sport-specific fields:{sport_fields}
{_NULL_AVOIDANCE}
Respond with ONLY the JSON object. No prose, no markdown code fences.
"""


def _build_match_prompt(sport: Sport, match_description: str) -> str:
    sport_fields = {
        Sport.ATHLETICS: """
sportDetails: {category, event, round, windSpeed, results}
- category: 'Sprints' | 'Middle Distance' | 'Long Distance' | 'Throws' | 'Jumps' | 'Relays'
- results: list of {position, athleteId, performance, medal}, medal is "Gold"/"Silver"/
  "Bronze"/null, ordered by position. performance in the event's native unit (e.g. "9.85s").
participants: list of {athleteId} — one per competitor.""",
        Sport.FOOTBALL: """
sportDetails: {matchWeek, competitionRound, currentMinute, homeScore, awayScore,
halfTimeScore, fullTimeScore, possession, shots, shotsOnTarget, corners, fouls, yellowCards,
redCards, goalScorers, assists, playerOfTheMatch, result}
- currentMinute: only if the match is LIVE; null otherwise.
- fullTimeScore: only if status is COMPLETED; null otherwise.
- goalScorers/assists: list of {athleteId, minute}.
participants: list of {teamId, type: "team"} — the two competing teams.""",
        Sport.CRICKET: """
sportDetails: {matchType, tossWinner, tossDecision, currentInnings, inningsScores, target,
playerOfTheMatch, result}
- matchType: "Test" | "ODI" | "T20"
- inningsScores: list of {team, runs, wickets, overs}, one entry per completed/in-progress
  innings.
- currentInnings: only if the match is LIVE; null otherwise.
participants: list of {teamId, type: "team"} — the two competing teams.""",
    }[sport]

    return f"""
You are drafting a Match/Event document for SportsFan360's {sport.value} match center. You
have access to Google Search — use it to look up current, accurate details about this
specific match/event rather than relying only on what you already know.

Match/event: {match_description}
Sport: {sport.value}

Produce a single JSON object with keys: matchId, sportId, competitionId, title, shortTitle,
status, stage, venue, scheduledAt, startedAt, endedAt, participants, winnerId,
featuredAthletes, recordReferences, sportDetails, createdAt, updatedAt. (highlights is not
drafted — leave [], a human editor attaches those from the media library.)
- matchId: lowercase-hyphenated slug (e.g. "mens-100m-final", "ars-vs-che").
- status: "UPCOMING" | "LIVE" | "COMPLETED" based on today's date and the actual result.
- venue: {{name, city, country}}.
- winnerId: the winning athleteId/teamId once status is COMPLETED; null otherwise.
- recordReferences: list of recordId-style slugs if this match/event involved a notable
  record being set (e.g. "games-record-men-100m"); [] if not.
- createdAt/updatedAt: current UTC timestamp in ISO8601 format for both.

Sport-specific fields:
{sport_fields}
{_NULL_AVOIDANCE}
Respond with ONLY the JSON object. No prose, no markdown code fences.
"""


def _build_schedule_prompt(sport: Sport, date: str) -> str:
    return f"""
You are drafting a Schedule document for SportsFan360's {sport.value} match center. You have
access to Google Search — use it to look up what's actually scheduled on this date.

Sport: {sport.value}
Date: {date}

Produce a single JSON object with keys: date, sportId, events, matches.
- events: list of matchId slugs (lowercase-hyphenated) scheduled that day — ATHLETICS ONLY,
  [] for other sports.
- matches: list of matchId slugs scheduled that day — FOOTBALL/CRICKET ONLY, [] for
  athletics.
- If nothing is genuinely scheduled for this sport on this date, return an empty list rather
  than inventing matches.
Respond with ONLY the JSON object. No prose, no markdown code fences.
"""


def _generate(prompt: str) -> dict:
    response = _call_gemini(prompt)
    try:
        return _extract_json(response.text, response_meta=response)
    except GenerationError:
        response = _call_gemini(prompt)
        return _extract_json(response.text, response_meta=response)


def generate_ranking(sport: Sport, event_or_category: str, gender: Optional[str], season: str) -> tuple[Ranking, dict]:
    data = _generate(_build_ranking_prompt(sport, event_or_category, gender, season))
    data = _coerce_to_model(data, Ranking)
    data["sportId"] = sport.value
    data.setdefault("updatedAt", datetime.now(timezone.utc).isoformat())
    ranking = Ranking.model_validate(data)
    return ranking, ranking.model_dump(mode="json")


def generate_dashboard(sport: Sport, featured_competition: Optional[str] = None) -> tuple[Dashboard, dict]:
    data = _generate(_build_dashboard_prompt(sport, featured_competition))
    data = _coerce_to_model(data, Dashboard)
    data["sportId"] = sport.value
    data.setdefault("updatedAt", datetime.now(timezone.utc).isoformat())
    dashboard = Dashboard.model_validate(data)
    return dashboard, dashboard.model_dump(mode="json")


def generate_competition(sport: Sport, competition_name: str) -> tuple[Competition, dict]:
    data = _generate(_build_competition_prompt(sport, competition_name))
    data = _coerce_to_model(data, Competition)
    data["sportId"] = sport.value
    data.setdefault("competitionId", _slugify(competition_name))
    now = datetime.now(timezone.utc).isoformat()
    data.setdefault("createdAt", now)
    data["updatedAt"] = now
    competition = Competition.model_validate(data)
    return competition, competition.model_dump(mode="json")


def generate_match(sport: Sport, match_description: str) -> tuple[Match, dict]:
    schema_cls = get_match_schema_for_sport(sport)
    data = _generate(_build_match_prompt(sport, match_description))
    data = _coerce_to_model(data, schema_cls)
    data["sportId"] = sport.value
    data.setdefault("matchId", _slugify(match_description))
    data.setdefault("highlights", [])
    now = datetime.now(timezone.utc).isoformat()
    data.setdefault("createdAt", now)
    data["updatedAt"] = now
    match = schema_cls.model_validate(data)
    return match, match.model_dump(mode="json")


def generate_schedule(sport: Sport, date: str) -> tuple[Schedule, dict]:
    data = _generate(_build_schedule_prompt(sport, date))
    data = _coerce_to_model(data, Schedule)
    data["sportId"] = sport.value
    data["date"] = date
    schedule = Schedule.model_validate(data)
    return schedule, schedule.model_dump(mode="json")


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


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    while True:
        do_another = input("\nGenerate a match-center document? (blank to quit): ").strip()
        if not do_another:
            print("Done.")
            break

        sport = Sport(_prompt_choice("Sport", [s.value for s in Sport]))
        doc_type = DocType(_prompt_choice("Document type", [d.value for d in DocType]))

        try:
            if doc_type == DocType.RANKING:
                event_or_category = input("Event/category (blank if N/A): ").strip()
                gender = input("Gender (blank if N/A): ").strip() or None
                season = input("Season (e.g. 2026): ").strip() or "2026"
                obj, obj_dict = generate_ranking(sport, event_or_category, gender, season)
                doc_id = obj.rankingId
            elif doc_type == DocType.DASHBOARD:
                featured = input("Featured competition (blank to let Gemini pick): ").strip() or None
                obj, obj_dict = generate_dashboard(sport, featured)
                doc_id = f"{sport.value}-dashboard"
            elif doc_type == DocType.COMPETITION:
                name = input("Competition name: ").strip()
                obj, obj_dict = generate_competition(sport, name)
                doc_id = obj.competitionId
            elif doc_type == DocType.MATCH:
                description = input("Match/event description (e.g. 'Arsenal vs Chelsea, EPL matchweek 12'): ").strip()
                obj, obj_dict = generate_match(sport, description)
                doc_id = obj.matchId
            else:  # SCHEDULE
                date = input("Date (YYYY-MM-DD): ").strip()
                obj, obj_dict = generate_schedule(sport, date)
                doc_id = f"{sport.value}-schedule-{date}"
        except (GenerationError, ValidationError) as e:
            print(f"FAILED: {e}", file=sys.stderr)
            continue

        out_path = os.path.join(OUTPUT_DIR, f"{_slugify(doc_id)}.json")
        with open(out_path, "w") as f:
            json.dump(obj_dict, f, indent=2, default=str)

        print(f"Saved: {out_path}")
        print(json.dumps(obj_dict, indent=2, default=str))


if __name__ == "__main__":
    main()