"""
Pydantic schemas for the AI-powered Athlete Data Automation pipeline.

Every field Gemini extracts must validate against one of these models before
it is allowed into the review queue. Malformed / hallucinated output is
rejected here — it never reaches a human editor, let alone the live profile.
"""

from datetime import date
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, HttpUrl


class Sport(str, Enum):
    SPRINTS = "sprints"
    MIDDLE_DISTANCE = "middle_distance"
    LONG_DISTANCE = "long_distance"
    THROWS = "throws"
    JUMPS = "jumps"
    RELAYS = "relays"
    CRICKET = "cricket"
    BADMINTON = "badminton"
    HOCKEY = "hockey"
    F1 = "f1"
    KABADDI = "kabaddi"
    BASKETBALL = "basketball"
    FOOTBALL = "football"
    TENNIS = "tennis"
    VOLLEYBALL = "volleyball"
    OTHER = "other"


class TriggerType(str, Enum):
    NEW_ATHLETE_ONBOARDING = "new_athlete_onboarding"
    SCHEDULED_RECHECK = "scheduled_recheck"     
    MANUAL_RECHECK = "manual_recheck" 

    # NEW_PERSONAL_BEST = "new_personal_best"
    # NEW_TITLE = "new_title"
    # COACH_CHANGE = "coach_change"
    # RANKING_CHANGE = "ranking_change"
    # INJURY_UPDATE = "injury_update"
    # RETIREMENT = "retirement"
    # TEAM_CHANGE = "team_change"
    # GENERAL_BIO_UPDATE = "general_bio_update"
    # NEW_ATHLETE_ONBOARDING = "new_athlete_onboarding"


class SourceRef(BaseModel):
    """
    Provenance for every draft. With no single licensed source anymore,
    this now points at what Gemini's search grounding actually cited
    (see grounding_sources on BaseAthleteProfile) rather than one fixed
    site. source_url is optional since grounded search may return zero
    citable URLs for some fields/athletes -- editors then rely on
    grounding_sources for spot-checking instead of a single link.
    """
    source_name: str = Field(default="Gemini (Google Search grounding)")
    source_url: Optional[HttpUrl] = None
    fetched_at: str  # ISO8601 timestamp, set by the pipeline


class CareerHighlight(BaseModel):
    title: str
    event: Optional[str] = None
    year: Optional[int] = None


class QuickFacts(BaseModel):
    """Maps to the 'Quick Facts' card on the profile UI."""
    age: Optional[int] = None
    height: Optional[str] = Field(None, description="e.g. '1.86 m'")
    weight: Optional[str] = Field(None, description="e.g. '86 kg'")
    birthplace: Optional[str] = None
    dominant_hand: Optional[str] = None
    personal_best: Optional[str] = Field(None, description="e.g. '90.23 m' or '3:37.87'")
    years_active: Optional[str] = Field(None, description="e.g. '2016' or '2016-present'")
    coach: Optional[str] = None
    honors: list[str] = Field(
        default_factory=list,
        description="e.g. ['Olympic Champion', 'World Champion', 'Asian Champion', 'CWG Gold', 'Diamond League Winner']",
    )

    model_config = {"extra": "forbid"}


class Medal(BaseModel):
    """One entry in the 'Medal Cabinet' section."""
    medal_type: str = Field(..., description="e.g. 'Olympics Gold', 'World Championship', 'Asian Games Gold'")
    competition: Optional[str] = None
    year: Optional[int] = None

    model_config = {"extra": "forbid"}


class SeasonStats(BaseModel):
    """Maps to the current-season summary card (e.g. '2026 Season')."""
    season_label: Optional[str] = Field(None, description="e.g. '2026 Season'")
    events: Optional[int] = None
    gold: Optional[int] = None
    silver: Optional[int] = None
    bronze: Optional[int] = None
    season_best: Optional[str] = None
    average_mark: Optional[str] = Field(None, description="average throw/time/score for the season")
    current_streak: Optional[str] = Field(None, description="e.g. '4 Podiums'")

    model_config = {"extra": "forbid"}


class PerformanceTrendPoint(BaseModel):
    """One point in the 'Performance Trend' (personal best progress) chart."""
    year: int
    value: str = Field(..., description="personal best mark for that year, e.g. '90.23 m'")

    model_config = {"extra": "forbid"}


