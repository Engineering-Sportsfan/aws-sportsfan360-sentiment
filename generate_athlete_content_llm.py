
# # """
# # generate_athlete_content_llm.py

# # Standalone content generator: prompts Gemini (with Google Search grounding
# # enabled) to draft a full athlete profile (bio, quick facts, medal cabinet,
# # season stats, performance trend) AND, in the same pass, a "record_highlight"
# # — the athlete's most notable record (previous record broken, where it was
# # set, who held it, the margin, the date) for the match-center record cards.
# # This is NOT a World Athletics / other licensed-source API integration —
# # Gemini searches the open web and its own knowledge, so results should still
# # be treated as a first-draft aid, not a verified source.

# # Everything lives in this one file on purpose — schemas, the generic
# # coercion engine, the prompt builder, and the CLI — after an earlier split
# # into separate athlete/record schema + script files led to files getting
# # saved under each other's names and a circular import. One file, one
# # `python generate_athlete_content_llm.py`, no cross-file import to get
# # wrong.

# # Run it, enter one athlete name (+ sport) at a time. Each run:
# #   1. builds a prompt asking Gemini for JSON matching BaseAthleteProfile's
# #      UI-facing fields (including record_highlight), with Google Search
# #      grounding turned on
# #   2. parses + validates the response against the schemas below
# #   3. if key fields (coach, current_ranking, season_stats,
# #      performance_trend, current_team_or_federation, record_highlight)
# #      still came back empty/null, runs up to 2 more targeted retry passes
# #      asking Gemini to search specifically for those fields, merging in
# #      anything newly found field-by-field (already-filled fields are never
# #      overwritten)
# #   4. prints the validated JSON and saves it to
# #      ./llm_athlete_drafts/<athlete_id>.json

# # NOTE: search-grounded LLM output can still be wrong or out of date — this is
# # a content-drafting aid for the review queue, not a source-of-truth stats
# # feed. Every draft still needs human review before publishing (source_name
# # below is set to "Gemini (search-grounded — unverified)" for that reason).
# # """

# # import json
# # import os
# # import re
# # import sys
# # import typing
# # from datetime import date as date_type, datetime, timezone
# # from enum import Enum
# # from typing import Optional

# # from google import genai
# # from google.genai import types
# # from pydantic import BaseModel, Field, HttpUrl, ValidationError


# # # ═══════════════════════════════════════════════════════════════════════
# # # SCHEMAS (formerly athlete_schemas.py + records_schemas.py — merged here
# # # so there's only ever one file to edit and one to run)
# # # ═══════════════════════════════════════════════════════════════════════

# # class Sport(str, Enum):
# #     SPRINTS = "sprints"
# #     MIDDLE_DISTANCE = "middle_distance"
# #     LONG_DISTANCE = "long_distance"
# #     THROWS = "throws"
# #     JUMPS = "jumps"
# #     RELAYS = "relays"
# #     CRICKET = "cricket"
# #     BADMINTON = "badminton"
# #     HOCKEY = "hockey"
# #     F1 = "f1"
# #     KABADDI = "kabaddi"
# #     BASKETBALL = "basketball"
# #     FOOTBALL = "football"
# #     TENNIS = "tennis"
# #     VOLLEYBALL = "volleyball"
# #     OTHER = "other"


# # class TriggerType(str, Enum):
# #     NEW_ATHLETE_ONBOARDING = "new_athlete_onboarding"
# #     SCHEDULED_RECHECK = "scheduled_recheck"
# #     MANUAL_RECHECK = "manual_recheck"


# # class SourceRef(BaseModel):
# #     """
# #     Provenance for every draft. With no single licensed source anymore,
# #     this now points at what Gemini's search grounding actually cited
# #     (see grounding_sources on BaseAthleteProfile) rather than one fixed
# #     site. source_url is optional since grounded search may return zero
# #     citable URLs for some fields/athletes -- editors then rely on
# #     grounding_sources for spot-checking instead of a single link.
# #     """
# #     source_name: str = Field(default="Gemini (Google Search grounding)")
# #     source_url: Optional[HttpUrl] = None
# #     fetched_at: str  # ISO8601 timestamp, set by the pipeline


# # class CareerHighlight(BaseModel):
# #     title: str
# #     event: Optional[str] = None
# #     year: Optional[int] = None


# # class QuickFacts(BaseModel):
# #     """Maps to the 'Quick Facts' card on the profile UI."""
# #     age: Optional[int] = None
# #     height: Optional[str] = Field(None, description="e.g. '1.86 m'")
# #     weight: Optional[str] = Field(None, description="e.g. '86 kg'")
# #     birthplace: Optional[str] = None
# #     dominant_hand: Optional[str] = None
# #     personal_best: Optional[str] = Field(None, description="e.g. '90.23 m' or '3:37.87'")
# #     years_active: Optional[str] = Field(None, description="e.g. '2016' or '2016-present'")
# #     coach: Optional[str] = None
# #     honors: list[str] = Field(
# #         default_factory=list,
# #         description="e.g. ['Olympic Champion', 'World Champion', 'Asian Champion', 'CWG Gold', 'Diamond League Winner']",
# #     )

# #     model_config = {"extra": "forbid"}


# # class Medal(BaseModel):
# #     """One entry in the 'Medal Cabinet' section."""
# #     medal_type: str = Field(..., description="e.g. 'Olympics Gold', 'World Championship', 'Asian Games Gold'")
# #     competition: Optional[str] = None
# #     year: Optional[int] = None

# #     model_config = {"extra": "forbid"}


# # class SeasonStats(BaseModel):
# #     """Maps to the current-season summary card (e.g. '2026 Season')."""
# #     season_label: Optional[str] = Field(None, description="e.g. '2026 Season'")
# #     events: Optional[int] = None
# #     gold: Optional[int] = None
# #     silver: Optional[int] = None
# #     bronze: Optional[int] = None
# #     season_best: Optional[str] = None
# #     average_mark: Optional[str] = Field(None, description="average throw/time/score for the season")
# #     current_streak: Optional[str] = Field(None, description="e.g. '4 Podiums'")

# #     model_config = {"extra": "forbid"}


# # class PerformanceTrendPoint(BaseModel):
# #     """One point in the 'Performance Trend' (personal best progress) chart."""
# #     year: int
# #     value: str = Field(..., description="personal best mark for that year, e.g. '90.23 m'")

# #     model_config = {"extra": "forbid"}


# # class ProgressPoint(BaseModel):
# #     """One point in record_highlight's progression chart — a numeric
# #     sibling of PerformanceTrendPoint, used for the match-center record
# #     card rather than the profile's performance-trend chart."""
# #     year: str = Field(..., description="e.g. '2022' — a chart label, kept as a string, not a date")
# #     value: float = Field(
# #         ..., description="numeric mark for that year in the record's native unit (seconds, meters, points), no unit suffix"
# #     )

# #     model_config = {"extra": "forbid"}


# # class RecordHighlight(BaseModel):
# #     """
# #     The athlete's most notable record for the match-center record card —
# #     NOT every record they've ever set, just the single most significant
# #     one (e.g. a still-standing Games/National/World Record). Null if this
# #     athlete doesn't currently hold a notable record worth featuring there.
# #     """
# #     event: Optional[str] = Field(None, description="e.g. \"Men's Javelin Throw\"")
# #     result: Optional[str] = Field(None, description="the record mark, e.g. '88.13m' or '9.85s'")
# #     type: Optional[str] = Field(
# #         None, description="one of: GR (Games Record), NR (National Record), WR (World Record), CR (Continental Record), PB (Personal Best)"
# #     )
# #     typeFull: Optional[str] = Field(None, description="e.g. 'Games Record', 'World Record'")
# #     phase: Optional[str] = Field(None, description="e.g. 'Final', 'Semifinal'")
# #     date: Optional[str] = Field(None, description="display date the record was set, e.g. 'Aug 7, 2026'")
# #     city: Optional[str] = Field(None, description="city/venue where THIS record was set")
# #     prevRecord: Optional[str] = Field(None, description="the mark this record broke, e.g. '85.60m'")
# #     prevRecordPlace: Optional[str] = Field(
# #         None, description="city/venue where the PREVIOUS (old) record was set — distinct from `city`"
# #     )
# #     prevHolder: Optional[str] = Field(
# #         None, description="athlete/team who held the previous record, e.g. 'Julius Yego (KEN)'"
# #     )
# #     improvement: Optional[str] = Field(None, description="signed margin, e.g. '+2.53m' or '-0.08s'")
# #     improvementDate: Optional[str] = Field(
# #         None, description="date the record was actually broken — should match `date` unless known otherwise"
# #     )
# #     aiInsight: Optional[str] = Field(
# #         None, description="2-3 factual sentences on what made the performance notable, no unfounded editorializing"
# #     )
# #     progressData: list[ProgressPoint] = Field(
# #         default_factory=list,
# #         description="the athlete's progression toward this record, up to the 6 most recent years; [] if not applicable",
# #     )

# #     model_config = {"extra": "forbid"}


# # class BaseAthleteProfile(BaseModel):
# #     """Fields common to every sport. Sport-specific pipelines subclass this."""
# #     athlete_id: str
# #     full_name: str
# #     sport: Sport
# #     nationality: str = "India"
# #     gender: Optional[str] = Field(
# #         None, description="e.g. 'Male' or 'Female' — used for filtering, not editorial content"
# #     )
# #     date_of_birth: Optional[date_type] = None
# #     current_team_or_federation: Optional[str] = None
# #     coach: Optional[str] = None
# #     current_ranking: Optional[int] = None
# #     career_highlights: list[CareerHighlight] = Field(default_factory=list)
# #     bio_summary: Optional[str] = Field(
# #         default=None, description="2-4 sentence factual summary, no editorializing"
# #     )
# #     quick_facts: Optional[QuickFacts] = None
# #     medal_cabinet: list[Medal] = Field(default_factory=list)
# #     season_stats: Optional[SeasonStats] = None
# #     performance_trend: list[PerformanceTrendPoint] = Field(default_factory=list)
# #     record_highlight: Optional[RecordHighlight] = Field(
# #         None,
# #         description="the athlete's single most notable record, for the match-center record card — null if none",
# #     )
# #     grounding_sources: list[str] = Field(
# #         default_factory=list,
# #         description="URLs Gemini's search grounding actually cited for this draft, for reviewer spot-checking",
# #     )
# #     source: SourceRef

# #     model_config = {"extra": "forbid"}  # reject any field Gemini invents


# # class TrackFieldProfile(BaseAthleteProfile):
# #     """
# #     Shared shape for all six track & field sub-disciplines (sprints,
# #     middle_distance, long_distance, throws, jumps, relays). `sport` has no
# #     default here since one class now maps to six different Sport values —
# #     the pipeline always sets data["sport"] explicitly before validation,
# #     so a default would be unused and misleading.
# #     """
# #     event: Optional[str] = Field(None, description="e.g. 'Javelin Throw', '100m', '4x400m Relay'")
# #     personal_best: Optional[str] = None
# #     personal_best_date: Optional[date_type] = None
# #     season_best: Optional[str] = None


# # class CricketProfile(BaseAthleteProfile):
# #     sport: Sport = Sport.CRICKET
# #     role: Optional[str] = Field(None, description="batsman / bowler / all-rounder / wicketkeeper")
# #     batting_style: Optional[str] = None
# #     bowling_style: Optional[str] = None
# #     formats: list[str] = Field(default_factory=list)  # e.g. ["Test", "ODI", "T20I"]


# # class BadmintonProfile(BaseAthleteProfile):
# #     sport: Sport = Sport.BADMINTON
# #     discipline: Optional[str] = Field(None, description="singles / doubles / mixed doubles")
# #     world_ranking_points: Optional[int] = None


# # SPORT_SCHEMA_MAP: dict[Sport, type[BaseAthleteProfile]] = {
# #     Sport.SPRINTS: TrackFieldProfile,
# #     Sport.MIDDLE_DISTANCE: TrackFieldProfile,
# #     Sport.LONG_DISTANCE: TrackFieldProfile,
# #     Sport.THROWS: TrackFieldProfile,
# #     Sport.JUMPS: TrackFieldProfile,
# #     Sport.RELAYS: TrackFieldProfile,
# #     Sport.CRICKET: CricketProfile,
# #     Sport.BADMINTON: BadmintonProfile,
# # }


# # def get_schema_for_sport(sport: Sport) -> type[BaseAthleteProfile]:
# #     """Falls back to the shared base schema for sports without a dedicated subclass yet."""
# #     return SPORT_SCHEMA_MAP.get(sport, BaseAthleteProfile)


# # class ReviewDraft(BaseModel):
# #     """What actually gets written to the review-queue Firestore collection."""
# #     athlete_id: str
# #     sport: Sport
# #     trigger_type: TriggerType
# #     trigger_reason: str
# #     proposed_data: dict
# #     current_data: Optional[dict] = None
# #     source: SourceRef
# #     fingerprint: str
# #     status: str = "pending_review"  # pending_review | approved | rejected | edited


# # # ═══════════════════════════════════════════════════════════════════════
# # # GENERATION PIPELINE
# # # ═══════════════════════════════════════════════════════════════════════

# # # ── Gemini Client Setup ──────────────────────────────────────────────
# # api_key = os.getenv("GEMINI_API_KEY")
# # if api_key:
# #     client = genai.Client(api_key=api_key)
# # else:
# #     client = genai.Client(
# #         vertexai=True,
# #         project=os.getenv("GCP_PROJECT_ID", "fleet-gift-498306-p7"),
# #         location=os.getenv("GCP_LOCATION", "us-central1"),
# #     )

# # GEMINI_MODEL = os.getenv("ATHLETE_EXTRACTION_MODEL", "gemini-2.5-flash")

# # OUTPUT_DIR = "llm_athlete_drafts"

# # _VALID_RECORD_TYPES = {"GR", "NR", "WR", "CR", "PB"}


# # class GenerationError(Exception):
# #     pass


# # # ── Field applicability map ──────────────────────────────────────────────
# # # Per sport: "na" fields structurally don't apply to that sport and should
# # # come back as an explicit "N/A" string rather than an ambiguous null.
# # # "push" fields DO apply and have a real, findable answer — Gemini should
# # # search harder for these rather than giving up and returning null.
# # #
# # # Dotted paths refer to nested fields (e.g. "quick_facts.dominant_hand",
# # # "season_stats.current_streak"). Bare names refer to top-level fields
# # # (e.g. "current_ranking", "performance_trend").
# # FIELD_APPLICABILITY: dict[Sport, dict[str, list[str]]] = {
# #     # All six track & field sub-disciplines share TrackFieldProfile's shape
# #     # and the same applicability rules the old single TRACK_FIELD entry had.
# #     Sport.SPRINTS: {
# #         "na": ["season_stats.current_streak"],
# #         "push": [
# #             "current_ranking",
# #             "personal_best",
# #             "season_stats.season_best",
# #             "season_stats.average_mark",
# #             "performance_trend",
# #         ],
# #     },
# #     Sport.MIDDLE_DISTANCE: {
# #         "na": ["season_stats.current_streak"],
# #         "push": [
# #             "current_ranking",
# #             "personal_best",
# #             "season_stats.season_best",
# #             "season_stats.average_mark",
# #             "performance_trend",
# #         ],
# #     },
# #     Sport.LONG_DISTANCE: {
# #         "na": ["season_stats.current_streak"],
# #         "push": [
# #             "current_ranking",
# #             "personal_best",
# #             "season_stats.season_best",
# #             "season_stats.average_mark",
# #             "performance_trend",
# #         ],
# #     },
# #     Sport.THROWS: {
# #         "na": ["season_stats.current_streak"],
# #         "push": [
# #             "current_ranking",
# #             "personal_best",
# #             "season_stats.season_best",
# #             "season_stats.average_mark",
# #             "performance_trend",
# #         ],
# #     },
# #     Sport.JUMPS: {
# #         "na": ["season_stats.current_streak"],
# #         "push": [
# #             "current_ranking",
# #             "personal_best",
# #             "season_stats.season_best",
# #             "season_stats.average_mark",
# #             "performance_trend",
# #         ],
# #     },
# #     Sport.RELAYS: {
# #         "na": ["season_stats.current_streak"],
# #         "push": [
# #             "current_ranking",
# #             "personal_best",
# #             "season_stats.season_best",
# #             "season_stats.average_mark",
# #             "performance_trend",
# #         ],
# #     },
# #     Sport.CRICKET: {
# #         "na": ["performance_trend", "season_stats.current_streak"],
# #         "push": ["current_team_or_federation", "season_stats.season_best"],
# #     },
# #     Sport.HOCKEY: {
# #         "na": [
# #             "personal_best",
# #             "performance_trend",
# #             "season_stats.current_streak",
# #         ],
# #         "push": ["current_team_or_federation", "season_stats.season_best"],
# #     },
# #     Sport.BADMINTON: {
# #         "na": ["performance_trend", "season_stats.current_streak"],
# #         "push": ["current_ranking", "medal_cabinet"],
# #     },
# #     Sport.F1: {
# #         "na": [
# #             "personal_best",
# #             "season_stats.average_mark",
# #             "performance_trend",
# #         ],
# #         "push": ["current_ranking", "current_team_or_federation", "season_stats.season_best"],
# #     },
# #     Sport.KABADDI: {
# #         "na": [
# #             "personal_best",
# #             "season_stats.average_mark",
# #             "season_stats.current_streak",
# #         ],
# #         "push": ["current_team_or_federation", "performance_trend", "medal_cabinet", "season_stats.season_best"],
# #     },
# #     Sport.BASKETBALL: {
# #         "na": ["personal_best", "performance_trend", "season_stats.current_streak"],
# #         "push": ["current_team_or_federation", "season_stats.season_best"],
# #     },
# #     Sport.FOOTBALL: {
# #         "na": ["personal_best", "performance_trend", "season_stats.current_streak"],
# #         "push": ["current_team_or_federation", "season_stats.season_best"],
# #     },
# #     # PLACEHOLDER — styled like badminton (individual ranking + medal
# #     # cabinet) per user's "decide later" call; revisit once tennis/
# #     # volleyball data needs are actually scoped out.
# #     Sport.TENNIS: {
# #         "na": ["performance_trend", "season_stats.current_streak"],
# #         "push": ["current_ranking", "medal_cabinet"],
# #     },
# #     # PLACEHOLDER — same as tennis for now; volleyball is more naturally a
# #     # team sport (current_team_or_federation) so this will likely need to
# #     # move to that style once decided.
# #     Sport.VOLLEYBALL: {
# #         "na": ["performance_trend", "season_stats.current_streak"],
# #         "push": ["current_ranking", "medal_cabinet"],
# #     },
# #     Sport.OTHER: {
# #         # Covers individual "mark" sports like weightlifting, long jump, etc.
# #         "na": ["season_stats.current_streak"],
# #         "push": [
# #             "current_ranking",
# #             "personal_best",
# #             "season_stats.season_best",
# #             "performance_trend",
# #         ],
# #     },
# # }

# # _DEFAULT_APPLICABILITY = {"na": ["season_stats.current_streak"], "push": []}

# # # gender applies to every sport (it's on BaseAthleteProfile) and is needed
# # # for filtering, so it's pushed for all sports here rather than repeating
# # # it in each sport's entry above.
# # _ALWAYS_PUSH_FIELDS = ["gender"]

# # # event (e.g. "Long Jump", "100m", "4x400m Relay") only exists on
# # # TrackFieldProfile, and is exactly the field that distinguishes disciplines
# # # within a Sport like "jumps" or "sprints" for filtering — push it for
# # # those sports specifically rather than repeating it six times above.
# # _TRACK_FIELD_SPORTS = {
# #     Sport.SPRINTS,
# #     Sport.MIDDLE_DISTANCE,
# #     Sport.LONG_DISTANCE,
# #     Sport.THROWS,
# #     Sport.JUMPS,
# #     Sport.RELAYS,
# # }


