"""
records_schemas.py

Pydantic schemas for the AI-powered Sport Records generation pipeline.
Mirrors athlete_schemas.py's approach: every field Gemini drafts must
validate against RecordEntry before it reaches the review queue.
"""

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class RecordSport(str, Enum):
    """Matches the SPORTS filter categories used on the records screen."""
    SPRINTS = "Sprints"
    HURDLES = "Hurdles"
    THROWS = "Throws"
    JUMPS = "Jumps"
    DISTANCE = "Distance"
    RELAY = "Relay"


class RecordType(str, Enum):
    GAMES_RECORD = "GR"
    NATIONAL_RECORD = "NR"
    WORLD_RECORD = "WR"
    CONTINENTAL_RECORD = "CR"
    PERSONAL_BEST = "PB"


class ProgressPoint(BaseModel):
    """One point on the record-progression chart."""
    year: str = Field(
        ..., description="e.g. '2022' — a chart label, kept as a string, not a date"
    )
    value: float = Field(
        ..., description="numeric mark for that year in the event's native unit (seconds, meters, points, etc.), no unit suffix"
    )

    model_config = {"extra": "forbid"}


class RecordEntry(BaseModel):
    """One entry in RECORDS_DATA — a broken record, its predecessor, and context."""
    id: str
    event: str = Field(..., description="e.g. \"Men's Javelin Throw\"")
    athlete: str = Field(..., description="athlete or team name, e.g. 'Neeraj Chopra' or 'Team Jamaica'")
    country: str
    flag: Optional[str] = Field(None, description="emoji flag, e.g. '🇮🇳'")
    result: str = Field(..., description="the new record mark, e.g. '88.13m' or '9.85s'")
    type: RecordType
    typeFull: str = Field(..., description="e.g. 'Games Record', 'World Record'")
    phase: Optional[str] = Field(None, description="e.g. 'Final', 'Semifinal', 'Heat 3'")
    sport: RecordSport
    date: str = Field(..., description="display date the NEW record was set, e.g. 'Aug 7, 2026'")
    city: str = Field(..., description="city/venue where the NEW record was set")
    image: Optional[str] = None
    prevRecord: str = Field(..., description="the mark being broken, e.g. '85.60m'")
    prevRecordPlace: str = Field(
        ..., description="city/venue where the PREVIOUS (old) record was set — distinct from `city`"
    )
    prevHolder: str = Field(
        ..., description="athlete/team who held the previous record, e.g. 'Julius Yego (KEN)'"
    )
    improvement: str = Field(..., description="signed margin, e.g. '+2.53m' or '-0.08s'")
    improvementDate: str = Field(
        ..., description="date the new record was actually broken — should match `date` unless known otherwise"
    )
    aiInsight: str = Field(
        ..., description="2-3 factual sentences on what made the performance notable, no unfounded editorializing"
    )
    progressData: list[ProgressPoint] = Field(
        default_factory=list,
        description="the athlete's progression in this event, up to the 6 most recent years",
    )
    grounding_sources: list[str] = Field(
        default_factory=list,
        description="URLs Gemini's search grounding actually cited for this draft, for reviewer spot-checking",
    )

    model_config = {"extra": "forbid"}