class BaseAthleteProfile(BaseModel):
    """Fields common to every sport. Sport-specific pipelines subclass this."""
    athlete_id: str
    full_name: str
    sport: Sport
    nationality: str = "India"
    gender: Optional[str] = Field(
        None, description="e.g. 'Male' or 'Female' — used for filtering, not editorial content"
    )
    date_of_birth: Optional[date] = None
    current_team_or_federation: Optional[str] = None
    coach: Optional[str] = None
    current_ranking: Optional[int] = None
    career_highlights: list[CareerHighlight] = Field(default_factory=list)
    bio_summary: Optional[str] = Field(
        default=None, description="2-4 sentence factual summary, no editorializing"
    )
    quick_facts: Optional[QuickFacts] = None
    medal_cabinet: list[Medal] = Field(default_factory=list)
    season_stats: Optional[SeasonStats] = None
    performance_trend: list[PerformanceTrendPoint] = Field(default_factory=list)
    grounding_sources: list[str] = Field(
        default_factory=list,
        description="URLs Gemini's search grounding actually cited for this draft, for reviewer spot-checking",
    )
    source: SourceRef

    model_config = {"extra": "forbid"}  # reject any field Gemini invents


class TrackFieldProfile(BaseAthleteProfile):
    """
    Shared shape for all six track & field sub-disciplines (sprints,
    middle_distance, long_distance, throws, jumps, relays). `sport` has no
    default here since one class now maps to six different Sport values —
    the pipeline always sets data["sport"] explicitly before validation
    (see generate_athlete_content_llm.py), so a default would be unused
    and misleading.
    """
    event: Optional[str] = Field(None, description="e.g. 'Javelin Throw', '100m', '4x400m Relay'")
    personal_best: Optional[str] = None
    personal_best_date: Optional[date] = None
    season_best: Optional[str] = None


class CricketProfile(BaseAthleteProfile):
    sport: Sport = Sport.CRICKET
    role: Optional[str] = Field(None, description="batsman / bowler / all-rounder / wicketkeeper")
    batting_style: Optional[str] = None
    bowling_style: Optional[str] = None
    formats: list[str] = Field(default_factory=list)  # e.g. ["Test", "ODI", "T20I"]


class BadmintonProfile(BaseAthleteProfile):
    sport: Sport = Sport.BADMINTON
    discipline: Optional[str] = Field(None, description="singles / doubles / mixed doubles")
    world_ranking_points: Optional[int] = None


SPORT_SCHEMA_MAP: dict[Sport, type[BaseAthleteProfile]] = {
    Sport.SPRINTS: TrackFieldProfile,
    Sport.MIDDLE_DISTANCE: TrackFieldProfile,
    Sport.LONG_DISTANCE: TrackFieldProfile,
    Sport.THROWS: TrackFieldProfile,
    Sport.JUMPS: TrackFieldProfile,
    Sport.RELAYS: TrackFieldProfile,
    Sport.CRICKET: CricketProfile,
    Sport.BADMINTON: BadmintonProfile,
}


def get_schema_for_sport(sport: Sport) -> type[BaseAthleteProfile]:
    """Falls back to the shared base schema for sports without a dedicated subclass yet."""
    return SPORT_SCHEMA_MAP.get(sport, BaseAthleteProfile)


class ReviewDraft(BaseModel):
    """What actually gets written to the review-queue Firestore collection."""
    athlete_id: str
    sport: Sport
    trigger_type: TriggerType
    trigger_reason: str
    proposed_data: dict
    current_data: Optional[dict] = None
    source: SourceRef
    # FIX: was a required `str` with no default, but athlete_pipeline.py never
    # passes `fingerprint=` when constructing a ReviewDraft (Step 3 / the
    # fingerprint-skip check was dropped from the pipeline entirely — see
    # fingerprint.py, which is now dead code). A required field with nothing
    # ever supplying it meant every single ReviewDraft() call raised
    # ValidationError, so no draft could ever reach the review queue.
    # Made optional; firebase_store.resolve_review_draft() already treats
    # a missing/None fingerprint as fine (draft_data.get("fingerprint")).
    fingerprint: Optional[str] = None
    status: str = "pending_review"  # pending_review | approved | rejected | edited