# # def _applicability(sport: Sport) -> dict:
# #     app = FIELD_APPLICABILITY.get(sport, _DEFAULT_APPLICABILITY)
# #     # "personal_best" means both the top-level field (TrackFieldProfile
# #     # only) AND quick_facts.personal_best (every sport) — expand it here
# #     # so callers only have to list it once per sport.
# #     na = list(app["na"])
# #     if "personal_best" in na and "quick_facts.personal_best" not in na:
# #         na.append("quick_facts.personal_best")

# #     push = list(app["push"]) + _ALWAYS_PUSH_FIELDS
# #     if sport in _TRACK_FIELD_SPORTS and "event" not in push:
# #         push.append("event")

# #     return {"na": na, "push": push}


# # # ── Retry-fill config ─────────────────────────────────────────────────────
# # # Fields where a null/empty result after the first pass is worth a second,
# # # more targeted search — separate from FIELD_APPLICABILITY, which says
# # # whether a field applies to a sport at all. This says "if it applies and
# # # still came back empty, chase it again with a narrower prompt."
# # #
# # # "coach" is special-cased throughout since it actually lives at
# # # quick_facts.coach, not top-level. "record_highlight" is checked as a
# # # whole (present but empty dict/None) — it's an all-or-nothing sub-object.
# # RETRYABLE_FIELDS = [
# #     "coach",
# #     "honors",
# #     "current_ranking",
# #     "season_stats",
# #     "performance_trend",
# #     "current_team_or_federation",
# #     "record_highlight",
# # ]


# # def _is_empty(value) -> bool:
# #     if value is None:
# #         return True
# #     if isinstance(value, str):
# #         return value.strip() == "" or value.strip().upper() == "N/A"
# #     if isinstance(value, (list, dict)):
# #         return len(value) == 0
# #     return False


# # def _slugify(name: str) -> str:
# #     return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


# # def _sport_specific_guidance(sport: Sport) -> str:
# #     """
# #     Extra per-sport notes so Gemini doesn't waste effort guessing at fields
# #     that structurally don't apply to a sport, and instead double-checks the
# #     fields that DO have a real answer but are commonly missed.
# #     """
# #     app = _applicability(sport)

# #     na_lines = "\n".join(
# #         f"    - {field}: does NOT apply to {sport.value} — return the "
# #         f'string "N/A" for it, not null.'
# #         for field in app["na"]
# #     )
# #     push_lines = "\n".join(
# #         f"    - {field}: this DOES apply to {sport.value} and usually has a "
# #         f"real, findable answer — search specifically for it before "
# #         f"resorting to null."
# #         for field in app["push"]
# #     )

# #     sections = []
# #     if na_lines:
# #         sections.append(f"    Fields that do NOT apply to {sport.value}:\n{na_lines}")
# #     if push_lines:
# #         sections.append(f"    Fields to search harder for:\n{push_lines}")

# #     return "\n\n".join(sections) + """

# #     General note for ALL sports:
# #     - quick_facts.coach MUST be just the coach's plain name (e.g. "Jaspal
# #       Rana"), never a name with a parenthetical annotation like "(former
# #       coach, deceased)" or "(personal)" attached. If the coach has changed
# #       or there's noteworthy context, mention that in bio_summary instead,
# #       and put only the CURRENT coach's name (or null if unknown/none) in
# #       quick_facts.coach.
# #     """


# # def _build_prompt(sport: Sport, athlete_name: str, focus_fields: list[str] | None = None) -> str:
# #     schema_cls = get_schema_for_sport(sport)
# #     # Fields the LLM should fill — source is set by this script, not the LLM.
# #     field_names = [
# #         name for name in schema_cls.model_fields.keys() if name != "source"
# #     ]

# #     base = f"""
# # You are drafting a structured athlete profile for SportsFan360, a sports fan
# # engagement platform. You have access to Google Search — use it to look up
# # current, accurate facts about this athlete rather than relying only on what
# # you already know. Search for their official bio, current stats, medal
# # history, and recent season results before answering.

# # Athlete name: {athlete_name}
# # Sport: {sport.value}

# # Fill in as many of the following fields as you can find reliable, verifiable
# # information for via search. Use null (JSON null) only for fields you could
# # not find or confirm after searching — do NOT guess, invent, or approximate
# # numbers you are unsure of. Accuracy matters more than completeness; a human
# # editor will review and correct this draft before it goes live.

# # Fields to produce (use exactly these JSON keys): {field_names}

# # Field notes:
# # - career_highlights: list of OBJECTS, each shaped exactly like
# #   {{"title": "Olympic Gold Medalist", "event": "Javelin Throw", "year": 2021}}
# #   — never a plain string. "event" and "year" may be null if unknown, but
# #   "title" is required and each entry must be an object, not free text.
# #   Include at most the 6 MOST SIGNIFICANT highlights (major international
# #   medals/titles) — do not list every minor competition result.
# # - quick_facts: object with age, height, weight, birthplace, dominant_hand,
# #   personal_best, years_active, coach, honors (list of title strings).
# #   personal_best MUST be a single plain string, e.g. "90.23 m" or
# #   "3:37.87" or "205 kg total (Snatch 88kg + Clean & Jerk 117kg)" — never
# #   a nested object, even for multi-component marks like weightlifting.
# # - medal_cabinet: list of objects {{medal_type, competition, year}}. Include
# #   at most the 8 MOST SIGNIFICANT medals (major international competitions:
# #   Olympics, World Championships, Asian Games, Commonwealth Games, Diamond
# #   League, etc.) — do not list every minor/domestic competition result.
# # - season_stats: object with season_label (e.g. "2026 Season"), events, gold,
# #   silver, bronze, season_best, average_mark, current_streak
# # - performance_trend: list of objects {{year, value}} showing personal-best
# #   progression by year. Include at most the 6 most recent years. If this
# #   sport/discipline has no single measurable "mark" to trend (e.g. combat
# #   sports, team sports), return an empty list [] rather than null.
# # - record_highlight: this is for the match-center RECORD CARD, separate from
# #   the profile above. Search specifically for whether this athlete currently
# #   holds (or recently set) a notable, still-relevant record — a Games
# #   Record, National Record, World Record, or Continental Record. If they do,
# #   fill the object: {{event, result, type, typeFull, phase, date, city,
# #   prevRecord, prevRecordPlace, prevHolder, improvement, improvementDate,
# #   aiInsight, progressData}}.
# #     - type MUST be exactly one of: "GR", "NR", "WR", "CR", "PB"
# #     - result / prevRecord / improvement MUST be plain strings in the
# #       record's native unit (e.g. "88.13m", "9.85s", "+2.53m", "-0.08s")
# #     - prevRecordPlace is the city/venue where the OLD record was set —
# #       distinct from `city`, which is where THIS record was set
# #     - prevHolder is the athlete/team who held the record before this one
# #     - improvementDate is the date the record was actually broken (usually
# #       the same as `date`)
# #     - progressData: list of {{"year": "2022", "value": 88.13}} objects
# #       (value as a plain number, no unit suffix), up to 6 most recent years,
# #       [] if not applicable
# #     - aiInsight: 2-3 factual sentences on what made the performance
# #       notable, no unfounded editorializing
# #     - If this athlete does NOT currently hold or hasn't recently set a
# #       notable record, set record_highlight to null entirely (not an
# #       object with all-null fields).
# # - If a list field has no data, use an empty list [], never null and never
# #   the string "N/A" — "N/A" only ever applies to single-value (scalar)
# #   fields that are explicitly called out as not applicable below, never to
# #   a list field like career_highlights, medal_cabinet, or performance_trend.
# # - current_ranking MUST be a plain integer (e.g. 1), never a string like
# #   "World No. 1" — if you only know a description, extract just the number,
# #   or use null if there is no clean number.
# # - quick_facts.years_active MUST be a string like "2016" or "2016-present",
# #   never a bare number of years.
# # - season_stats.events MUST be a plain integer count, never a list of
# #   competition names.
# # - bio_summary: 2-4 factual sentences, no editorializing
# # - gender: MUST be a plain string, "Male" or "Female" — this is used for
# #   filtering athletes on the site, not for any editorial content.
# # - event (track & field sports only): the specific discipline within this
# #   Sport category, e.g. for sport "jumps" use "Long Jump", "High Jump",
# #   "Triple Jump", or "Pole Vault"; for sport "sprints" use "100m", "200m",
# #   "400m", or "400m Hurdles"; for sport "relays" use "4x100m Relay" or
# #   "4x400m Relay". This is also used for filtering, so it must be filled
# #   with the athlete's actual specific event, never left null or generic.
# # {_sport_specific_guidance(sport)}
# # Respond with ONLY a single JSON object containing these fields. No prose,
# # no markdown code fences, no commentary before or after the JSON.
# # """

# #     if not focus_fields:
# #         return base

# #     focus_list = ", ".join(focus_fields)
# #     return base + f"""

# # IMPORTANT — TARGETED RETRY:
# # A previous search pass could not find reliable data for these specific
# # fields: {focus_list}

# # For THIS pass, prioritize finding data for exactly these fields. Try
# # different search angles than a generic profile search would use, for
# # example:
# # - For season_stats / performance_trend: search for the athlete's league
# #   or federation stats page directly (e.g. "<athlete name> <league>
# #   season stats", "<athlete name> career statistics site:<known stats
# #   site>"), or recent season recap articles.
# # - For coach: search "<athlete name> coach" and recent interview or
# #   profile articles that name training staff.
# # - For current_ranking: search "<athlete name> world ranking <year>" or
# #   the relevant federation's ranking page.
# # - For current_team_or_federation: search for the athlete's current club/
# #   team roster or national federation page.
# # - For record_highlight: search "<athlete name> record" or "<athlete
# #   name> <event> Games Record / National Record / World Record", plus
# #   the record it broke — "<event> record before <year>".

# # Only return null for a field in this list if, after genuinely attempting
# # these searches, no reliable source has the information. Do not give up
# # after a single generic search.

# # Still return the FULL schema (all fields), not just these — reuse
# # whatever solid data you already have for the rest.
# # """


# # def _extract_json(raw_text: str, response_meta=None) -> dict:
# #     raw = (raw_text or "").strip()
# #     start = raw.find("{")
# #     end = raw.rfind("}") + 1
# #     if start == -1 or end == 0:
# #         raise GenerationError(f"Gemini response did not contain valid JSON: {raw[:300]}")
# #     try:
# #         return json.loads(raw[start:end])
# #     except json.JSONDecodeError as e:
# #         hint = ""
# #         try:
# #             candidates = getattr(response_meta, "candidates", None) or []
# #             finish_reason = getattr(candidates[0], "finish_reason", None) if candidates else None
# #             if finish_reason and str(finish_reason).upper() != "STOP":
# #                 hint = f" (finish_reason={finish_reason} — likely truncated, response may have exceeded max_output_tokens)"
# #         except Exception:
# #             pass
# #         raise GenerationError(
# #             f"Gemini response was not valid JSON: {e}{hint} [response length: {len(raw)} chars]"
# #         ) from e


# # # ── Generic schema-driven coercion engine ────────────────────────────────
# # # Every bug found so far (career_highlights as strings, null lists, nested
# # # personal_best dicts, "World No. 1" instead of an int, numeric performance
# # # scores instead of strings, an extra "event" key on medal_cabinet, a bare-
# # # year date crashing validation, etc.) is really the same handful of
# # # *shape mismatches* recurring on different fields as new sports/fields get
# # # tried. Rather than hand-listing each one as we discover it (which only
# # # protects fields we've already seen break), this walks the actual Pydantic
# # # schema and coerces every field toward the type it expects, generically —
# # # so record_highlight (and any future nested addition) gets the same
# # # protection automatically instead of needing a new patch.
# # #
# # # This runs BEFORE the smaller domain-specific fixes below (coach-name
# # # stripping, medal event-key folding, record-type normalization, N/A
# # # applicability) — those add meaning on top of shapes this pass has
# # # already made safe to touch.

# # def _unwrap_optional(annotation):
# #     """Optional[X] / Union[X, None] -> (X, True). Anything else -> (annotation, False)."""
# #     origin = typing.get_origin(annotation)
# #     if origin is typing.Union:
# #         args = [a for a in typing.get_args(annotation) if a is not type(None)]
# #         if len(args) == 1:
# #             return args[0], True
# #     return annotation, False


# # def _is_model(tp) -> bool:
# #     return isinstance(tp, type) and issubclass(tp, BaseModel)


# # def _generic_coerce_scalar(value, target_type):
# #     """Best-effort coercion of a single value toward target_type (str/int/
# #     float/bool). Never raises — falls back to returning value unchanged if
# #     it can't confidently coerce, so schema validation (not this function)
# #     is always the final word on whether the result is acceptable."""
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
# #             # e.g. season_stats.events given a list of competition names
# #             # instead of a count — the count of entries is the sane reading.
# #             return len(value)
# #         if isinstance(value, dict):
# #             # e.g. current_ranking given as a per-event breakdown, like
# #             # {"10m Air Pistol Women": 3, "25m Pistol Women": 10} instead
# #             # of one number (seen for Manu Bhaker, shooting). "Current
# #             # ranking" reads most naturally as the best (lowest) rank
# #             # across events, so extract that rather than dropping the
# #             # field or crashing.
# #             nums = [v for v in value.values() if isinstance(v, (int, float)) and not isinstance(v, bool)]
# #             return int(min(nums)) if nums else None
# #         return value
# #     if target_type is float:
# #         if isinstance(value, (int, float)) and not isinstance(value, bool):
# #             return float(value)
# #         if isinstance(value, str):
# #             match = re.search(r"-?\d+\.?\d*", value)
# #             return float(match.group()) if match else None
# #         return value
# #     return value


# # def _coerce_to_model(data, model_cls, na_fields=None, path=""):
# #     """
# #     Recursively coerces a raw dict toward model_cls's field shapes:
# #       - list fields: null or a stray string (e.g. "N/A") -> []; each item
# #         coerced toward the list's declared item type (nested model or
# #         scalar); a bare string item for a model list is wrapped using the
# #         item model's first required string field (e.g. career_highlights
# #         strings -> {"title": ...})
# #       - nested model fields: recurse with the same rules (this is how
# #         record_highlight and its progressData get the same protection as
# #         quick_facts/season_stats/medal_cabinet automatically)
# #       - scalar str/int/float fields: coerced via _generic_coerce_scalar;
# #         a still-null scalar becomes "N/A" if its dotted path is in
# #         na_fields (the sport's applicability map), otherwise stays null
# #       - date-typed fields: a malformed/partial date string (e.g. a bare
# #         year) is nulled out rather than crashing Pydantic's strict date
# #         validation
# #       - keys not defined on model_cls are dropped here (extra="forbid"
# #         would otherwise crash validation) — domain-specific folding of a
# #         dropped key's value (e.g. medal_cabinet's "event") happens in the
# #         pass after this one, before information is lost for those fields
# #     Unknown/unhandled types are passed through untouched and left for
# #     Pydantic to accept or reject.
# #     """
# #     if not isinstance(data, dict):
# #         return data
# #     na_fields = na_fields or set()

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
# #                 coerced_items = []
# #                 for item in value:
# #                     if isinstance(item, str):
# #                         wrap_field = next(
# #                             (
# #                                 fn
# #                                 for fn, f2 in item_type.model_fields.items()
# #                                 if f2.is_required() and _unwrap_optional(f2.annotation)[0] is str
# #                             ),
# #                             None,
# #                         )
# #                         item = {wrap_field: item} if wrap_field else None
# #                     if isinstance(item, dict):
# #                         coerced_items.append(
# #                             _coerce_to_model(item, item_type, na_fields=na_fields, path=field_path)
# #                         )
# #                 result[name] = coerced_items
# #             else:
# #                 result[name] = [
# #                     _generic_coerce_scalar(v, item_type) if item_type in (str, int, float) else v
# #                     for v in value
# #                     if v is not None
# #                 ]
# #             continue

# #         if _is_model(annotation):
# #             if isinstance(value, str):
# #                 # e.g. record_highlight (or any nested-model field) coming
# #                 # back as the literal string "N/A" or similar instead of
# #                 # an object/null — treat it the same as null rather than
# #                 # crashing on "expected dict, got str".
# #                 value = None
# #             result[name] = (
# #                 _coerce_to_model(value, annotation, na_fields=na_fields, path=field_path)
# #                 if isinstance(value, dict)
# #                 else value
# #             )
# #             continue

# #         if annotation in (str, int, float):
# #             coerced = _generic_coerce_scalar(value, annotation)
# #             if coerced is None and annotation is str:
# #                 if field_path in na_fields:
# #                     coerced = "N/A"
# #                 elif field.is_required():
# #                     # A required string field can never validate as None —
# #                     # crashing the whole draft over one unfilled required
# #                     # field (e.g. a performance_trend point with a known
# #                     # year but no confirmed mark) loses everything Gemini
# #                     # DID find. "N/A" preserves the entry instead.
# #                     coerced = "N/A"
# #             result[name] = coerced
# #             continue

# #         if annotation is date_type:
# #             if isinstance(value, str):
# #                 try:
# #                     date_type.fromisoformat(value)
# #                     result[name] = value
# #                 except ValueError:
# #                     # Gemini sometimes returns a bare year ("2023") or
# #                     # otherwise malformed/partial date instead of full
# #                     # ISO YYYY-MM-DD (seen on personal_best_date for
# #                     # Murali Sreeshankar) — Pydantic's date_from_datetime
# #                     # validation crashes the WHOLE draft over this one
# #                     # decorative field. Null it out instead: the year is
# #                     # already captured in performance_trend/career_highlights,
# #                     # so nothing load-bearing is lost.
# #                     result[name] = None
# #             else:
# #                 result[name] = value
# #             continue

# #         # bools, HttpUrl, enums, etc. — leave for Pydantic to validate directly
# #         result[name] = value

# #     return result


# # def _normalize_record_type(record_highlight: Optional[dict]) -> Optional[dict]:
# #     """
# #     record_highlight.type is free-typed by Gemini (unlike `sport`, which
# #     the pipeline sets explicitly), so it can come back with different
# #     casing or the full name instead of the code (e.g. "world record" or
# #     "World Record" instead of "WR"). Matches case-insensitively against
# #     the known codes/names before validation rather than letting a
# #     close-but-not-exact string fail silently or crash.
# #     """
# #     if not isinstance(record_highlight, dict):
# #         return record_highlight
# #     value = record_highlight.get("type")
# #     if not isinstance(value, str):
# #         return record_highlight
# #     normalized = value.strip().upper()
# #     if normalized in _VALID_RECORD_TYPES:
# #         record_highlight["type"] = normalized
# #         return record_highlight
# #     name_map = {
# #         "GAMES RECORD": "GR",
# #         "NATIONAL RECORD": "NR",
# #         "WORLD RECORD": "WR",
# #         "CONTINENTAL RECORD": "CR",
# #         "PERSONAL BEST": "PB",
# #     }
# #     record_highlight["type"] = name_map.get(normalized, value)
# #     return record_highlight


# # def _sanitize(data: dict) -> dict:
# #     """
# #     Domain-specific fixes that add real meaning, not just fix shape — these
# #     stay hand-written because they require judgment the generic coercion
# #     engine above shouldn't guess at:
# #       - coach parenthetical stripping ("(former coach, deceased)" doesn't
# #         belong in a plain-name field, but shouldn't be silently discarded
# #         either — the general prompt guidance already asks Gemini to put
# #         that context in bio_summary instead)
# #       - medal_cabinet's extra "event" key: Medal's schema forbids it, but
# #         the value itself (e.g. "10m Air Pistol Women") is useful, so it's
# #         folded into competition instead of being dropped
# #       - record_highlight.type normalization (see _normalize_record_type)

# #     This MUST run before _coerce_to_model(), which drops any key not
# #     defined on the target model — by the time that pass runs, an unfolded
# #     "event" key would just be gone rather than folded in.
# #     """
# #     quick_facts = data.get("quick_facts")
# #     if isinstance(quick_facts, dict):
# #         coach = quick_facts.get("coach")
# #         if isinstance(coach, str):
# #             quick_facts["coach"] = re.sub(r"\s*\([^)]*\)", "", coach).strip() or None

# #     medals = data.get("medal_cabinet")
# #     if isinstance(medals, list):
# #         allowed = {"medal_type", "competition", "year"}
# #         for m in medals:
# #             if not isinstance(m, dict):
# #                 continue
# #             for key in [k for k in list(m.keys()) if k not in allowed]:
# #                 extra_val = m.pop(key)
# #                 if extra_val:
# #                     m["competition"] = f"{m['competition']} — {extra_val}" if m.get("competition") else str(extra_val)

# #     data["record_highlight"] = _normalize_record_type(data.get("record_highlight"))

# #     return data


# # def _sync_duplicate_fields(data: dict) -> dict:
# #     """
# #     BaseAthleteProfile has 'coach' at both the top level AND nested inside
# #     quick_facts.coach — two copies of the same fact. Gemini/the retry loop
# #     generally only fills quick_facts.coach (the field notes in the prompt
# #     treat it as the canonical spot), which silently left top-level `coach`
# #     null even when quick_facts.coach was correctly filled (seen on Sunil
# #     Kumar/kabaddi — quick_facts.coach = "Anil Chaprana" but coach = null).
# #     Runs after every coercion pass (initial + each retry) so the two never
# #     drift: whichever side has a real value wins and gets copied to the
# #     other if that side is empty.
# #     """
# #     quick_facts = data.get("quick_facts") or {}
# #     top_coach = data.get("coach")
# #     qf_coach = quick_facts.get("coach")

# #     if _is_empty(top_coach) and not _is_empty(qf_coach):
# #         data["coach"] = qf_coach
# #     elif _is_empty(qf_coach) and not _is_empty(top_coach):
# #         quick_facts["coach"] = top_coach
# #         data["quick_facts"] = quick_facts

# #     return data


# # def _extract_grounding_sources(response) -> list[str]:
# #     """
# #     Pulls the URLs Gemini's search grounding actually cited, from
# #     response.candidates[0].grounding_metadata.grounding_chunks. Defensive —
# #     grounding metadata shape can vary/be absent, so any failure here just
# #     yields an empty list rather than breaking generation.
# #     """
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
# #     # de-dupe, keep order
# #     seen = set()
# #     deduped = []
# #     for u in urls:
# #         if u not in seen:
# #             seen.add(u)
# #             deduped.append(u)
# #     return deduped


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


# # def _generate_single_pass(athlete_name: str, sport: Sport, focus_fields: list[str] | None = None):
# #     """
# #     One prompt -> Gemini call -> parsed JSON dict. Retries once at the raw
# #     HTTP/parsing level if the response wasn't valid JSON (truncation etc.)
# #     before raising. Returns (data, response) so callers can also pull
# #     grounding sources off the raw response.
# #     """
# #     prompt = _build_prompt(sport, athlete_name, focus_fields=focus_fields)
# #     response = _call_gemini(prompt)
# #     try:
# #         data = _extract_json(response.text, response_meta=response)
# #     except GenerationError:
# #         response = _call_gemini(prompt)
# #         data = _extract_json(response.text, response_meta=response)
# #     return data, response


# # def _missing_retryable_fields(data: dict, na_fields: set) -> list[str]:
# #     """Which RETRYABLE_FIELDS are still empty on `data`, excluding any
# #     marked N/A for this sport (those aren't gaps, they're correct)."""
# #     quick_facts_fields = {"coach", "honors"}
# #     missing = []
# #     for f in RETRYABLE_FIELDS:
# #         if f in quick_facts_fields:
# #             if _is_empty((data.get("quick_facts") or {}).get(f)):
# #                 missing.append(f)
# #         elif f in data and _is_empty(data.get(f)):
# #             missing.append(f)
# #     return [f for f in missing if f not in na_fields]


# # def generate_athlete_profile(
# #     athlete_name: str, sport: Sport, max_passes: int = 3
# # ) -> BaseAthleteProfile:
# #     """
# #     Calls Gemini with search grounding enabled, validates against the
# #     sport's schema, then — since search-grounded generation can return
# #     different field coverage on different calls for the same athlete —
# #     runs up to (max_passes - 1) additional targeted retry passes for any
# #     of RETRYABLE_FIELDS that are still empty/null after the first pass
# #     (this now includes record_highlight, so a missed record gets a
# #     second, more targeted search pass too).

# #     Merging is field-by-field and one-directional: once a field is filled,
# #     a later pass can never overwrite it, even if that pass returns a
# #     different value for it. This avoids the exact symptom seen in practice
# #     (performance_trend filled on run 1, empty on run 2 for the same
# #     athlete) — a later pass can only fill gaps, never clobber good data.

# #     A pass that fills nothing new stops the loop early, since further
# #     passes targeting the same still-missing fields are unlikely to help.

# #     Raises GenerationError or pydantic.ValidationError on failure — caller
# #     decides how to handle.
# #     """
# #     schema_cls = get_schema_for_sport(sport)
# #     na_fields = set(_applicability(sport)["na"])

# #     data, response = _generate_single_pass(athlete_name, sport)
# #     data = _sanitize(data)

# #     data["athlete_id"] = _slugify(athlete_name)
# #     data["full_name"] = athlete_name
# #     data["sport"] = sport.value
# #     data["source"] = SourceRef(
# #         source_name="Gemini (search-grounded — unverified, needs human review)",
# #         source_url="https://sportsfan360.internal/llm-draft",  # placeholder, not a real citation
# #         fetched_at=datetime.now(timezone.utc).isoformat(),
# #     ).model_dump(mode="json")

# #     data = _coerce_to_model(data, schema_cls, na_fields=na_fields)
# #     data = _sync_duplicate_fields(data)

# #     grounding_sources = _extract_grounding_sources(response)
# #     retry_log = []

# #     for pass_num in range(2, max_passes + 1):
# #         missing = _missing_retryable_fields(data, na_fields)
# #         if not missing:
# #             break

# #         retry_data, retry_response = _generate_single_pass(
# #             athlete_name, sport, focus_fields=missing
# #         )
# #         retry_data = _sanitize(retry_data)
# #         retry_data = _coerce_to_model(retry_data, schema_cls, na_fields=na_fields)
# #         retry_data = _sync_duplicate_fields(retry_data)

# #         filled_this_pass = []
# #         for f in missing:
# #             if f in ("coach", "honors"):
# #                 candidate = (retry_data.get("quick_facts") or {}).get(f)
# #                 if not _is_empty(candidate):
# #                     data.setdefault("quick_facts", {})[f] = candidate
# #                     filled_this_pass.append(f)
# #             else:
# #                 candidate = retry_data.get(f)
# #                 if not _is_empty(candidate):
# #                     data[f] = candidate
# #                     filled_this_pass.append(f)

# #         retry_log.append(
# #             {"pass": pass_num, "targeted": missing, "filled": filled_this_pass}
# #         )
# #         data = _sync_duplicate_fields(data)

# #         if filled_this_pass:
# #             grounding_sources.extend(_extract_grounding_sources(retry_response))
# #         else:
# #             # This pass found nothing new — further passes targeting the
# #             # same fields are unlikely to help either, stop early.
# #             break

# #     data["grounding_sources"] = list(dict.fromkeys(grounding_sources))  # de-dupe, keep order

# #     profile = schema_cls.model_validate(data)

# #     # Retry provenance isn't part of the schema (kept out of the models
# #     # above deliberately — it's debugging/QA metadata, not athlete
# #     # content), so it's attached after validation and only shows up in
# #     # the saved JSON / printed output, not in the validated model.
# #     profile_dict = profile.model_dump(mode="json")
# #     profile_dict["_retry_log"] = retry_log
# #     return profile, profile_dict


# # def _prompt_sport() -> Sport:
# #     options = list(Sport)
# #     print("\nSport:")
# #     for i, s in enumerate(options, 1):
# #         print(f"  {i}. {s.value}")
# #     while True:
# #         choice = input("Choose a number: ").strip()
# #         if choice.isdigit() and 1 <= int(choice) <= len(options):
# #             return options[int(choice) - 1]
# #         print("Invalid choice, try again.")


# # def main():
# #     os.makedirs(OUTPUT_DIR, exist_ok=True)

# #     while True:
# #         athlete_name = input("\nAthlete name (blank to quit): ").strip()
# #         if not athlete_name:
# #             print("Done.")
# #             break

# #         sport = _prompt_sport()

# #         print(f"\nGenerating draft for {athlete_name} ({sport.value})...")
# #         try:
# #             profile, profile_dict = generate_athlete_profile(athlete_name, sport)
# #         except (GenerationError, ValidationError) as e:
# #             print(f"FAILED: {e}", file=sys.stderr)
# #             continue

# #         out_path = os.path.join(OUTPUT_DIR, f"{profile.athlete_id}.json")
# #         with open(out_path, "w") as f:
# #             json.dump(profile_dict, f, indent=2, default=str)

# #         if profile_dict.get("_retry_log"):
# #             print("\nRetry log:")
# #             for entry in profile_dict["_retry_log"]:
# #                 print(
# #                     f"  pass {entry['pass']}: targeted {entry['targeted']} "
# #                     f"-> filled {entry['filled'] or '(nothing new)'}"
# #                 )

# #         print(f"Saved: {out_path}")
# #         print(json.dumps(profile_dict, indent=2, default=str))


# # if __name__ == "__main__":
# #     main()






# """
# generate_athlete_content_llm.py

# Standalone content generator: prompts Gemini (with Google Search grounding
# enabled) to draft an athlete document matching sportsfan360-schema-v4.md —
# `coreInfo` (identity/bio), `performance` (sport-specific stats), and
# `record_highlight` (the athlete's most notable record, for the match-center
# record card + the National/Olympic/World benchmark-comparison strip).
# Scoped to the 3 priority sports: cricket, football, and track & field (one
# `sportId`, "athletics", covering 6 sub-disciplines: sprints,
# middle_distance, long_distance, throws, jumps, relays).

# `analytics` (seasonalData/radarData/coachImpactData/consistencyData/
# heatmapData) IS drafted by Gemini. radarData's axes are now CATEGORY-
# SPECIFIC — a sprinter is scored on Reaction/Top Speed/Acceleration, a
# thrower on Power/Technique/Release, etc. (see _RADAR_AXES_BY_CATEGORY) —
# rather than one generic axis set stretched across every sport.

# Fields should be FILLED, not left null, whenever the underlying fact is
# realistically findable via search (a personal best, a world record holder,
# a coach's name). Gemini is told this explicitly and should only null a
# field after genuinely searching and coming up empty — see
# "NULL-AVOIDANCE" in the prompt.

# Also NOT drafted: the platform-engagement fields from the schema doc's
# top level (isVerified, fanImpactScore, fansCount, badges, hubCounts,
# featuredContent, activeRecordsHeld) — these are internal platform state,
# not facts about the athlete, so they're outside this script's job. It
# outputs `coreInfo` + `performance` + `analytics` + `record_highlight`,
# meant to be merged into the rest of the document by the API route, not
# the whole document itself.

# Everything still lives in this one file on purpose — schemas, the generic
# coercion engine, the prompt builder, and the CLI — after an earlier split
# into separate files led to a circular import. One file, one
# `python generate_athlete_content_llm.py`.

# Run it, enter one athlete name + pick a sport at a time. Each run:
#   1. builds a prompt asking Gemini for JSON matching this sport's document
#      shape, with Google Search grounding turned on
#   2. parses + validates the response against the schemas below
#   3. if key fields are still empty/null after pass 1, runs up to 2 more
#      targeted retry passes, merging in anything newly found field-by-field
#      (already-filled fields are never overwritten)
#   4. prints the validated JSON and saves it to
#      ./llm_athlete_drafts/<athlete_id>.json

# NOTE: search-grounded LLM output can still be wrong or out of date — this is
# a content-drafting aid for the review queue, not a source-of-truth stats
# feed. Every draft still needs human review before publishing.
# """

# import json
# import os
# import re
# import sys
# import typing
# from datetime import date as date_type, datetime, timezone
# from enum import Enum
# from typing import Optional

# from google import genai
# from google.genai import types
# from pydantic import BaseModel, Field, HttpUrl, ValidationError


# # ═══════════════════════════════════════════════════════════════════════
# # SCHEMAS
# # ═══════════════════════════════════════════════════════════════════════

# class Sport(str, Enum):
#     SPRINTS = "sprints"
#     MIDDLE_DISTANCE = "middle_distance"
#     LONG_DISTANCE = "long_distance"
#     THROWS = "throws"
#     JUMPS = "jumps"
#     RELAYS = "relays"
#     CRICKET = "cricket"
#     FOOTBALL = "football"


# _TRACK_FIELD_SPORTS = {
#     Sport.SPRINTS,
#     Sport.MIDDLE_DISTANCE,
#     Sport.LONG_DISTANCE,
#     Sport.THROWS,
#     Sport.JUMPS,
#     Sport.RELAYS,
# }

# _CATEGORY_LABELS: dict[Sport, str] = {
#     Sport.SPRINTS: "Sprints",
#     Sport.MIDDLE_DISTANCE: "Middle Distance",
#     Sport.LONG_DISTANCE: "Long Distance",
#     Sport.THROWS: "Throws",
#     Sport.JUMPS: "Jumps",
#     Sport.RELAYS: "Relays",
# }

# # Category -> the 5 radar axes Gemini should score for THIS discipline,
# # matching the real analytics widget's radarData shape (list of
# # {metric, value, fullMark}, 5 entries). Distinct per category rather than
# # one generic set stretched across every sport (the old "Avg Distance" /
# # "Best Throw" placeholders that showed up identically for every athlete
# # regardless of event were wrong — a sprinter and a thrower are good at
# # fundamentally different things). "Growth" is always the 5th axis, shared
# # across categories, since it's a universal year-over-year trend score.
# _RADAR_AXES_BY_CATEGORY: dict[Sport, list[str]] = {
#     Sport.SPRINTS: ["Reaction Time", "Top Speed", "Acceleration", "Consistency", "Growth"],
#     Sport.MIDDLE_DISTANCE: ["Pace Judgment", "Kick Speed", "Endurance", "Consistency", "Growth"],
#     Sport.LONG_DISTANCE: ["Endurance", "Pace Judgment", "Race Tactics", "Consistency", "Growth"],
#     Sport.THROWS: ["Power", "Technique", "Release Consistency", "Big-Meet Performance", "Growth"],
#     Sport.JUMPS: ["Explosiveness", "Technique", "Approach Consistency", "Big-Meet Performance", "Growth"],
#     Sport.RELAYS: ["Baton Exchange", "Leg Speed", "Consistency", "Team Synergy", "Growth"],
# }

# # Category -> (metricLabel, unit, tooltipValueSuffix, consistencyBarLabel),
# # pipeline-set (like `category` itself) rather than left for Gemini to
# # pick, since these are fixed per discipline, not researched facts.
# _ANALYTICS_DEFAULTS_BY_CATEGORY: dict[Sport, dict] = {
#     Sport.SPRINTS: {"metricLabel": "Time", "unit": "s", "tooltipValueSuffix": "s", "consistencyBarLabel": "attempts"},
#     Sport.MIDDLE_DISTANCE: {"metricLabel": "Time", "unit": "s", "tooltipValueSuffix": "s", "consistencyBarLabel": "attempts"},
#     Sport.LONG_DISTANCE: {"metricLabel": "Time", "unit": "s", "tooltipValueSuffix": "s", "consistencyBarLabel": "attempts"},
#     Sport.THROWS: {"metricLabel": "Distance", "unit": "m", "tooltipValueSuffix": "m", "consistencyBarLabel": "throws"},
#     Sport.JUMPS: {"metricLabel": "Distance", "unit": "m", "tooltipValueSuffix": "m", "consistencyBarLabel": "attempts"},
#     Sport.RELAYS: {"metricLabel": "Time", "unit": "s", "tooltipValueSuffix": "s", "consistencyBarLabel": "attempts"},
# }

# _HEATMAP_TOURNAMENT_TIERS = ["Olympics", "World Champ", "National Champ"]


# def _sport_id_for(sport: Sport) -> str:
#     if sport in _TRACK_FIELD_SPORTS:
#         return "athletics"
#     return sport.value  # "cricket" | "football"


# class SourceRef(BaseModel):
#     source_name: str = Field(default="Gemini (Google Search grounding)")
#     source_url: Optional[HttpUrl] = None
#     fetched_at: str  # ISO8601 timestamp, set by the pipeline


# class CoreInfo(BaseModel):
#     name: str
#     country: Optional[str] = None
#     flag: Optional[str] = Field(None, description="emoji flag, e.g. '🇮🇳'")
#     gender: Optional[str] = Field(None, description="'Men' or 'Women' — used for filtering")
#     dob: Optional[date_type] = None
#     heightCm: Optional[int] = None
#     weightKg: Optional[float] = None
#     profileImage: Optional[str] = None  # not LLM-drafted, left null for editors
#     bio: Optional[str] = Field(None, description="2-4 sentence factual summary, no editorializing")
#     birthplace: Optional[str] = None
#     coachName: Optional[str] = None
#     yearsActiveSince: Optional[str] = Field(None, description="e.g. '2018'")

#     model_config = {"extra": "forbid"}


# class ProgressPoint(BaseModel):
#     year: str = Field(..., description="e.g. '2022' — a chart label, kept as a string, not a date")
#     value: float = Field(
#         ..., description="numeric mark for that year in the record's native unit (seconds, meters, points), no unit suffix"
#     )

#     model_config = {"extra": "forbid"}


# class BenchmarkRecord(BaseModel):
#     """
#     One row of the National/Olympic/World record-comparison strip. Always
#     present as 3 entries on every RecordHighlight — label is fixed, the
#     rest is filled with whatever Gemini can find for that tier. World and
#     Olympic records are usually well-documented, stable facts (e.g. Usain
#     Bolt's 9.58s 100m WR) — these should essentially never end up null;
#     National can be genuinely unknown for less-tracked events, and that's
#     fine to leave null after a real search attempt.
#     """
#     label: str = Field(..., description="exactly one of: 'National', 'Olympic', 'World'")
#     value: Optional[str] = Field(None, description="the record mark in its native unit, e.g. '9.58s', '90.23m'")
#     holder: Optional[str] = Field(None, description="athlete/team who holds this record")
#     date: Optional[str] = Field(None, description="display date the record was set, e.g. 'Aug 16, 2009'")
#     venue: Optional[str] = Field(None, description="city/venue where this record was set")
#     tournament: Optional[str] = Field(None, description="event/meet name, e.g. 'Berlin World Championships'")

#     model_config = {"extra": "forbid"}


# _BENCHMARK_LABELS = ["National", "Olympic", "World"]


# class RecordHighlight(BaseModel):
#     """
#     The athlete's most notable record for the match-center record card —
#     NOT every record they've ever set, just the single most significant
#     still-relevant one — plus the National/Olympic/World benchmark strip
#     (`benchmarks`) that compares the event's own top marks against those 3
#     tiers regardless of whether the athlete personally holds any of them.
#     """
#     event: Optional[str] = Field(None, description="e.g. \"Men's Javelin Throw\", \"Most T20I Wickets\"")
#     result: Optional[str] = Field(None, description="the record mark, e.g. '88.13m', '5/17', '3 goals in a match'")
#     type: Optional[str] = Field(
#         None, description="one of: GR (Games Record), NR (National Record), WR (World Record), CR (Continental Record), PB (Personal Best)"
#     )
#     typeFull: Optional[str] = Field(None, description="e.g. 'Games Record', 'World Record'")
#     phase: Optional[str] = Field(None, description="e.g. 'Final', 'Semifinal'")
#     date: Optional[str] = Field(None, description="display date the record was set, e.g. 'Aug 7, 2026'")
#     city: Optional[str] = Field(None, description="city/venue where THIS record was set")
#     prevRecord: Optional[str] = Field(None, description="the mark this record broke")
#     prevRecordPlace: Optional[str] = Field(
#         None, description="city/venue where the PREVIOUS (old) record was set — distinct from `city`"
#     )
#     prevHolder: Optional[str] = Field(None, description="athlete/team who held the previous record")
#     improvement: Optional[str] = Field(None, description="signed margin, e.g. '+2.53m', '-0.08s'")
#     improvementDate: Optional[str] = Field(
#         None, description="date the record was actually broken — should match `date` unless known otherwise"
#     )
#     aiInsight: Optional[str] = Field(
#         None, description="2-3 factual sentences on what made the performance notable, no unfounded editorializing"
#     )
#     progressData: list[ProgressPoint] = Field(
#         default_factory=list,
#         description="the athlete's progression toward this record, up to the 6 most recent years; [] if not applicable",
#     )
#     benchmarks: list[BenchmarkRecord] = Field(
#         default_factory=lambda: [BenchmarkRecord(label=l) for l in _BENCHMARK_LABELS],
#         description="always exactly 3 entries, one per _BENCHMARK_LABELS ('National', 'Olympic', 'World'), for the record-comparison strip",
#     )

#     model_config = {"extra": "forbid"}


# class Analytics(BaseModel):
#     """Generic fallback analytics shape — used for cricket/football, where
#     no reference export like the athletics one below exists yet. Keep
#     using dict containers there until a real cricket/football analytics
#     sample is available to match field-for-field the way AthleticsAnalytics
#     now does."""
#     seasonalData: dict = Field(default_factory=dict)
#     radarData: dict = Field(default_factory=dict)
#     coachImpactData: dict = Field(default_factory=dict)
#     consistencyData: dict = Field(default_factory=dict)
#     heatmapData: dict = Field(default_factory=dict)

#     model_config = {"extra": "forbid"}


# # ── AthleticsAnalytics — matches the real analytics-widget export field-
# # for-field (see csvjson.json sample: gurindervir-singh, neeraj-chopra,
# # etc.), not a generic guessed shape. Every sub-model's field names mirror
# # that export exactly so the pipeline output can be merged straight in. ──

# class SeasonalDataPoint(BaseModel):
#     year: str
#     event: Optional[str] = None
#     value: Optional[float] = None
#     rank: Optional[int] = None
#     highlight: bool = False
#     glow: bool = False

#     model_config = {"extra": "forbid"}


# class MedalDataPoint(BaseModel):
#     year: str
#     gold: int = 0
#     silver: int = 0
#     bronze: int = 0

#     model_config = {"extra": "forbid"}


# class RadarDataPoint(BaseModel):
#     metric: str
#     value: Optional[float] = Field(None, description="0-100 score")
#     fullMark: float = 100

#     model_config = {"extra": "forbid"}


# class ConsistencyDataPoint(BaseModel):
#     range: str = Field(..., description="e.g. '10.2–10.3s', '83-85m'")
#     count: Optional[int] = None
#     throws: Optional[int] = Field(None, description="attempt count for this bucket — same number as `count`, kept for export compatibility")
#     percentage: Optional[float] = None
#     peak: bool = False

#     model_config = {"extra": "forbid"}


# class CoachImpactPoint(BaseModel):
#     year: int
#     value: Optional[float] = None
#     period: str = Field(..., description="'before' | 'after' (relative to coachJoinYear)")

#     model_config = {"extra": "forbid"}


# class BenchmarkTier(BaseModel):
#     """One entry of analytics.heatmapData — despite the legacy name, this
#     is the Olympics/World Champ/National Champ comparison tier data, NOT a
#     spatial heatmap. Always exactly 3 entries (see _HEATMAP_TOURNAMENT_TIERS),
#     `years` holding whatever record/best-mark data is findable for that
#     tier (kept as a free dict since the export itself leaves it as an
#     open bag, e.g. {"value": "9.58s", "holder": "Usain Bolt", "date": ...})."""
#     tournament: str = Field(..., description="exactly one of: 'Olympics', 'World Champ', 'National Champ'")
#     years: dict = Field(default_factory=dict)

#     model_config = {"extra": "forbid"}


# class AthleteStats(BaseModel):
#     worldRank: Optional[str] = None
#     personalBest: Optional[str] = None
#     bestYear: Optional[str] = None
#     totalGold: Optional[int] = None
#     totalSilver: Optional[int] = None
#     totalBronze: Optional[int] = None
#     olympicGold: Optional[int] = None

#     model_config = {"extra": "forbid"}


# class AthleticsAnalytics(BaseModel):
#     """Track & field analytics — field names and shapes match the real
#     export exactly, NOT the generic nested-dict Analytics model above.
#     Fields Gemini should fill via search/derivation; only heatmapData's
#     tournament labels, consistencyBarLabel/metricLabel/unit/
#     tooltipValueSuffix are pipeline-set (see _ANALYTICS_DEFAULTS_BY_CATEGORY,
#     _HEATMAP_TOURNAMENT_TIERS) rather than drafted, since those are fixed
#     by category, not researched facts."""
#     heroLabel: str = "Personal Best"
#     heroStat: Optional[str] = None
#     metricLabel: Optional[str] = Field(None, description="'Time' | 'Distance'")
#     unit: Optional[str] = Field(None, description="'s' | 'm'")
#     tooltipValueSuffix: Optional[str] = None
#     yAxisDomain: list[float] = Field(default_factory=list, description="[min, max]")
#     sport: Optional[str] = Field(None, description="display event name, e.g. '100m Sprint', 'Long Jump', 'Javelin Throw', 'Decathlon'")
#     achievementLabel: Optional[str] = Field(None, description="a short badge, e.g. 'Asian U18 Champion'")
#     stats: Optional[AthleteStats] = None
#     seasonalData: list[SeasonalDataPoint] = Field(default_factory=list)
#     medalData: list[MedalDataPoint] = Field(default_factory=list)
#     radarData: list[RadarDataPoint] = Field(default_factory=list)
#     consistencyBarLabel: Optional[str] = None
#     consistencyData: list[ConsistencyDataPoint] = Field(default_factory=list)
#     consistencyNote: Optional[str] = None
#     peakZoneLabel: Optional[str] = Field(None, description="e.g. '10.2–10.3s · 36% success rate', derived from consistencyData's peak bucket")
#     heatmapData: list[BenchmarkTier] = Field(
#         default_factory=lambda: [BenchmarkTier(tournament=t) for t in _HEATMAP_TOURNAMENT_TIERS],
#         description="always exactly 3 entries, one per _HEATMAP_TOURNAMENT_TIERS",
#     )
#     beforeCoach: Optional[str] = Field(None, description="the athlete's performance mark BEFORE coachJoinYear, e.g. '10.38s' — a mark, not a name")
#     afterCoach: Optional[str] = Field(None, description="the athlete's performance mark AFTER coachJoinYear, e.g. '10.09s' — a mark, not a name")
#     coachImprovement: Optional[str] = Field(None, description="signed delta + %, e.g. '+0.13m (+7.2%)', or 'Data not available'")
#     coachJoinYear: Optional[int] = None
#     coachImpactData: list[CoachImpactPoint] = Field(default_factory=list)

#     model_config = {"extra": "forbid"}


# class MedalEntry(BaseModel):
#     event: str = Field(..., description="e.g. 'CWG 2026'")
#     medal: str = Field(..., description="'GOLD' | 'SILVER' | 'BRONZE'")
#     category: Optional[str] = Field(None, description="e.g. '100m'")

#     model_config = {"extra": "forbid"}


# class AthleticsStats(BaseModel):
#     personalBest: Optional[str] = None
#     seasonBest: Optional[str] = None

#     model_config = {"extra": "forbid"}


# class AthleticsChartConfig(BaseModel):
#     yAxisDomain: list[float] = Field(default_factory=list, description="[min, max]")
#     unit: Optional[str] = Field(None, description="'seconds' | 'meters'")

#     model_config = {"extra": "forbid"}


# class AthleticsPerformance(BaseModel):
#     primaryEvent: Optional[str] = Field(None, description="e.g. '100m', 'Javelin Throw'")
#     category: Optional[str] = Field(
#         None, description="'Sprints' | 'Middle Distance' | 'Long Distance' | 'Throws' | 'Jumps' | 'Relays'"
#     )
#     preferredLane: Optional[int] = None
#     currentSeason: Optional[str] = None
#     medalCabinet: list[MedalEntry] = Field(default_factory=list)
#     stats: Optional[AthleticsStats] = None
#     chartConfig: Optional[AthleticsChartConfig] = None

#     model_config = {"extra": "forbid"}


# class CricketBattingStats(BaseModel):
#     runs: Optional[int] = None
#     ballsFaced: Optional[int] = None
#     battingAverage: Optional[float] = None
#     strikeRate: Optional[float] = None
#     fours: Optional[int] = None
#     sixes: Optional[int] = None
#     dismissals: Optional[int] = None

#     model_config = {"extra": "forbid"}


# class CricketBowlingStats(BaseModel):
#     oversBowled: Optional[float] = None
#     ballsBowled: Optional[int] = None
#     runsConceded: Optional[int] = None
#     wickets: Optional[int] = None
#     economy: Optional[float] = None
#     bowlingAverage: Optional[float] = None

#     model_config = {"extra": "forbid"}


# class CricketChartConfig(BaseModel):
#     battingUnit: str = "runs"
#     bowlingUnit: str = "wickets"

#     model_config = {"extra": "forbid"}


# class CricketPerformance(BaseModel):
#     format: Optional[str] = Field(None, description="e.g. 'T20'")
#     tournament: Optional[str] = Field(None, description="e.g. 'womens_t20i', 'womens_ipl'")
#     jerseyNo: Optional[int] = None
#     battingStats: Optional[CricketBattingStats] = None
#     bowlingStats: Optional[CricketBowlingStats] = None
#     chartConfig: CricketChartConfig = Field(default_factory=CricketChartConfig)

#     model_config = {"extra": "forbid"}


# class FootballAttackingStats(BaseModel):
#     goals: Optional[int] = None
#     assists: Optional[int] = None
#     shots: Optional[int] = None
#     shotsOnTarget: Optional[int] = None
#     shotConversionPct: Optional[float] = None
#     xg: Optional[float] = None
#     xa: Optional[float] = None

#     model_config = {"extra": "forbid"}


# class FootballPlaymakingStats(BaseModel):
#     chancesCreated: Optional[int] = None
#     bigChancesCreated: Optional[int] = None
#     keyPasses: Optional[int] = None
#     dribblesCompleted: Optional[int] = None

#     model_config = {"extra": "forbid"}


# class FootballAppearanceStats(BaseModel):
#     matchesPlayed: Optional[int] = None
#     minutesPlayed: Optional[int] = None

#     model_config = {"extra": "forbid"}


# class FootballChartConfig(BaseModel):
#     primaryUnit: str = "goals"
#     secondaryUnit: str = "xg"

#     model_config = {"extra": "forbid"}


# class FootballPerformance(BaseModel):
#     position: Optional[str] = Field(None, description="'GK' | 'DF' | 'MF' | 'FW'")
#     team: Optional[str] = None
#     season: Optional[int] = None
#     tournament: Optional[str] = None
#     format: Optional[str] = Field(None, description="e.g. 'international', 'club'")
#     attackingStats: Optional[FootballAttackingStats] = None
#     playmakingStats: Optional[FootballPlaymakingStats] = None
#     appearanceStats: Optional[FootballAppearanceStats] = None
#     chartConfig: FootballChartConfig = Field(default_factory=FootballChartConfig)

#     model_config = {"extra": "forbid"}


# class AthleteDocumentBase(BaseModel):
#     athlete_id: str
#     sportId: str
#     coreInfo: CoreInfo
#     record_highlight: Optional[RecordHighlight] = Field(
#         None, description="null only if generation failed to run at all; otherwise always returned with at least benchmarks populated"
#     )
#     analytics: Analytics = Field(default_factory=Analytics)
#     grounding_sources: list[str] = Field(
#         default_factory=list,
#         description="URLs Gemini's search grounding actually cited for this draft, for reviewer spot-checking",
#     )
#     source: SourceRef

#     model_config = {"extra": "forbid"}


# class AthleticsAthleteDocument(AthleteDocumentBase):
#     performance: AthleticsPerformance
#     analytics: AthleticsAnalytics = Field(default_factory=AthleticsAnalytics)


# class CricketAthleteDocument(AthleteDocumentBase):
#     performance: CricketPerformance


# class FootballAthleteDocument(AthleteDocumentBase):
#     performance: FootballPerformance


# def get_schema_for_sport(sport: Sport) -> type[AthleteDocumentBase]:
#     if sport in _TRACK_FIELD_SPORTS:
#         return AthleticsAthleteDocument
#     if sport == Sport.CRICKET:
#         return CricketAthleteDocument
#     return FootballAthleteDocument


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

# GEMINI_MODEL = os.getenv("ATHLETE_EXTRACTION_MODEL", "gemini-2.5-flash")
# OUTPUT_DIR = "llm_athlete_drafts"
# _VALID_RECORD_TYPES = {"GR", "NR", "WR", "CR", "PB"}


# class GenerationError(Exception):
#     pass


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


# RETRYABLE_PATHS: dict[Sport, list[str]] = {
#     **{
#         s: [
#             "coreInfo.coachName",
#             "performance.medalCabinet",
#             "performance.stats.personalBest",
#             "performance.stats.seasonBest",
#             "record_highlight",
#             "record_highlight.benchmarks",
#             "analytics.seasonalData",
#             "analytics.radarData",
#             "analytics.medalData",
#             "analytics.stats",
#             "analytics.consistencyData",
#             "analytics.heatmapData",
#         ]
#         for s in _TRACK_FIELD_SPORTS
#     },
#     Sport.CRICKET: [
#         "coreInfo.coachName",
#         "performance.tournament",
#         "performance.jerseyNo",
#         "performance.battingStats",
#         "performance.bowlingStats",
#         "record_highlight",
#         "record_highlight.benchmarks",
#         "analytics.seasonalData",
#         "analytics.radarData",
#     ],
#     Sport.FOOTBALL: [
#         "coreInfo.coachName",
#         "performance.team",
#         "performance.attackingStats",
#         "performance.playmakingStats",
#         "record_highlight",
#         "record_highlight.benchmarks",
#         "analytics.seasonalData",
#         "analytics.radarData",
#     ],
# }


# def _tiered_list_has_data(entries: list, key_names: tuple[str, ...]) -> bool:
#     """Shared check for the two 'always 3 label-only entries by default'
#     lists (record_highlight.benchmarks, analytics.heatmapData) — true if
#     ANY entry has actual content beyond its fixed label/tournament key."""
#     for e in entries or []:
#         if not isinstance(e, dict):
#             continue
#         for k in key_names:
#             if not _is_empty(e.get(k)):
#                 return True
#     return False


# def _missing_retryable_paths(data: dict, sport: Sport) -> list[str]:
#     missing = []
#     for p in RETRYABLE_PATHS[sport]:
#         if p == "record_highlight.benchmarks":
#             benches = _get_path(data, "record_highlight.benchmarks") or []
#             if not _tiered_list_has_data(benches, ("value", "holder")):
#                 missing.append(p)
#             continue
#         if p == "analytics.heatmapData":
#             tiers = _get_path(data, "analytics.heatmapData") or []
#             if not _tiered_list_has_data(tiers, ("years",)):
#                 missing.append(p)
#             continue
#         if _is_empty(_get_path(data, p)):
#             missing.append(p)
#     return missing


# def _sport_specific_field_notes(sport: Sport, cricket_format: str | None = None) -> str:
#     if sport in _TRACK_FIELD_SPORTS:
#         axes = _RADAR_AXES_BY_CATEGORY[sport]
#         axes_list = ", ".join(f'"{a}"' for a in axes)
#         return f"""
# - performance.category MUST be exactly "{_CATEGORY_LABELS[sport]}" (already fixed by the
#   category you were asked to draft for — do not change it).
# - performance.primaryEvent: the specific event within this category, e.g. for Sprints use
#   "100m", "200m", "400m", or "400m Hurdles"; for Jumps use "Long Jump", "High Jump", "Triple
#   Jump", or "Pole Vault"; for Throws use "Javelin Throw", "Shot Put", "Discus Throw", or
#   "Hammer Throw"; for Relays use "4x100m Relay" or "4x400m Relay". Search for the athlete's
#   actual specific event — never leave generic.
# - performance.medalCabinet: list of {{event, medal, category}}, medal is exactly "GOLD" |
#   "SILVER" | "BRONZE". Include at most the 8 most significant international medals.
# - performance.stats.personalBest / seasonBest: plain strings in the event's native unit,
#   e.g. "9.99s" or "90.23 m" — never bare numbers.
# - performance.chartConfig.unit: "seconds" for track events, "meters" for field events.
#   chartConfig.yAxisDomain: a sensible [min, max] range around the athlete's personal best
#   (e.g. personal best 9.99s -> [9.5, 10.5]) — leave [] only if you genuinely cannot estimate
#   one (you almost always can once you know the personal best).
# - performance.preferredLane: only fill if you find a specific, reliably-reported lane
#   preference; otherwise null — this is commonly unknown and that's fine.

# record_highlight.benchmarks (the National/Olympic/World record-comparison strip — ALWAYS
# return exactly 3 entries with label "National", "Olympic", "World" in that order, even when
# record_highlight itself has nothing for this athlete — this compares the EVENT's records,
# not just this athlete's own):
# - For each: value (the record mark), holder (athlete/team), date, venue, tournament/meet
#   name. World and Olympic records for well-known events (e.g. 100m, javelin, marathon) are
#   stable, well-documented facts — search for and fill these confidently; do not leave them
#   null just because the record isn't this athlete's own. National can genuinely be
#   hard-to-verify for less-tracked events/countries — null is acceptable there ONLY after an
#   actual search attempt.
# - If the athlete's OWN personal best equals or is close to one of these tiers, still report
#   the tier's actual official record (which may be someone else's), not the athlete's mark.

# analytics fields — these match a real export format field-for-field, NOT a generic guessed
# shape, so use these EXACT keys. Derive every value from facts you actually find via search;
# avoid leaving fields null or empty when the underlying stat is realistically findable — only
# use an empty list/dict/null after a genuine search attempt comes up empty:
# - analytics.heroStat: the athlete's headline personal-best mark as a display string, e.g.
#   "10.09s", "90.23m" (heroLabel is always "Personal Best", already set).
# - analytics.sport: the specific display event name, e.g. "100m Sprint", "Long Jump",
#   "Javelin Throw", "3000m Steeplechase", "High Jump", "Decathlon" — matches
#   performance.primaryEvent in meaning but written as the full display label.
# - analytics.achievementLabel: one short badge phrase, e.g. "Asian U18 Champion", "Tokyo 2020
#   Olympic Gold" — the single most notable achievement, not a full list.
# - analytics.stats: {{worldRank, personalBest, bestYear, totalGold, totalSilver, totalBronze,
#   olympicGold}} — career-total medal counts and current world ranking, all searchable facts.
# - analytics.seasonalData: list of {{year, event, value, rank, highlight, glow}} objects, one
#   per year across the athlete's career (most recent last) — value as a plain number in the
#   event's native unit, no suffix; highlight=true for personal-best/breakthrough years,
#   glow=true for the single standout result. Reuse the same year-by-year marks you found for
#   record_highlight.progressData rather than re-deriving them.
# - analytics.medalData: list of {{year, gold, silver, bronze}} objects — medal counts PER YEAR
#   (not career totals — those go in stats), one entry per year with any international medal.
# - analytics.radarData: list of exactly 5 {{metric, value, fullMark}} objects, metric being
#   each of [{axes_list}] IN THAT ORDER, fullMark always 100, value your 0-100 editorial
#   estimate (like aiInsight) based on what your research actually showed — do not default to
#   a flat score for every metric, vary them based on what you found. These axes are specific
#   to {_CATEGORY_LABELS[sport]} — do not substitute a generic axis set like "Avg Distance"
#   or "Best Throw" for a discipline they don't fit.
# - analytics.consistencyData: list of {{range, count, throws, percentage, peak}} objects — a
#   histogram of the athlete's performance distribution across ~5-6 buckets of their native
#   unit (e.g. "10.2–10.3s" buckets for a sprinter, "83-85m" buckets for a thrower), percentage
#   summing to ~100 across buckets, peak=true for the bucket(s) with the highest count. Derive
#   this from whatever season-by-season / meet-by-meet results you can find; a reasonable
#   estimate from real results is expected, not a literal database export.
# - analytics.consistencyNote: 1 factual sentence on the athlete's consistency, e.g. "National
#   Record holder; consistent sub-10.2 performer".
# - analytics.peakZoneLabel: derived directly from consistencyData — "<range of the peak
#   bucket> · <its percentage>% success rate", e.g. "10.2–10.3s · 36% success rate".
# - analytics.coachJoinYear, beforeCoach, afterCoach, coachImprovement, coachImpactData: only
#   fill if you find a clear, specific before/after comparison tied to a documented coaching
#   change — beforeCoach/afterCoach are performance MARKS (not coach names) from just before
#   and after that year, coachImpactData is the full year-by-year list of {{year, value,
#   period}} with period "before" or "after" relative to coachJoinYear, coachImprovement is
#   the signed delta ("+0.13m (+7.2%)") or the literal string "Data not available" if you have
#   the before/after marks but can't compute a meaningful percentage. Leave all of these
#   null/[] together if no documented coaching change is findable — that's the expected/
#   correct answer for most athletes, not a miss.
# - analytics.heatmapData: ALWAYS exactly 3 entries with tournament "Olympics", "World Champ",
#   "National Champ" in that order (same National/Olympic/World tiers as
#   record_highlight.benchmarks, kept here too for export compatibility — you can reuse the
#   same facts). For each, fill `years` with whatever you found for that tier, e.g. {{"value":
#   "9.58s", "holder": "Usain Bolt", "date": "Aug 16, 2009", "venue": "Berlin"}} — leave
#   `years` as {{}} only after a genuine search attempt for that tier comes up empty.
# """
#     if sport == Sport.CRICKET:
#         return f"""
# - performance.format MUST be exactly "{cricket_format}" — this athlete plays multiple
#   formats (Test/ODI/T20), each with completely different career statistics. EVERY figure you
#   report below (battingStats, bowlingStats, tournament, and record_highlight if applicable)
#   MUST come from their {cricket_format} career specifically — do not mix in stats, records, or
#   tournament names from a different format, even if they're more impressive or more recent.
#   If you cannot find reliable {cricket_format}-specific stats, leave the relevant fields null
#   rather than substituting another format's numbers.
# - performance.battingStats and performance.bowlingStats: fill BOTH if the athlete has any
#   batting and bowling record in {cricket_format}, even a minor one — this data source doesn't
#   split players into "batsman only" / "bowler only" roles, most players carry some figures in
#   both. Only null out a whole stats object if the athlete genuinely never bats or never bowls
#   in this format. battingStats: runs, ballsFaced, battingAverage, strikeRate, fours, sixes,
#   dismissals (all plain numbers). bowlingStats: oversBowled (decimal, e.g. 38.3), ballsBowled,
#   runsConceded, wickets, economy, bowlingAverage (all plain numbers).
# - performance.tournament: the specific {cricket_format} tournament/series being described,
#   e.g. "womens_t20i", "womens_ipl", or a similarly specific identifier — not just "cricket".

# record_highlight.benchmarks (the National/Olympic/World record-comparison strip — ALWAYS
# return exactly 3 entries with label "National", "Olympic", "World" in that order, even when
# record_highlight itself has nothing for this athlete. Cricket has no "Olympic" record in the
# athletics sense, so map the 3 tiers to the closest cricket equivalents and say so via
# `tournament`: "National" = the Indian national {cricket_format} record for this stat category
# (e.g. most runs/wickets in {cricket_format}), "Olympic" = the equivalent ICC-tournament/
# major-global-event record most comparable in prestige (e.g. World Cup record for this stat),
# "World" = the outright {cricket_format} world record for this stat category. Fill
# value/holder/date/venue/tournament for each — these are stable, well-documented facts for
# cricket's headline records (most runs/wickets); avoid leaving them null.

# analytics fields (derive from facts you actually find via search — avoid leaving these null
# when the underlying stat is realistically findable; only use {{}} after a genuine search
# comes up empty):
# - analytics.seasonalData: {{"labels": [...years/seasons, most recent last...], "runs": [...],
#   "wickets": [...]}} — season-by-season {cricket_format} figures if you can find them via a
#   stats site or season recap articles. {{}} only if truly just career totals are findable.
# - analytics.radarData: {{"axes": ["Batting Average", "Strike Rate", "Economy", "Wickets"],
#   "values": [...four 0-100 scores...]}}. These ARE your editorial estimates (like aiInsight),
#   not hard facts — score based on the actual battingStats/bowlingStats you found relative to
#   what's typical for a {cricket_format} player (e.g. a strike rate well above the format's
#   norm scores high). Do not default to a flat score for every axis. {{}} only if
#   battingStats and bowlingStats are both largely null.
# - analytics.consistencyData: {{"note": <1 short factual sentence on consistency, if you
#   found something specific, e.g. a notable string of 50+ scores>}} — {{}} if nothing specific
#   found; do not pad this with generic filler.
# - analytics.coachImpactData and analytics.heatmapData: leave {{}} — a documented coaching
#   before/after or spatial dismissal/scoring-zone data is rarely findable for cricket via
#   general search; {{}} is the expected/correct answer here, not a miss.
# """
#     # football
#     return """
# - performance.attackingStats: goals, assists, shots, shotsOnTarget, shotConversionPct (as a
#   percentage number, e.g. 15.5 not "15.5%"), xg, xa (expected goals/assists, decimals).
# - performance.playmakingStats: chancesCreated, bigChancesCreated, keyPasses,
#   dribblesCompleted — all plain integers.
# - performance.appearanceStats: matchesPlayed, minutesPlayed for the season/tournament being
#   described.
# - performance.position: exactly one of "GK" | "DF" | "MF" | "FW".
# - performance.team, performance.season, performance.tournament, performance.format: the
#   specific team/season/competition/format (e.g. "international", "club") the stats above
#   refer to — be specific, not generic.

# record_highlight.benchmarks (the National/Olympic/World record-comparison strip — ALWAYS
# return exactly 3 entries with label "National", "Olympic", "World" in that order, even when
# record_highlight itself has nothing for this athlete. Map to football's closest equivalents
# via `tournament`: "National" = the Indian national-team record for this stat category (e.g.
# most international goals), "Olympic" = the Olympic football tournament record for this stat
# category, "World" = the outright global record (e.g. most international goals ever, any
# nation). Fill value/holder/date/venue/tournament for each — these are well-documented facts;
# avoid leaving them null.

# analytics fields (derive from facts you actually find via search — avoid leaving these null
# when the underlying stat is realistically findable; only use {} after a genuine search comes
# up empty):
# - analytics.seasonalData: {"labels": [...seasons, most recent last...], "goals": [...],
#   "assists": [...]} — season-by-season figures if findable via a stats site. {} only if truly
#   just the single season/tournament you're already describing is findable.
# - analytics.radarData: {"axes": ["Finishing", "Playmaking", "Dribbling", "Availability"],
#   "values": [...four 0-100 scores...]}. These ARE your editorial estimates (like aiInsight),
#   not hard facts — score based on the actual attackingStats/playmakingStats/appearanceStats
#   you found relative to what's typical for the player's position (e.g. high shotConversionPct
#   scores well on "Finishing"; high minutesPlayed relative to squad availability scores well
#   on "Availability"). Do not default to a flat score for every axis. {} only if the
#   underlying stats are largely null.
# - analytics.consistencyData: {"note": <1 short factual sentence, e.g. a notable scoring
#   streak, if found>} — {} if nothing specific found.
# - analytics.coachImpactData: only fill if you find a clear, specific before/after comparison
#   tied to a documented manager change — {} is the expected/correct answer for most players.
# - analytics.heatmapData: {"zone": <e.g. "left channel", "central", "box">} only if a
#   reliable source specifically describes the player's typical pitch position/zone — {}
#   otherwise, not a miss.
# """


# def _build_prompt(
#     sport: Sport, athlete_name: str, focus_fields: list[str] | None = None, cricket_format: str | None = None
# ) -> str:
#     sport_id = _sport_id_for(sport)

#     base = f"""
# You are drafting a structured athlete document for SportsFan360, a sports fan engagement
# platform. You have access to Google Search — use it to look up current, accurate facts about
# this athlete rather than relying only on what you already know. Search for their official
# bio, current stats, and recent results before answering.

# Athlete name: {athlete_name}
# Sport category: {sport_id}{f' ({_CATEGORY_LABELS[sport]})' if sport in _TRACK_FIELD_SPORTS else ''}{f' — {cricket_format} career specifically' if cricket_format else ''}

# Produce a single JSON object with exactly these top-level keys: coreInfo, performance,
# record_highlight, analytics.

# NULL-AVOIDANCE: use null (JSON null) only after you have genuinely searched for a field and
# could not find or confirm it. Do not guess or invent numbers — but also do not default to
# null out of caution for facts that are realistically findable (a personal best, a coach's
# name, a well-known world/Olympic record). Accuracy matters more than completeness, and a
# human editor will review this draft, but an all-null draft is not more "safe" than a
# well-researched one — it just means the search wasn't tried hard enough.

# coreInfo fields: name, country, flag, gender, dob, heightCm, weightKg, bio, birthplace,
# coachName, yearsActiveSince.
# - gender MUST be exactly "Men" or "Women" — used for filtering, not editorial content.
# - flag is an emoji flag, e.g. "🇮🇳".
# - dob MUST be a full ISO date "YYYY-MM-DD" if known, otherwise null — never a bare year.
# - bio: 2-4 factual sentences, no editorializing.
# - coachName MUST be just the coach's plain name (e.g. "Rana Reider"), never a name with a
#   parenthetical annotation like "(former coach)" attached — if there's noteworthy context
#   about a coaching change, mention it in bio instead and put only the CURRENT coach's name
#   (or null if unknown/none) here.

# performance fields (this sport's specific shape):
# {_sport_specific_field_notes(sport, cricket_format=cricket_format)}

# record_highlight: for the match-center RECORD CARD, separate from the profile above. Search
# specifically for whether this athlete currently holds (or recently set) a notable,
# still-relevant record{f' in {cricket_format} specifically — do not report a record from a different cricket format' if cricket_format else ''}. If they do, fill the object: {{event, result, type, typeFull, phase,
# date, city, prevRecord, prevRecordPlace, prevHolder, improvement, improvementDate, aiInsight,
# progressData, benchmarks}}.
#     - type MUST be exactly one of: "GR", "NR", "WR", "CR", "PB"
#     - result / prevRecord / improvement MUST be plain strings in the record's native unit
#       (e.g. "88.13m", "9.85s", "5/17", "+2.53m", "-0.08s")
#     - prevRecordPlace is the city/venue where the OLD record was set — distinct from `city`,
#       which is where THIS record was set
#     - prevHolder is the athlete/team who held the record before this one
#     - improvementDate is the date the record was actually broken (usually the same as `date`)
#     - progressData: list of {{"year": "2022", "value": 88.13}} objects (value as a plain
#       number, no unit suffix), up to 6 most recent years, [] if not applicable
#     - aiInsight: 2-3 factual sentences on what made the performance notable, no unfounded
#       editorializing
#     - benchmarks: see the National/Olympic/World instructions above under "performance
#       fields" — always exactly 3 entries, regardless of whether the rest of
#       record_highlight is filled.
#     - If this athlete does NOT currently hold or hasn't recently set a notable record, set
#       every field EXCEPT benchmarks to null (event, result, type, etc. all null) — but still
#       populate benchmarks, since that's about the event's records, not the athlete's own. Do
#       not omit record_highlight entirely; return the object with benchmarks filled and the
#       rest null instead.

# If a list field has no data, use an empty list [], never null.

# Respond with ONLY a single JSON object containing coreInfo, performance, record_highlight,
# and analytics. No prose, no markdown code fences, no commentary before or after the JSON.
# """

#     if not focus_fields:
#         return base

#     focus_list = ", ".join(focus_fields)
#     return base + f"""

# IMPORTANT — TARGETED RETRY:
# A previous search pass could not find reliable data for these specific fields: {focus_list}

# For THIS pass, prioritize finding data for exactly these fields, trying different search
# angles than a generic profile search would use — e.g. the athlete's federation/league stats
# page directly, recent season recap articles, "<athlete name> coach" / "<athlete name>
# record", or for record_highlight.benchmarks specifically, "<event> world record" / "<event>
# Olympic record" / "<event> [country] national record". Only return null for a field in this
# list if, after genuinely attempting these searches, no reliable source has the information.

# Still return the FULL set of keys (coreInfo, performance, record_highlight), reusing whatever
# solid data you already have for the rest.
# """


# def _extract_json(raw_text: str, response_meta=None) -> dict:
#     raw = (raw_text or "").strip()
#     start = raw.find("{")
#     end = raw.rfind("}") + 1
#     if start == -1 or end == 0:
#         raise GenerationError(f"Gemini response did not contain valid JSON: {raw[:300]}")
#     try:
#         return json.loads(raw[start:end])
#     except json.JSONDecodeError as e:
#         hint = ""
#         try:
#             candidates = getattr(response_meta, "candidates", None) or []
#             finish_reason = getattr(candidates[0], "finish_reason", None) if candidates else None
#             if finish_reason and str(finish_reason).upper() != "STOP":
#                 hint = f" (finish_reason={finish_reason} — likely truncated, response may have exceeded max_output_tokens)"
#         except Exception:
#             pass
#         raise GenerationError(
#             f"Gemini response was not valid JSON: {e}{hint} [response length: {len(raw)} chars]"
#         ) from e


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
#         if isinstance(value, dict):
#             nums = [v for v in value.values() if isinstance(v, (int, float)) and not isinstance(v, bool)]
#             return int(min(nums)) if nums else None
#         return value
#     if target_type is float:
#         if isinstance(value, (int, float)) and not isinstance(value, bool):
#             return float(value)
#         if isinstance(value, str):
#             match = re.search(r"-?\d+\.?\d*", value)
#             return float(match.group()) if match else None
#         return value
#     return value


# def _coerce_to_model(data, model_cls, na_fields=None, path=""):
#     if not isinstance(data, dict):
#         return data
#     na_fields = na_fields or set()

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
#                     if isinstance(item, str):
#                         wrap_field = next(
#                             (
#                                 fn
#                                 for fn, f2 in item_type.model_fields.items()
#                                 if f2.is_required() and _unwrap_optional(f2.annotation)[0] is str
#                             ),
#                             None,
#                         )
#                         item = {wrap_field: item} if wrap_field else None
#                     if isinstance(item, dict):
#                         coerced_items.append(
#                             _coerce_to_model(item, item_type, na_fields=na_fields, path=field_path)
#                         )
#                 result[name] = coerced_items
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
#                 _coerce_to_model(value, annotation, na_fields=na_fields, path=field_path)
#                 if isinstance(value, dict)
#                 else value
#             )
#             continue

#         if annotation in (str, int, float):
#             coerced = _generic_coerce_scalar(value, annotation)
#             if coerced is None and annotation is str:
#                 if field_path in na_fields:
#                     coerced = "N/A"
#                 elif field.is_required():
#                     coerced = "N/A"
#             result[name] = coerced
#             continue

#         if annotation is date_type:
#             if isinstance(value, str):
#                 try:
#                     date_type.fromisoformat(value)
#                     result[name] = value
#                 except ValueError:
#                     result[name] = None
#             else:
#                 result[name] = value
#             continue

#         result[name] = value

#     return result


# def _normalize_record_type(record_highlight: Optional[dict]) -> Optional[dict]:
#     if not isinstance(record_highlight, dict):
#         return record_highlight
#     value = record_highlight.get("type")
#     if not isinstance(value, str):
#         return record_highlight
#     normalized = value.strip().upper()
#     if normalized in _VALID_RECORD_TYPES:
#         record_highlight["type"] = normalized
#         return record_highlight
#     name_map = {
#         "GAMES RECORD": "GR",
#         "NATIONAL RECORD": "NR",
#         "WORLD RECORD": "WR",
#         "CONTINENTAL RECORD": "CR",
#         "PERSONAL BEST": "PB",
#     }
#     record_highlight["type"] = name_map.get(normalized, value)
#     return record_highlight


# def _normalize_benchmarks(record_highlight: Optional[dict]) -> Optional[dict]:
#     """Ensures record_highlight.benchmarks always has exactly the 3
#     National/Olympic/World entries in order, regardless of what Gemini
#     returned (missing entries, extra entries, wrong order, or the whole
#     key omitted). Matches existing entries by label case-insensitively and
#     fills any missing label with an empty (label-only) BenchmarkRecord
#     rather than dropping it."""
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


# def _normalize_heatmap_tiers(analytics: Optional[dict]) -> Optional[dict]:
#     """Same idea as _normalize_benchmarks, for analytics.heatmapData —
#     always exactly the 3 _HEATMAP_TOURNAMENT_TIERS entries in order,
#     regardless of what Gemini returned."""
#     if not isinstance(analytics, dict):
#         return analytics
#     raw = analytics.get("heatmapData")
#     by_tournament = {}
#     if isinstance(raw, list):
#         for entry in raw:
#             if isinstance(entry, dict) and isinstance(entry.get("tournament"), str):
#                 by_tournament[entry["tournament"].strip().lower()] = entry
#     analytics["heatmapData"] = [
#         {**by_tournament.get(t.lower(), {}), "tournament": t} for t in _HEATMAP_TOURNAMENT_TIERS
#     ]
#     return analytics


# def _sanitize(data: dict) -> dict:
#     core_info = data.get("coreInfo")
#     if isinstance(core_info, dict):
#         coach = core_info.get("coachName")
#         if isinstance(coach, str):
#             core_info["coachName"] = re.sub(r"\s*\([^)]*\)", "", coach).strip() or None

#     record_highlight = data.get("record_highlight")
#     record_highlight = _normalize_record_type(record_highlight)
#     record_highlight = _normalize_benchmarks(record_highlight)
#     data["record_highlight"] = record_highlight

#     if isinstance(data.get("analytics"), dict):
#         data["analytics"] = _normalize_heatmap_tiers(data["analytics"])

#     return data


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
#     seen = set()
#     deduped = []
#     for u in urls:
#         if u not in seen:
#             seen.add(u)
#             deduped.append(u)
#     return deduped


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


# def _generate_single_pass(
#     athlete_name: str, sport: Sport, focus_fields: list[str] | None = None, cricket_format: str | None = None
# ):
#     prompt = _build_prompt(sport, athlete_name, focus_fields=focus_fields, cricket_format=cricket_format)
#     response = _call_gemini(prompt)
#     try:
#         data = _extract_json(response.text, response_meta=response)
#     except GenerationError:
#         response = _call_gemini(prompt)
#         data = _extract_json(response.text, response_meta=response)
#     return data, response


# def generate_athlete_profile(athlete_name: str, sport: Sport, max_passes: int = 3, cricket_format: str | None = None):
#     """
#     cricket_format ("Test" | "ODI" | "T20") pins which format's career
#     stats/record to draft — cricketers have wildly different career
#     numbers per format, and without pinning one, search-grounded
#     generation picks a different format on different calls for the same
#     athlete. Required for Sport.CRICKET, ignored otherwise.
#     """
#     schema_cls = get_schema_for_sport(sport)

#     data, response = _generate_single_pass(athlete_name, sport, cricket_format=cricket_format)
#     data = _sanitize(data)
#     data = _coerce_to_model(data, schema_cls, na_fields=set())

#     data["athlete_id"] = _slugify(athlete_name)
#     data["sportId"] = _sport_id_for(sport)
#     if sport in _TRACK_FIELD_SPORTS:
#         data.setdefault("performance", {})["category"] = _CATEGORY_LABELS[sport]
#         # These 4 are fixed by category, not researched facts, so the
#         # pipeline sets them directly (same reasoning as `category`
#         # itself) rather than trusting Gemini to pick consistently.
#         analytics = data.setdefault("analytics", {})
#         for k, v in _ANALYTICS_DEFAULTS_BY_CATEGORY[sport].items():
#             analytics[k] = v  # forced, same as performance.category — fixed by category, not a research field
#         analytics["heatmapData"] = _normalize_heatmap_tiers({"heatmapData": analytics.get("heatmapData")})["heatmapData"]
#         data["analytics"] = analytics
#     if sport == Sport.CRICKET and cricket_format:
#         data.setdefault("performance", {})["format"] = cricket_format
#     if isinstance(data.get("coreInfo"), dict):
#         data["coreInfo"]["name"] = athlete_name
#     if not isinstance(data.get("analytics"), dict):
#         data["analytics"] = {}  # Gemini omitted it entirely -- Analytics' defaults fill the 5 empty containers
#     if not isinstance(data.get("record_highlight"), dict):
#         # Gemini omitted record_highlight entirely (or returned null) --
#         # still stand up the object so benchmarks (event-level, not
#         # athlete-specific) get populated via retry rather than staying
#         # permanently absent.
#         data["record_highlight"] = {}
#     data["record_highlight"] = _normalize_benchmarks(data["record_highlight"])
#     data["source"] = SourceRef(
#         source_name="Gemini (search-grounded — unverified, needs human review)",
#         source_url="https://sportsfan360.internal/llm-draft",  # placeholder, not a real citation
#         fetched_at=datetime.now(timezone.utc).isoformat(),
#     ).model_dump(mode="json")

#     grounding_sources = _extract_grounding_sources(response)
#     retry_log = []

#     for pass_num in range(2, max_passes + 1):
#         missing = _missing_retryable_paths(data, sport)
#         if not missing:
#             break

#         retry_data, retry_response = _generate_single_pass(
#             athlete_name, sport, focus_fields=missing, cricket_format=cricket_format
#         )
#         retry_data = _sanitize(retry_data)
#         retry_data = _coerce_to_model(retry_data, schema_cls, na_fields=set())

#         filled_this_pass = []
#         for p in missing:
#             if p in ("record_highlight.benchmarks", "analytics.heatmapData"):
#                 key_names = ("value", "holder") if p.endswith("benchmarks") else ("years",)
#                 candidate = _get_path(retry_data, p)
#                 if isinstance(candidate, list) and _tiered_list_has_data(candidate, key_names):
#                     _set_path(data, p, candidate)
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

#     data["grounding_sources"] = list(dict.fromkeys(grounding_sources))

#     profile = schema_cls.model_validate(data)

#     profile_dict = profile.model_dump(mode="json")
#     profile_dict["_retry_log"] = retry_log
#     return profile, profile_dict


# def _prompt_sport() -> Sport:
#     options = list(Sport)
#     print("\nSport:")
#     for i, s in enumerate(options, 1):
#         label = _CATEGORY_LABELS.get(s, s.value.title())
#         print(f"  {i}. {label}" + (" (athletics)" if s in _TRACK_FIELD_SPORTS else ""))
#     while True:
#         choice = input("Choose a number: ").strip()
#         if choice.isdigit() and 1 <= int(choice) <= len(options):
#             return options[int(choice) - 1]
#         print("Invalid choice, try again.")


# def _prompt_cricket_format() -> str:
#     options = ["Test", "ODI", "T20"]
#     print("\nCricket format (career stats differ a lot by format — pick one):")
#     for i, f in enumerate(options, 1):
#         print(f"  {i}. {f}")
#     while True:
#         choice = input("Choose a number: ").strip()
#         if choice.isdigit() and 1 <= int(choice) <= len(options):
#             return options[int(choice) - 1]
#         print("Invalid choice, try again.")


# def main():
#     os.makedirs(OUTPUT_DIR, exist_ok=True)

#     while True:
#         athlete_name = input("\nAthlete name (blank to quit): ").strip()
#         if not athlete_name:
#             print("Done.")
#             break

#         sport = _prompt_sport()
#         cricket_format = _prompt_cricket_format() if sport == Sport.CRICKET else None

#         print(f"\nGenerating draft for {athlete_name} ({_sport_id_for(sport)}"
#               f"{f', {cricket_format}' if cricket_format else ''})...")
#         try:
#             profile, profile_dict = generate_athlete_profile(athlete_name, sport, cricket_format=cricket_format)
#         except (GenerationError, ValidationError) as e:
#             print(f"FAILED: {e}", file=sys.stderr)
#             continue

#         out_path = os.path.join(OUTPUT_DIR, f"{profile.athlete_id}.json")
#         with open(out_path, "w") as f:
#             json.dump(profile_dict, f, indent=2, default=str)

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
generate_athlete_content_llm.py

Standalone content generator: prompts Gemini (with Google Search grounding
enabled) to draft an athlete document matching sportsfan360-schema-v4.md —
`coreInfo` (identity/bio), `performance` (sport-specific stats), and
`record_highlight` (the athlete's most notable record, for the match-center
record card + the National/Olympic/World benchmark-comparison strip).
Scoped to the 3 priority sports: cricket, football, and track & field (one
`sportId`, "athletics", covering 6 sub-disciplines: sprints,
middle_distance, long_distance, throws, jumps, relays).

`analytics` (seasonalData/radarData/coachImpactData/consistencyData/
heatmapData) IS drafted by Gemini. radarData's axes are now CATEGORY-
SPECIFIC — a sprinter is scored on Reaction/Top Speed/Acceleration, a
thrower on Power/Technique/Release, etc. (see _RADAR_AXES_BY_CATEGORY) —
rather than one generic axis set stretched across every sport.

Fields should be FILLED, not left null, whenever the underlying fact is
realistically findable via search (a personal best, a world record holder,
a coach's name). Gemini is told this explicitly and should only null a
field after genuinely searching and coming up empty — see
"NULL-AVOIDANCE" in the prompt.

Also NOT drafted: the platform-engagement fields from the schema doc's
top level (isVerified, fanImpactScore, fansCount, badges, hubCounts,
featuredContent, activeRecordsHeld) — these are internal platform state,
not facts about the athlete, so they're outside this script's job. It
outputs `coreInfo` + `performance` + `analytics` + `record_highlight`,
meant to be merged into the rest of the document by the API route, not
the whole document itself.

Everything still lives in this one file on purpose — schemas, the generic
coercion engine, the prompt builder, and the CLI — after an earlier split
into separate files led to a circular import. One file, one
`python generate_athlete_content_llm.py`.

Run it, enter one athlete name + pick a sport at a time. Each run:
  1. builds a prompt asking Gemini for JSON matching this sport's document
     shape, with Google Search grounding turned on
  2. parses + validates the response against the schemas below
  3. if key fields are still empty/null after pass 1, runs up to 2 more
     targeted retry passes, merging in anything newly found field-by-field
     (already-filled fields are never overwritten)
  4. prints the validated JSON, saves it to
     ./llm_athlete_drafts/<athlete_id>.json, AND writes it into the
     DynamoDB review queue (SportsData, status=pending_review) via
     firebase_store.save_review_draft() — same review-queue pattern
     athlete_pipeline.py already uses.

NOTE: search-grounded LLM output can still be wrong or out of date — this is
a content-drafting aid for the review queue, not a source-of-truth stats
feed. Every draft still needs human review before publishing.
"""

import json
import os
import re
import sys
import typing
from datetime import date as date_type, datetime, timezone
from enum import Enum
from typing import Optional

from google import genai
from google.genai import types
from pydantic import BaseModel, Field, HttpUrl, ValidationError

import firebase_store


# ═══════════════════════════════════════════════════════════════════════
# SCHEMAS
# ═══════════════════════════════════════════════════════════════════════

class Sport(str, Enum):
    SPRINTS = "sprints"
    MIDDLE_DISTANCE = "middle_distance"
    LONG_DISTANCE = "long_distance"
    THROWS = "throws"
    JUMPS = "jumps"
    RELAYS = "relays"
    CRICKET = "cricket"
    FOOTBALL = "football"


_TRACK_FIELD_SPORTS = {
    Sport.SPRINTS,
    Sport.MIDDLE_DISTANCE,
    Sport.LONG_DISTANCE,
    Sport.THROWS,
    Sport.JUMPS,
    Sport.RELAYS,
}

_CATEGORY_LABELS: dict[Sport, str] = {
    Sport.SPRINTS: "Sprints",
    Sport.MIDDLE_DISTANCE: "Middle Distance",
    Sport.LONG_DISTANCE: "Long Distance",
    Sport.THROWS: "Throws",
    Sport.JUMPS: "Jumps",
    Sport.RELAYS: "Relays",
}

# Category -> the 5 radar axes Gemini should score for THIS discipline,
# matching the real analytics widget's radarData shape (list of
# {metric, value, fullMark}, 5 entries). Distinct per category rather than
# one generic set stretched across every sport (the old "Avg Distance" /
# "Best Throw" placeholders that showed up identically for every athlete
# regardless of event were wrong — a sprinter and a thrower are good at
# fundamentally different things). "Growth" is always the 5th axis, shared
# across categories, since it's a universal year-over-year trend score.
_RADAR_AXES_BY_CATEGORY: dict[Sport, list[str]] = {
    Sport.SPRINTS: ["Reaction Time", "Top Speed", "Acceleration", "Consistency", "Growth"],
    Sport.MIDDLE_DISTANCE: ["Pace Judgment", "Kick Speed", "Endurance", "Consistency", "Growth"],
    Sport.LONG_DISTANCE: ["Endurance", "Pace Judgment", "Race Tactics", "Consistency", "Growth"],
    Sport.THROWS: ["Power", "Technique", "Release Consistency", "Big-Meet Performance", "Growth"],
    Sport.JUMPS: ["Explosiveness", "Technique", "Approach Consistency", "Big-Meet Performance", "Growth"],
    Sport.RELAYS: ["Baton Exchange", "Leg Speed", "Consistency", "Team Synergy", "Growth"],
}

# Category -> (metricLabel, unit, tooltipValueSuffix, consistencyBarLabel),
# pipeline-set (like `category` itself) rather than left for Gemini to
# pick, since these are fixed per discipline, not researched facts.
_ANALYTICS_DEFAULTS_BY_CATEGORY: dict[Sport, dict] = {
    Sport.SPRINTS: {"metricLabel": "Time", "unit": "s", "tooltipValueSuffix": "s", "consistencyBarLabel": "attempts"},
    Sport.MIDDLE_DISTANCE: {"metricLabel": "Time", "unit": "s", "tooltipValueSuffix": "s", "consistencyBarLabel": "attempts"},
    Sport.LONG_DISTANCE: {"metricLabel": "Time", "unit": "s", "tooltipValueSuffix": "s", "consistencyBarLabel": "attempts"},
    Sport.THROWS: {"metricLabel": "Distance", "unit": "m", "tooltipValueSuffix": "m", "consistencyBarLabel": "throws"},
    Sport.JUMPS: {"metricLabel": "Distance", "unit": "m", "tooltipValueSuffix": "m", "consistencyBarLabel": "attempts"},
    Sport.RELAYS: {"metricLabel": "Time", "unit": "s", "tooltipValueSuffix": "s", "consistencyBarLabel": "attempts"},
}

_HEATMAP_TOURNAMENT_TIERS = ["Olympics", "World Champ", "National Champ"]


def _sport_id_for(sport: Sport) -> str:
    if sport in _TRACK_FIELD_SPORTS:
        return "athletics"
    return sport.value  # "cricket" | "football"


class SourceRef(BaseModel):
    source_name: str = Field(default="Gemini (Google Search grounding)")
    source_url: Optional[HttpUrl] = None
    fetched_at: str  # ISO8601 timestamp, set by the pipeline


class CoreInfo(BaseModel):
    name: str
    country: Optional[str] = None
    flag: Optional[str] = Field(None, description="emoji flag, e.g. '🇮🇳'")
    gender: Optional[str] = Field(None, description="'Men' or 'Women' — used for filtering")
    dob: Optional[date_type] = None
    heightCm: Optional[int] = None
    weightKg: Optional[float] = None
    profileImage: Optional[str] = None  # not LLM-drafted, left null for editors
    bio: Optional[str] = Field(None, description="2-4 sentence factual summary, no editorializing")
    birthplace: Optional[str] = None
    coachName: Optional[str] = None
    yearsActiveSince: Optional[str] = Field(None, description="e.g. '2018'")

    model_config = {"extra": "forbid"}


class ProgressPoint(BaseModel):
    year: str = Field(..., description="e.g. '2022' — a chart label, kept as a string, not a date")
    value: float = Field(
        ..., description="numeric mark for that year in the record's native unit (seconds, meters, points), no unit suffix"
    )

    model_config = {"extra": "forbid"}


class BenchmarkRecord(BaseModel):
    """
    One row of the National/Olympic/World record-comparison strip. Always
    present as 3 entries on every RecordHighlight — label is fixed, the
    rest is filled with whatever Gemini can find for that tier. World and
    Olympic records are usually well-documented, stable facts (e.g. Usain
    Bolt's 9.58s 100m WR) — these should essentially never end up null;
    National can be genuinely unknown for less-tracked events, and that's
    fine to leave null after a real search attempt.
    """
    label: str = Field(..., description="exactly one of: 'National', 'Olympic', 'World'")
    value: Optional[str] = Field(None, description="the record mark in its native unit, e.g. '9.58s', '90.23m'")
    holder: Optional[str] = Field(None, description="athlete/team who holds this record")
    date: Optional[str] = Field(None, description="display date the record was set, e.g. 'Aug 16, 2009'")
    venue: Optional[str] = Field(None, description="city/venue where this record was set")
    tournament: Optional[str] = Field(None, description="event/meet name, e.g. 'Berlin World Championships'")

    model_config = {"extra": "forbid"}


_BENCHMARK_LABELS = ["National", "Olympic", "World"]


class RecordHighlight(BaseModel):
    """
    The athlete's most notable record for the match-center record card —
    NOT every record they've ever set, just the single most significant
    still-relevant one — plus the National/Olympic/World benchmark strip
    (`benchmarks`) that compares the event's own top marks against those 3
    tiers regardless of whether the athlete personally holds any of them.
    """
    event: Optional[str] = Field(None, description="e.g. \"Men's Javelin Throw\", \"Most T20I Wickets\"")
    result: Optional[str] = Field(None, description="the record mark, e.g. '88.13m', '5/17', '3 goals in a match'")
    type: Optional[str] = Field(
        None, description="one of: GR (Games Record), NR (National Record), WR (World Record), CR (Continental Record), PB (Personal Best)"
    )
    typeFull: Optional[str] = Field(None, description="e.g. 'Games Record', 'World Record'")
    phase: Optional[str] = Field(None, description="e.g. 'Final', 'Semifinal'")
    date: Optional[str] = Field(None, description="display date the record was set, e.g. 'Aug 7, 2026'")
    city: Optional[str] = Field(None, description="city/venue where THIS record was set")
    prevRecord: Optional[str] = Field(None, description="the mark this record broke")
    prevRecordPlace: Optional[str] = Field(
        None, description="city/venue where the PREVIOUS (old) record was set — distinct from `city`"
    )
    prevHolder: Optional[str] = Field(None, description="athlete/team who held the previous record")
    improvement: Optional[str] = Field(None, description="signed margin, e.g. '+2.53m', '-0.08s'")
    improvementDate: Optional[str] = Field(
        None, description="date the record was actually broken — should match `date` unless known otherwise"
    )
    aiInsight: Optional[str] = Field(
        None, description="2-3 factual sentences on what made the performance notable, no unfounded editorializing"
    )
    progressData: list[ProgressPoint] = Field(
        default_factory=list,
        description="the athlete's progression toward this record, up to the 6 most recent years; [] if not applicable",
    )
    benchmarks: list[BenchmarkRecord] = Field(
        default_factory=lambda: [BenchmarkRecord(label=l) for l in _BENCHMARK_LABELS],
        description="always exactly 3 entries, one per _BENCHMARK_LABELS ('National', 'Olympic', 'World'), for the record-comparison strip",
    )

    model_config = {"extra": "forbid"}


class Analytics(BaseModel):
    """Generic fallback analytics shape — used for cricket/football, where
    no reference export like the athletics one below exists yet. Keep
    using dict containers there until a real cricket/football analytics
    sample is available to match field-for-field the way AthleticsAnalytics
    now does."""
    seasonalData: dict = Field(default_factory=dict)
    radarData: dict = Field(default_factory=dict)
    coachImpactData: dict = Field(default_factory=dict)
    consistencyData: dict = Field(default_factory=dict)
    heatmapData: dict = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


# ── AthleticsAnalytics — matches the real analytics-widget export field-
# for-field (see csvjson.json sample: gurindervir-singh, neeraj-chopra,
# etc.), not a generic guessed shape. Every sub-model's field names mirror
# that export exactly so the pipeline output can be merged straight in. ──

class SeasonalDataPoint(BaseModel):
    year: str
    event: Optional[str] = None
    value: Optional[float] = None
    rank: Optional[int] = None
    highlight: bool = False
    glow: bool = False

    model_config = {"extra": "forbid"}


class MedalDataPoint(BaseModel):
    year: str
    gold: int = 0
    silver: int = 0
    bronze: int = 0

    model_config = {"extra": "forbid"}


class RadarDataPoint(BaseModel):
    metric: str
    value: Optional[float] = Field(None, description="0-100 score")
    fullMark: float = 100

    model_config = {"extra": "forbid"}


class ConsistencyDataPoint(BaseModel):
    range: str = Field(..., description="e.g. '10.2–10.3s', '83-85m'")
    count: Optional[int] = None
    throws: Optional[int] = Field(None, description="attempt count for this bucket — same number as `count`, kept for export compatibility")
    percentage: Optional[float] = None
    peak: bool = False

    model_config = {"extra": "forbid"}


class CoachImpactPoint(BaseModel):
    year: int
    value: Optional[float] = None
    period: str = Field(..., description="'before' | 'after' (relative to coachJoinYear)")

    model_config = {"extra": "forbid"}


class BenchmarkTier(BaseModel):
    """One entry of analytics.heatmapData — despite the legacy name, this
    is the Olympics/World Champ/National Champ comparison tier data, NOT a
    spatial heatmap. Always exactly 3 entries (see _HEATMAP_TOURNAMENT_TIERS),
    `years` holding whatever record/best-mark data is findable for that
    tier (kept as a free dict since the export itself leaves it as an
    open bag, e.g. {"value": "9.58s", "holder": "Usain Bolt", "date": ...})."""
    tournament: str = Field(..., description="exactly one of: 'Olympics', 'World Champ', 'National Champ'")
    years: dict = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class AthleteStats(BaseModel):
    worldRank: Optional[str] = None
    personalBest: Optional[str] = None
    bestYear: Optional[str] = None
    totalGold: Optional[int] = None
    totalSilver: Optional[int] = None
    totalBronze: Optional[int] = None
    olympicGold: Optional[int] = None

    model_config = {"extra": "forbid"}


class AthleticsAnalytics(BaseModel):
    """Track & field analytics — field names and shapes match the real
    export exactly, NOT the generic nested-dict Analytics model above.
    Fields Gemini should fill via search/derivation; only heatmapData's
    tournament labels, consistencyBarLabel/metricLabel/unit/
    tooltipValueSuffix are pipeline-set (see _ANALYTICS_DEFAULTS_BY_CATEGORY,
    _HEATMAP_TOURNAMENT_TIERS) rather than drafted, since those are fixed
    by category, not researched facts."""
    heroLabel: str = "Personal Best"
    heroStat: Optional[str] = None
    metricLabel: Optional[str] = Field(None, description="'Time' | 'Distance'")
    unit: Optional[str] = Field(None, description="'s' | 'm'")
    tooltipValueSuffix: Optional[str] = None
    yAxisDomain: list[float] = Field(default_factory=list, description="[min, max]")
    sport: Optional[str] = Field(None, description="display event name, e.g. '100m Sprint', 'Long Jump', 'Javelin Throw', 'Decathlon'")
    achievementLabel: Optional[str] = Field(None, description="a short badge, e.g. 'Asian U18 Champion'")
    stats: Optional[AthleteStats] = None
    seasonalData: list[SeasonalDataPoint] = Field(default_factory=list)
    medalData: list[MedalDataPoint] = Field(default_factory=list)
    radarData: list[RadarDataPoint] = Field(default_factory=list)
    consistencyBarLabel: Optional[str] = None
    consistencyData: list[ConsistencyDataPoint] = Field(default_factory=list)
    consistencyNote: Optional[str] = None
    peakZoneLabel: Optional[str] = Field(None, description="e.g. '10.2–10.3s · 36% success rate', derived from consistencyData's peak bucket")
    heatmapData: list[BenchmarkTier] = Field(
        default_factory=lambda: [BenchmarkTier(tournament=t) for t in _HEATMAP_TOURNAMENT_TIERS],
        description="always exactly 3 entries, one per _HEATMAP_TOURNAMENT_TIERS",
    )
    beforeCoach: Optional[str] = Field(None, description="the athlete's performance mark BEFORE coachJoinYear, e.g. '10.38s' — a mark, not a name")
    afterCoach: Optional[str] = Field(None, description="the athlete's performance mark AFTER coachJoinYear, e.g. '10.09s' — a mark, not a name")
    coachImprovement: Optional[str] = Field(None, description="signed delta + %, e.g. '+0.13m (+7.2%)', or 'Data not available'")
    coachJoinYear: Optional[int] = None
    coachImpactData: list[CoachImpactPoint] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class MedalEntry(BaseModel):
    event: str = Field(..., description="e.g. 'CWG 2026'")
    medal: str = Field(..., description="'GOLD' | 'SILVER' | 'BRONZE'")
    category: Optional[str] = Field(None, description="e.g. '100m'")

    model_config = {"extra": "forbid"}


class AthleticsStats(BaseModel):
    personalBest: Optional[str] = None
    seasonBest: Optional[str] = None

    model_config = {"extra": "forbid"}


class AthleticsChartConfig(BaseModel):
    yAxisDomain: list[float] = Field(default_factory=list, description="[min, max]")
    unit: Optional[str] = Field(None, description="'seconds' | 'meters'")

    model_config = {"extra": "forbid"}


class AthleticsPerformance(BaseModel):
    primaryEvent: Optional[str] = Field(None, description="e.g. '100m', 'Javelin Throw'")
    category: Optional[str] = Field(
        None, description="'Sprints' | 'Middle Distance' | 'Long Distance' | 'Throws' | 'Jumps' | 'Relays'"
    )
    preferredLane: Optional[int] = None
    currentSeason: Optional[str] = None
    medalCabinet: list[MedalEntry] = Field(default_factory=list)
    stats: Optional[AthleticsStats] = None
    chartConfig: Optional[AthleticsChartConfig] = None

    model_config = {"extra": "forbid"}


class CricketBattingStats(BaseModel):
    runs: Optional[int] = None
    ballsFaced: Optional[int] = None
    battingAverage: Optional[float] = None
    strikeRate: Optional[float] = None
    fours: Optional[int] = None
    sixes: Optional[int] = None
    dismissals: Optional[int] = None

    model_config = {"extra": "forbid"}


class CricketBowlingStats(BaseModel):
    oversBowled: Optional[float] = None
    ballsBowled: Optional[int] = None
    runsConceded: Optional[int] = None
    wickets: Optional[int] = None
    economy: Optional[float] = None
    bowlingAverage: Optional[float] = None

    model_config = {"extra": "forbid"}


class CricketChartConfig(BaseModel):
    battingUnit: str = "runs"
    bowlingUnit: str = "wickets"

    model_config = {"extra": "forbid"}


class CricketPerformance(BaseModel):
    format: Optional[str] = Field(None, description="e.g. 'T20'")
    tournament: Optional[str] = Field(None, description="e.g. 'womens_t20i', 'womens_ipl'")
    jerseyNo: Optional[int] = None
    battingStats: Optional[CricketBattingStats] = None
    bowlingStats: Optional[CricketBowlingStats] = None
    chartConfig: CricketChartConfig = Field(default_factory=CricketChartConfig)

    model_config = {"extra": "forbid"}


class FootballAttackingStats(BaseModel):
    goals: Optional[int] = None
    assists: Optional[int] = None
    shots: Optional[int] = None
    shotsOnTarget: Optional[int] = None
    shotConversionPct: Optional[float] = None
    xg: Optional[float] = None
    xa: Optional[float] = None

    model_config = {"extra": "forbid"}


class FootballPlaymakingStats(BaseModel):
    chancesCreated: Optional[int] = None
    bigChancesCreated: Optional[int] = None
    keyPasses: Optional[int] = None
    dribblesCompleted: Optional[int] = None

    model_config = {"extra": "forbid"}


class FootballAppearanceStats(BaseModel):
    matchesPlayed: Optional[int] = None
    minutesPlayed: Optional[int] = None

    model_config = {"extra": "forbid"}


class FootballChartConfig(BaseModel):
    primaryUnit: str = "goals"
    secondaryUnit: str = "xg"

    model_config = {"extra": "forbid"}


class FootballPerformance(BaseModel):
    position: Optional[str] = Field(None, description="'GK' | 'DF' | 'MF' | 'FW'")
    team: Optional[str] = None
    season: Optional[int] = None
    tournament: Optional[str] = None
    format: Optional[str] = Field(None, description="e.g. 'international', 'club'")
    attackingStats: Optional[FootballAttackingStats] = None
    playmakingStats: Optional[FootballPlaymakingStats] = None
    appearanceStats: Optional[FootballAppearanceStats] = None
    chartConfig: FootballChartConfig = Field(default_factory=FootballChartConfig)

    model_config = {"extra": "forbid"}


class AthleteDocumentBase(BaseModel):
    athlete_id: str
    sportId: str
    coreInfo: CoreInfo
    record_highlight: Optional[RecordHighlight] = Field(
        None, description="null only if generation failed to run at all; otherwise always returned with at least benchmarks populated"
    )
    analytics: Analytics = Field(default_factory=Analytics)
    grounding_sources: list[str] = Field(
        default_factory=list,
        description="URLs Gemini's search grounding actually cited for this draft, for reviewer spot-checking",
    )
    source: SourceRef

    model_config = {"extra": "forbid"}


class AthleticsAthleteDocument(AthleteDocumentBase):
    performance: AthleticsPerformance
    analytics: AthleticsAnalytics = Field(default_factory=AthleticsAnalytics)


class CricketAthleteDocument(AthleteDocumentBase):
    performance: CricketPerformance


class FootballAthleteDocument(AthleteDocumentBase):
    performance: FootballPerformance


def get_schema_for_sport(sport: Sport) -> type[AthleteDocumentBase]:
    if sport in _TRACK_FIELD_SPORTS:
        return AthleticsAthleteDocument
    if sport == Sport.CRICKET:
        return CricketAthleteDocument
    return FootballAthleteDocument


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

GEMINI_MODEL = os.getenv("ATHLETE_EXTRACTION_MODEL", "gemini-2.5-flash")
OUTPUT_DIR = "llm_athlete_drafts"
_VALID_RECORD_TYPES = {"GR", "NR", "WR", "CR", "PB"}


class GenerationError(Exception):
    pass


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


RETRYABLE_PATHS: dict[Sport, list[str]] = {
    **{
        s: [
            "coreInfo.coachName",
            "performance.medalCabinet",
            "performance.stats.personalBest",
            "performance.stats.seasonBest",
            "record_highlight",
            "record_highlight.benchmarks",
            "analytics.seasonalData",
            "analytics.radarData",
            "analytics.medalData",
            "analytics.stats",
            "analytics.consistencyData",
            "analytics.heatmapData",
        ]
        for s in _TRACK_FIELD_SPORTS
    },
    Sport.CRICKET: [
        "coreInfo.coachName",
        "performance.tournament",
        "performance.jerseyNo",
        "performance.battingStats",
        "performance.bowlingStats",
        "record_highlight",
        "record_highlight.benchmarks",
        "analytics.seasonalData",
        "analytics.radarData",
    ],
    Sport.FOOTBALL: [
        "coreInfo.coachName",
        "performance.team",
        "performance.attackingStats",
        "performance.playmakingStats",
        "record_highlight",
        "record_highlight.benchmarks",
        "analytics.seasonalData",
        "analytics.radarData",
    ],
}


def _tiered_list_has_data(entries: list, key_names: tuple[str, ...]) -> bool:
    """Shared check for the two 'always 3 label-only entries by default'
    lists (record_highlight.benchmarks, analytics.heatmapData) — true if
    ANY entry has actual content beyond its fixed label/tournament key."""
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        for k in key_names:
            if not _is_empty(e.get(k)):
                return True
    return False


def _missing_retryable_paths(data: dict, sport: Sport) -> list[str]:
    missing = []
    for p in RETRYABLE_PATHS[sport]:
        if p == "record_highlight.benchmarks":
            benches = _get_path(data, "record_highlight.benchmarks") or []
            if not _tiered_list_has_data(benches, ("value", "holder")):
                missing.append(p)
            continue
        if p == "analytics.heatmapData":
            tiers = _get_path(data, "analytics.heatmapData") or []
            if not _tiered_list_has_data(tiers, ("years",)):
                missing.append(p)
            continue
        if _is_empty(_get_path(data, p)):
            missing.append(p)
    return missing


def _sport_specific_field_notes(sport: Sport, cricket_format: str | None = None) -> str:
    if sport in _TRACK_FIELD_SPORTS:
        axes = _RADAR_AXES_BY_CATEGORY[sport]
        axes_list = ", ".join(f'"{a}"' for a in axes)
        return f"""
- performance.category MUST be exactly "{_CATEGORY_LABELS[sport]}" (already fixed by the
  category you were asked to draft for — do not change it).
- performance.primaryEvent: the specific event within this category, e.g. for Sprints use
  "100m", "200m", "400m", or "400m Hurdles"; for Jumps use "Long Jump", "High Jump", "Triple
  Jump", or "Pole Vault"; for Throws use "Javelin Throw", "Shot Put", "Discus Throw", or
  "Hammer Throw"; for Relays use "4x100m Relay" or "4x400m Relay". Search for the athlete's
  actual specific event — never leave generic.
- performance.medalCabinet: list of {{event, medal, category}}, medal is exactly "GOLD" |
  "SILVER" | "BRONZE". Include at most the 8 most significant international medals.
- performance.stats.personalBest / seasonBest: plain strings in the event's native unit,
  e.g. "9.99s" or "90.23 m" — never bare numbers.
- performance.chartConfig.unit: "seconds" for track events, "meters" for field events.
  chartConfig.yAxisDomain: a sensible [min, max] range around the athlete's personal best
  (e.g. personal best 9.99s -> [9.5, 10.5]) — leave [] only if you genuinely cannot estimate
  one (you almost always can once you know the personal best).
- performance.preferredLane: only fill if you find a specific, reliably-reported lane
  preference; otherwise null — this is commonly unknown and that's fine.

record_highlight.benchmarks (the National/Olympic/World record-comparison strip — ALWAYS
return exactly 3 entries with label "National", "Olympic", "World" in that order, even when
record_highlight itself has nothing for this athlete — this compares the EVENT's records,
not just this athlete's own):
- For each: value (the record mark), holder (athlete/team), date, venue, tournament/meet
  name. World and Olympic records for well-known events (e.g. 100m, javelin, marathon) are
  stable, well-documented facts — search for and fill these confidently; do not leave them
  null just because the record isn't this athlete's own. National can genuinely be
  hard-to-verify for less-tracked events/countries — null is acceptable there ONLY after an
  actual search attempt.
- If the athlete's OWN personal best equals or is close to one of these tiers, still report
  the tier's actual official record (which may be someone else's), not the athlete's mark.

analytics fields — these match a real export format field-for-field, NOT a generic guessed
shape, so use these EXACT keys. Derive every value from facts you actually find via search;
avoid leaving fields null or empty when the underlying stat is realistically findable — only
use an empty list/dict/null after a genuine search attempt comes up empty:
- analytics.heroStat: the athlete's headline personal-best mark as a display string, e.g.
  "10.09s", "90.23m" (heroLabel is always "Personal Best", already set).
- analytics.sport: the specific display event name, e.g. "100m Sprint", "Long Jump",
  "Javelin Throw", "3000m Steeplechase", "High Jump", "Decathlon" — matches
  performance.primaryEvent in meaning but written as the full display label.
- analytics.achievementLabel: one short badge phrase, e.g. "Asian U18 Champion", "Tokyo 2020
  Olympic Gold" — the single most notable achievement, not a full list.
- analytics.stats: {{worldRank, personalBest, bestYear, totalGold, totalSilver, totalBronze,
  olympicGold}} — career-total medal counts and current world ranking, all searchable facts.
- analytics.seasonalData: list of {{year, event, value, rank, highlight, glow}} objects, one
  per year across the athlete's career (most recent last) — value as a plain number in the
  event's native unit, no suffix; highlight=true for personal-best/breakthrough years,
  glow=true for the single standout result. Reuse the same year-by-year marks you found for
  record_highlight.progressData rather than re-deriving them.
- analytics.medalData: list of {{year, gold, silver, bronze}} objects — medal counts PER YEAR
  (not career totals — those go in stats), one entry per year with any international medal.
- analytics.radarData: list of exactly 5 {{metric, value, fullMark}} objects, metric being
  each of [{axes_list}] IN THAT ORDER, fullMark always 100, value your 0-100 editorial
  estimate (like aiInsight) based on what your research actually showed — do not default to
  a flat score for every metric, vary them based on what you found. These axes are specific
  to {_CATEGORY_LABELS[sport]} — do not substitute a generic axis set like "Avg Distance"
  or "Best Throw" for a discipline they don't fit.
- analytics.consistencyData: list of {{range, count, throws, percentage, peak}} objects — a
  histogram of the athlete's performance distribution across ~5-6 buckets of their native
  unit (e.g. "10.2–10.3s" buckets for a sprinter, "83-85m" buckets for a thrower), percentage
  summing to ~100 across buckets, peak=true for the bucket(s) with the highest count. Derive
  this from whatever season-by-season / meet-by-meet results you can find; a reasonable
  estimate from real results is expected, not a literal database export.
- analytics.consistencyNote: 1 factual sentence on the athlete's consistency, e.g. "National
  Record holder; consistent sub-10.2 performer".
- analytics.peakZoneLabel: derived directly from consistencyData — "<range of the peak
  bucket> · <its percentage>% success rate", e.g. "10.2–10.3s · 36% success rate".
- analytics.coachJoinYear, beforeCoach, afterCoach, coachImprovement, coachImpactData: only
  fill if you find a clear, specific before/after comparison tied to a documented coaching
  change — beforeCoach/afterCoach are performance MARKS (not coach names) from just before
  and after that year, coachImpactData is the full year-by-year list of {{year, value,
  period}} with period "before" or "after" relative to coachJoinYear, coachImprovement is
  the signed delta ("+0.13m (+7.2%)") or the literal string "Data not available" if you have
  the before/after marks but can't compute a meaningful percentage. Leave all of these
  null/[] together if no documented coaching change is findable — that's the expected/
  correct answer for most athletes, not a miss.
- analytics.heatmapData: ALWAYS exactly 3 entries with tournament "Olympics", "World Champ",
  "National Champ" in that order (same National/Olympic/World tiers as
  record_highlight.benchmarks, kept here too for export compatibility — you can reuse the
  same facts). For each, fill `years` with whatever you found for that tier, e.g. {{"value":
  "9.58s", "holder": "Usain Bolt", "date": "Aug 16, 2009", "venue": "Berlin"}} — leave
  `years` as {{}} only after a genuine search attempt for that tier comes up empty.
"""
    if sport == Sport.CRICKET:
        return f"""
- performance.format MUST be exactly "{cricket_format}" — this athlete plays multiple
  formats (Test/ODI/T20), each with completely different career statistics. EVERY figure you
  report below (battingStats, bowlingStats, tournament, and record_highlight if applicable)
  MUST come from their {cricket_format} career specifically — do not mix in stats, records, or
  tournament names from a different format, even if they're more impressive or more recent.
  If you cannot find reliable {cricket_format}-specific stats, leave the relevant fields null
  rather than substituting another format's numbers.
- performance.battingStats and performance.bowlingStats: fill BOTH if the athlete has any
  batting and bowling record in {cricket_format}, even a minor one — this data source doesn't
  split players into "batsman only" / "bowler only" roles, most players carry some figures in
  both. Only null out a whole stats object if the athlete genuinely never bats or never bowls
  in this format. battingStats: runs, ballsFaced, battingAverage, strikeRate, fours, sixes,
  dismissals (all plain numbers). bowlingStats: oversBowled (decimal, e.g. 38.3), ballsBowled,
  runsConceded, wickets, economy, bowlingAverage (all plain numbers).
- performance.tournament: the specific {cricket_format} tournament/series being described,
  e.g. "womens_t20i", "womens_ipl", or a similarly specific identifier — not just "cricket".

record_highlight.benchmarks (the National/Olympic/World record-comparison strip — ALWAYS
return exactly 3 entries with label "National", "Olympic", "World" in that order, even when
record_highlight itself has nothing for this athlete. Cricket has no "Olympic" record in the
athletics sense, so map the 3 tiers to the closest cricket equivalents and say so via
`tournament`: "National" = the Indian national {cricket_format} record for this stat category
(e.g. most runs/wickets in {cricket_format}), "Olympic" = the equivalent ICC-tournament/
major-global-event record most comparable in prestige (e.g. World Cup record for this stat),
"World" = the outright {cricket_format} world record for this stat category. Fill
value/holder/date/venue/tournament for each — these are stable, well-documented facts for
cricket's headline records (most runs/wickets); avoid leaving them null.

analytics fields (derive from facts you actually find via search — avoid leaving these null
when the underlying stat is realistically findable; only use {{}} after a genuine search
comes up empty):
- analytics.seasonalData: {{"labels": [...years/seasons, most recent last...], "runs": [...],
  "wickets": [...]}} — season-by-season {cricket_format} figures if you can find them via a
  stats site or season recap articles. {{}} only if truly just career totals are findable.
- analytics.radarData: {{"axes": ["Batting Average", "Strike Rate", "Economy", "Wickets"],
  "values": [...four 0-100 scores...]}}. These ARE your editorial estimates (like aiInsight),
  not hard facts — score based on the actual battingStats/bowlingStats you found relative to
  what's typical for a {cricket_format} player (e.g. a strike rate well above the format's
  norm scores high). Do not default to a flat score for every axis. {{}} only if
  battingStats and bowlingStats are both largely null.
- analytics.consistencyData: {{"note": <1 short factual sentence on consistency, if you
  found something specific, e.g. a notable string of 50+ scores>}} — {{}} if nothing specific
  found; do not pad this with generic filler.
- analytics.coachImpactData and analytics.heatmapData: leave {{}} — a documented coaching
  before/after or spatial dismissal/scoring-zone data is rarely findable for cricket via
  general search; {{}} is the expected/correct answer here, not a miss.
"""
    # football
    return """
- performance.attackingStats: goals, assists, shots, shotsOnTarget, shotConversionPct (as a
  percentage number, e.g. 15.5 not "15.5%"), xg, xa (expected goals/assists, decimals).
- performance.playmakingStats: chancesCreated, bigChancesCreated, keyPasses,
  dribblesCompleted — all plain integers.
- performance.appearanceStats: matchesPlayed, minutesPlayed for the season/tournament being
  described.
- performance.position: exactly one of "GK" | "DF" | "MF" | "FW".
- performance.team, performance.season, performance.tournament, performance.format: the
  specific team/season/competition/format (e.g. "international", "club") the stats above
  refer to — be specific, not generic.

record_highlight.benchmarks (the National/Olympic/World record-comparison strip — ALWAYS
return exactly 3 entries with label "National", "Olympic", "World" in that order, even when
record_highlight itself has nothing for this athlete. Map to football's closest equivalents
via `tournament`: "National" = the Indian national-team record for this stat category (e.g.
most international goals), "Olympic" = the Olympic football tournament record for this stat
category, "World" = the outright global record (e.g. most international goals ever, any
nation). Fill value/holder/date/venue/tournament for each — these are well-documented facts;
avoid leaving them null.

analytics fields (derive from facts you actually find via search — avoid leaving these null
when the underlying stat is realistically findable; only use {} after a genuine search comes
up empty):
- analytics.seasonalData: {"labels": [...seasons, most recent last...], "goals": [...],
  "assists": [...]} — season-by-season figures if findable via a stats site. {} only if truly
  just the single season/tournament you're already describing is findable.
- analytics.radarData: {"axes": ["Finishing", "Playmaking", "Dribbling", "Availability"],
  "values": [...four 0-100 scores...]}. These ARE your editorial estimates (like aiInsight),
  not hard facts — score based on the actual attackingStats/playmakingStats/appearanceStats
  you found relative to what's typical for the player's position (e.g. high shotConversionPct
  scores well on "Finishing"; high minutesPlayed relative to squad availability scores well
  on "Availability"). Do not default to a flat score for every axis. {} only if the
  underlying stats are largely null.
- analytics.consistencyData: {"note": <1 short factual sentence, e.g. a notable scoring
  streak, if found>} — {} if nothing specific found.
- analytics.coachImpactData: only fill if you find a clear, specific before/after comparison
  tied to a documented manager change — {} is the expected/correct answer for most players.
- analytics.heatmapData: {"zone": <e.g. "left channel", "central", "box">} only if a
  reliable source specifically describes the player's typical pitch position/zone — {}
  otherwise, not a miss.
"""


def _build_prompt(
    sport: Sport, athlete_name: str, focus_fields: list[str] | None = None, cricket_format: str | None = None
) -> str:
    sport_id = _sport_id_for(sport)

    base = f"""
You are drafting a structured athlete document for SportsFan360, a sports fan engagement
platform. You have access to Google Search — use it to look up current, accurate facts about
this athlete rather than relying only on what you already know. Search for their official
bio, current stats, and recent results before answering.

Athlete name: {athlete_name}
Sport category: {sport_id}{f' ({_CATEGORY_LABELS[sport]})' if sport in _TRACK_FIELD_SPORTS else ''}{f' — {cricket_format} career specifically' if cricket_format else ''}

Produce a single JSON object with exactly these top-level keys: coreInfo, performance,
record_highlight, analytics.

NULL-AVOIDANCE: use null (JSON null) only after you have genuinely searched for a field and
could not find or confirm it. Do not guess or invent numbers — but also do not default to
null out of caution for facts that are realistically findable (a personal best, a coach's
name, a well-known world/Olympic record). Accuracy matters more than completeness, and a
human editor will review this draft, but an all-null draft is not more "safe" than a
well-researched one — it just means the search wasn't tried hard enough.

coreInfo fields: name, country, flag, gender, dob, heightCm, weightKg, bio, birthplace,
coachName, yearsActiveSince.
- gender MUST be exactly "Men" or "Women" — used for filtering, not editorial content.
- flag is an emoji flag, e.g. "🇮🇳".
- dob MUST be a full ISO date "YYYY-MM-DD" if known, otherwise null — never a bare year.
- bio: 2-4 factual sentences, no editorializing.
- coachName MUST be just the coach's plain name (e.g. "Rana Reider"), never a name with a
  parenthetical annotation like "(former coach)" attached — if there's noteworthy context
  about a coaching change, mention it in bio instead and put only the CURRENT coach's name
  (or null if unknown/none) here.

performance fields (this sport's specific shape):
{_sport_specific_field_notes(sport, cricket_format=cricket_format)}

record_highlight: for the match-center RECORD CARD, separate from the profile above. Search
specifically for whether this athlete currently holds (or recently set) a notable,
still-relevant record{f' in {cricket_format} specifically — do not report a record from a different cricket format' if cricket_format else ''}. If they do, fill the object: {{event, result, type, typeFull, phase,
date, city, prevRecord, prevRecordPlace, prevHolder, improvement, improvementDate, aiInsight,
progressData, benchmarks}}.
    - type MUST be exactly one of: "GR", "NR", "WR", "CR", "PB"
    - result / prevRecord / improvement MUST be plain strings in the record's native unit
      (e.g. "88.13m", "9.85s", "5/17", "+2.53m", "-0.08s")
    - prevRecordPlace is the city/venue where the OLD record was set — distinct from `city`,
      which is where THIS record was set
    - prevHolder is the athlete/team who held the record before this one
    - improvementDate is the date the record was actually broken (usually the same as `date`)
    - progressData: list of {{"year": "2022", "value": 88.13}} objects (value as a plain
      number, no unit suffix), up to 6 most recent years, [] if not applicable
    - aiInsight: 2-3 factual sentences on what made the performance notable, no unfounded
      editorializing
    - benchmarks: see the National/Olympic/World instructions above under "performance
      fields" — always exactly 3 entries, regardless of whether the rest of
      record_highlight is filled.
    - If this athlete does NOT currently hold or hasn't recently set a notable record, set
      every field EXCEPT benchmarks to null (event, result, type, etc. all null) — but still
      populate benchmarks, since that's about the event's records, not the athlete's own. Do
      not omit record_highlight entirely; return the object with benchmarks filled and the
      rest null instead.

If a list field has no data, use an empty list [], never null.

Respond with ONLY a single JSON object containing coreInfo, performance, record_highlight,
and analytics. No prose, no markdown code fences, no commentary before or after the JSON.
"""

    if not focus_fields:
        return base

    focus_list = ", ".join(focus_fields)
    return base + f"""

IMPORTANT — TARGETED RETRY:
A previous search pass could not find reliable data for these specific fields: {focus_list}

For THIS pass, prioritize finding data for exactly these fields, trying different search
angles than a generic profile search would use — e.g. the athlete's federation/league stats
page directly, recent season recap articles, "<athlete name> coach" / "<athlete name>
record", or for record_highlight.benchmarks specifically, "<event> world record" / "<event>
Olympic record" / "<event> [country] national record". Only return null for a field in this
list if, after genuinely attempting these searches, no reliable source has the information.

Still return the FULL set of keys (coreInfo, performance, record_highlight), reusing whatever
solid data you already have for the rest.
"""


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
                hint = f" (finish_reason={finish_reason} — likely truncated, response may have exceeded max_output_tokens)"
        except Exception:
            pass
        raise GenerationError(
            f"Gemini response was not valid JSON: {e}{hint} [response length: {len(raw)} chars]"
        ) from e


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
        if isinstance(value, dict):
            nums = [v for v in value.values() if isinstance(v, (int, float)) and not isinstance(v, bool)]
            return int(min(nums)) if nums else None
        return value
    if target_type is float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            match = re.search(r"-?\d+\.?\d*", value)
            return float(match.group()) if match else None
        return value
    return value


def _coerce_to_model(data, model_cls, na_fields=None, path=""):
    if not isinstance(data, dict):
        return data
    na_fields = na_fields or set()

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
                    if isinstance(item, str):
                        wrap_field = next(
                            (
                                fn
                                for fn, f2 in item_type.model_fields.items()
                                if f2.is_required() and _unwrap_optional(f2.annotation)[0] is str
                            ),
                            None,
                        )
                        item = {wrap_field: item} if wrap_field else None
                    if isinstance(item, dict):
                        coerced_items.append(
                            _coerce_to_model(item, item_type, na_fields=na_fields, path=field_path)
                        )
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
                _coerce_to_model(value, annotation, na_fields=na_fields, path=field_path)
                if isinstance(value, dict)
                else value
            )
            continue

        if annotation in (str, int, float):
            coerced = _generic_coerce_scalar(value, annotation)
            if coerced is None and annotation is str:
                if field_path in na_fields:
                    coerced = "N/A"
                elif field.is_required():
                    coerced = "N/A"
            result[name] = coerced
            continue

        if annotation is date_type:
            if isinstance(value, str):
                try:
                    date_type.fromisoformat(value)
                    result[name] = value
                except ValueError:
                    result[name] = None
            else:
                result[name] = value
            continue

        result[name] = value

    return result


def _normalize_record_type(record_highlight: Optional[dict]) -> Optional[dict]:
    if not isinstance(record_highlight, dict):
        return record_highlight
    value = record_highlight.get("type")
    if not isinstance(value, str):
        return record_highlight
    normalized = value.strip().upper()
    if normalized in _VALID_RECORD_TYPES:
        record_highlight["type"] = normalized
        return record_highlight
    name_map = {
        "GAMES RECORD": "GR",
        "NATIONAL RECORD": "NR",
        "WORLD RECORD": "WR",
        "CONTINENTAL RECORD": "CR",
        "PERSONAL BEST": "PB",
    }
    record_highlight["type"] = name_map.get(normalized, value)
    return record_highlight


def _normalize_benchmarks(record_highlight: Optional[dict]) -> Optional[dict]:
    """Ensures record_highlight.benchmarks always has exactly the 3
    National/Olympic/World entries in order, regardless of what Gemini
    returned (missing entries, extra entries, wrong order, or the whole
    key omitted). Matches existing entries by label case-insensitively and
    fills any missing label with an empty (label-only) BenchmarkRecord
    rather than dropping it."""
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


def _normalize_heatmap_tiers(analytics: Optional[dict]) -> Optional[dict]:
    """Same idea as _normalize_benchmarks, for analytics.heatmapData —
    always exactly the 3 _HEATMAP_TOURNAMENT_TIERS entries in order,
    regardless of what Gemini returned."""
    if not isinstance(analytics, dict):
        return analytics
    raw = analytics.get("heatmapData")
    by_tournament = {}
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict) and isinstance(entry.get("tournament"), str):
                by_tournament[entry["tournament"].strip().lower()] = entry
    analytics["heatmapData"] = [
        {**by_tournament.get(t.lower(), {}), "tournament": t} for t in _HEATMAP_TOURNAMENT_TIERS
    ]
    return analytics


def _sanitize(data: dict) -> dict:
    core_info = data.get("coreInfo")
    if isinstance(core_info, dict):
        coach = core_info.get("coachName")
        if isinstance(coach, str):
            core_info["coachName"] = re.sub(r"\s*\([^)]*\)", "", coach).strip() or None

    record_highlight = data.get("record_highlight")
    record_highlight = _normalize_record_type(record_highlight)
    record_highlight = _normalize_benchmarks(record_highlight)
    data["record_highlight"] = record_highlight

    if isinstance(data.get("analytics"), dict):
        data["analytics"] = _normalize_heatmap_tiers(data["analytics"])

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
    seen = set()
    deduped = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            deduped.append(u)
    return deduped


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


def _generate_single_pass(
    athlete_name: str, sport: Sport, focus_fields: list[str] | None = None, cricket_format: str | None = None
):
    prompt = _build_prompt(sport, athlete_name, focus_fields=focus_fields, cricket_format=cricket_format)
    response = _call_gemini(prompt)
    try:
        data = _extract_json(response.text, response_meta=response)
    except GenerationError:
        response = _call_gemini(prompt)
        data = _extract_json(response.text, response_meta=response)
    return data, response


def generate_athlete_profile(athlete_name: str, sport: Sport, max_passes: int = 3, cricket_format: str | None = None):
    """
    cricket_format ("Test" | "ODI" | "T20") pins which format's career
    stats/record to draft — cricketers have wildly different career
    numbers per format, and without pinning one, search-grounded
    generation picks a different format on different calls for the same
    athlete. Required for Sport.CRICKET, ignored otherwise.
    """
    schema_cls = get_schema_for_sport(sport)

    data, response = _generate_single_pass(athlete_name, sport, cricket_format=cricket_format)
    data = _sanitize(data)
    data = _coerce_to_model(data, schema_cls, na_fields=set())

    data["athlete_id"] = _slugify(athlete_name)
    data["sportId"] = _sport_id_for(sport)
    if sport in _TRACK_FIELD_SPORTS:
        data.setdefault("performance", {})["category"] = _CATEGORY_LABELS[sport]
        # These 4 are fixed by category, not researched facts, so the
        # pipeline sets them directly (same reasoning as `category`
        # itself) rather than trusting Gemini to pick consistently.
        analytics = data.setdefault("analytics", {})
        for k, v in _ANALYTICS_DEFAULTS_BY_CATEGORY[sport].items():
            analytics[k] = v  # forced, same as performance.category — fixed by category, not a research field
        analytics["heatmapData"] = _normalize_heatmap_tiers({"heatmapData": analytics.get("heatmapData")})["heatmapData"]
        data["analytics"] = analytics
    if sport == Sport.CRICKET and cricket_format:
        data.setdefault("performance", {})["format"] = cricket_format
    if isinstance(data.get("coreInfo"), dict):
        data["coreInfo"]["name"] = athlete_name
    if not isinstance(data.get("analytics"), dict):
        data["analytics"] = {}  # Gemini omitted it entirely -- Analytics' defaults fill the 5 empty containers
    if not isinstance(data.get("record_highlight"), dict):
        # Gemini omitted record_highlight entirely (or returned null) --
        # still stand up the object so benchmarks (event-level, not
        # athlete-specific) get populated via retry rather than staying
        # permanently absent.
        data["record_highlight"] = {}
    data["record_highlight"] = _normalize_benchmarks(data["record_highlight"])
    data["source"] = SourceRef(
        source_name="Gemini (search-grounded — unverified, needs human review)",
        source_url="https://sportsfan360.internal/llm-draft",  # placeholder, not a real citation
        fetched_at=datetime.now(timezone.utc).isoformat(),
    ).model_dump(mode="json")

    grounding_sources = _extract_grounding_sources(response)
    retry_log = []

    for pass_num in range(2, max_passes + 1):
        missing = _missing_retryable_paths(data, sport)
        if not missing:
            break

        retry_data, retry_response = _generate_single_pass(
            athlete_name, sport, focus_fields=missing, cricket_format=cricket_format
        )
        retry_data = _sanitize(retry_data)
        retry_data = _coerce_to_model(retry_data, schema_cls, na_fields=set())

        filled_this_pass = []
        for p in missing:
            if p in ("record_highlight.benchmarks", "analytics.heatmapData"):
                key_names = ("value", "holder") if p.endswith("benchmarks") else ("years",)
                candidate = _get_path(retry_data, p)
                if isinstance(candidate, list) and _tiered_list_has_data(candidate, key_names):
                    _set_path(data, p, candidate)
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

    data["grounding_sources"] = list(dict.fromkeys(grounding_sources))

    profile = schema_cls.model_validate(data)

    profile_dict = profile.model_dump(mode="json")
    profile_dict["_retry_log"] = retry_log
    return profile, profile_dict


def save_draft_to_dynamodb(profile_dict: dict, sport: Sport, athlete_name: str) -> str | None:
    """
    Writes this LLM-generated profile into the DynamoDB review queue
    (SportsData, entityId="REVIEW#<draft_id>") via
    firebase_store.save_review_draft(), same pending_review pattern
    athlete_pipeline.py already uses.

    proposed_data is the full profile dict minus the internal _retry_log,
    which is debug-only and shouldn't land on the live athlete record if
    this draft is later approved.

    Returns the new draft_id, or None if the write failed (e.g. AWS
    creds/network not available in this environment) -- callers should
    treat that as non-fatal since the local JSON save already succeeded.
    """
    proposed_data = {k: v for k, v in profile_dict.items() if k != "_retry_log"}

    draft = {
        "athlete_id": profile_dict["athlete_id"],
        "athlete_name": athlete_name,
        "sport": _sport_id_for(sport),
        "trigger_reason": "llm_content_generation",
        "proposed_data": proposed_data,
        "status": "pending_review",
    }

    try:
        draft_id = firebase_store.save_review_draft(draft)
    except Exception as e:
        print(f"⚠️ Failed to save draft to DynamoDB: {e}", file=sys.stderr)
        return None

    return draft_id


def _prompt_sport() -> Sport:
    options = list(Sport)
    print("\nSport:")
    for i, s in enumerate(options, 1):
        label = _CATEGORY_LABELS.get(s, s.value.title())
        print(f"  {i}. {label}" + (" (athletics)" if s in _TRACK_FIELD_SPORTS else ""))
    while True:
        choice = input("Choose a number: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            return options[int(choice) - 1]
        print("Invalid choice, try again.")


def _prompt_cricket_format() -> str:
    options = ["Test", "ODI", "T20"]
    print("\nCricket format (career stats differ a lot by format — pick one):")
    for i, f in enumerate(options, 1):
        print(f"  {i}. {f}")
    while True:
        choice = input("Choose a number: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            return options[int(choice) - 1]
        print("Invalid choice, try again.")


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    while True:
        athlete_name = input("\nAthlete name (blank to quit): ").strip()
        if not athlete_name:
            print("Done.")
            break

        sport = _prompt_sport()
        cricket_format = _prompt_cricket_format() if sport == Sport.CRICKET else None

        print(f"\nGenerating draft for {athlete_name} ({_sport_id_for(sport)}"
              f"{f', {cricket_format}' if cricket_format else ''})...")
        try:
            profile, profile_dict = generate_athlete_profile(athlete_name, sport, cricket_format=cricket_format)
        except (GenerationError, ValidationError) as e:
            print(f"FAILED: {e}", file=sys.stderr)
            continue

        out_path = os.path.join(OUTPUT_DIR, f"{profile.athlete_id}.json")
        with open(out_path, "w") as f:
            json.dump(profile_dict, f, indent=2, default=str)

        draft_id = save_draft_to_dynamodb(profile_dict, sport, athlete_name)
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