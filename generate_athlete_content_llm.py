
# """
# generate_athlete_content_llm.py

# Standalone content generator: prompts Gemini (with Google Search grounding
# enabled) to draft an athlete document matching sportsfan360-schema-v4.md —
# `coreInfo` (identity/bio), `performance` (sport-specific stats), and
# `record_highlight` (the athlete's most notable record, for the match-center
# record card + the National/Olympic/World benchmark-comparison strip).
# Scoped to the priority sports: cricket, football, boxing, and track & field
# (one `sportId`, "athletics", covering 6 sub-disciplines: sprints,
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
#   4. prints the validated JSON, saves it to
#      ./llm_athlete_drafts/<athlete_id>.json, AND writes it into the
#      DynamoDB review queue (SportsData, status=pending_review) via
#      firebase_store.save_review_draft() — same review-queue pattern
#      athlete_pipeline.py already uses.

# NOTE: search-grounded LLM output can still be wrong or out of date — this is
# a content-drafting aid for the review queue, not a source-of-truth stats
# feed. Every draft still needs human review before publishing.
# """

# import base64
# import json
# import os
# import re
# import sys
# import typing
# import urllib.parse
# from datetime import date as date_type, datetime, timezone
# from enum import Enum
# from typing import Optional
# import requests

# from google import genai
# from google.genai import types
# from pydantic import BaseModel, Field, HttpUrl, ValidationError

# import firebase_store


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
#     BOXING = "boxing"


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
#     return sport.value  # "cricket" | "football" | "boxing"


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
#     welcomeVideoUrl: Optional[str] = None
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
#     events: Optional[int] = Field(None, description="total competitions entered that year")
#     gold: int = 0
#     silver: int = 0
#     bronze: int = 0
#     seasonBest: Optional[float] = Field(None, description="best mark that year, in the event's native unit, no suffix")
#     seasonAverage: Optional[float] = Field(None, description="average mark that year, in the event's native unit, no suffix")
#     currentStreak: Optional[int] = Field(None, description="active podium/win streak as of that year, if tracked")

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


# class BoxingRecordStats(BaseModel):
#     wins: Optional[int] = None
#     losses: Optional[int] = None
#     draws: Optional[int] = None
#     knockouts: Optional[int] = Field(None, description="wins by KO/TKO specifically, a subset of `wins`")
#     noContests: Optional[int] = None

#     model_config = {"extra": "forbid"}


# class BoxingChartConfig(BaseModel):
#     primaryUnit: str = "wins"
#     secondaryUnit: str = "knockouts"

#     model_config = {"extra": "forbid"}


# class BoxingMedalDataPoint(BaseModel):
#     year: str
#     events: Optional[int] = Field(None, description="total bouts/tournaments entered that year, if determinable")
#     gold: int = 0
#     silver: int = 0
#     bronze: int = 0
#     tournament: Optional[str] = Field(None, description="the tournament/event name this year's medal(s) came from, e.g. 'IBA Women's World Boxing Championships' — if multiple medals in different tournaments that year, name the most significant one")
#     seasonBest: Optional[str] = Field(None, description="the single best result value that year, e.g. 'Gold' — just the medal color/outcome, NOT the tournament name")
#     seasonAverage: Optional[str] = Field(None, description="the overall medal-color value for that year, e.g. 'Gold', 'Silver', 'Bronze' — if multiple medals that year, the most common or highest-value color")
#     currentStreak: Optional[int] = Field(None, description="active win streak as of that year, if determinable; null if not")

#     model_config = {"extra": "forbid"}


# class BoxingAnalytics(BaseModel):
#     """Boxing analytics — same generic dict-container shape as
#     cricket/football's Analytics for seasonalData/radarData/
#     coachImpactData/consistencyData/heatmapData (no reference export
#     exists for boxing yet), but with a typed medalData field (unlike
#     cricket/football) since boxing's medals are well-documented,
#     tournament-by-tournament facts worth structuring properly rather than
#     leaving as a free-form dict."""
#     seasonalData: dict = Field(default_factory=dict)
#     radarData: dict = Field(default_factory=dict)
#     coachImpactData: dict = Field(default_factory=dict)
#     consistencyData: dict = Field(default_factory=dict)
#     heatmapData: dict = Field(default_factory=dict)
#     medalData: list[BoxingMedalDataPoint] = Field(
#         default_factory=list,
#         description="medal counts PER YEAR (not career totals — those stay in performance.recordStats/titles), "
#                      "one entry per year with any international medal",
#     )

#     model_config = {"extra": "forbid"}


# class BoxingPerformance(BaseModel):
#     weightClass: Optional[str] = Field(None, description="e.g. 'Lightweight', 'Welterweight', '75kg'")
#     stance: Optional[str] = Field(None, description="'Orthodox' | 'Southpaw' | 'Switch'")
#     reachCm: Optional[int] = None
#     heightAdvantageNote: Optional[str] = Field(
#         None, description="only fill if a specific reach/height comparison vs a notable opponent is documented; otherwise null"
#     )
#     tournament: Optional[str] = Field(None, description="e.g. 'professional', 'amateur', 'olympic_boxing', a specific title/promotion name")
#     titles: list[str] = Field(
#         default_factory=list, description="list of titles held or won, e.g. ['WBC World Lightweight Champion'] — [] if none"
#     )
#     recordStats: Optional[BoxingRecordStats] = None
#     chartConfig: BoxingChartConfig = Field(default_factory=BoxingChartConfig)

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


# class BoxingAthleteDocument(AthleteDocumentBase):
#     performance: BoxingPerformance
#     analytics: BoxingAnalytics = Field(default_factory=BoxingAnalytics)


# def get_schema_for_sport(sport: Sport) -> type[AthleteDocumentBase]:
#     if sport in _TRACK_FIELD_SPORTS:
#         return AthleticsAthleteDocument
#     if sport == Sport.CRICKET:
#         return CricketAthleteDocument
#     if sport == Sport.BOXING:
#         return BoxingAthleteDocument
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
#             "coreInfo.heightCm",
#             "coreInfo.weightKg",
#             "performance.medalCabinet",
#             "performance.stats.personalBest",
#             "performance.stats.seasonBest",
#             "record_highlight",
#             "record_highlight.benchmarks",
#             "record_highlight.progressData",
#             "analytics.seasonalData",
#             "analytics.radarData",
#             "analytics.medalData",
#             "analytics.stats",
#             "analytics.consistencyData",
#             "analytics.heatmapData",
#         ]
#         for s in _TRACK_FIELD_SPORTS
#     },
#     Sport.BOXING: [
#         "coreInfo.coachName",
#         "coreInfo.heightCm",
#         "coreInfo.weightKg",
#         "performance.weightClass",
#         "performance.stance",
#         "performance.reachCm",
#         "performance.tournament",
#         "performance.titles",
#         "performance.recordStats",
#         "record_highlight",
#         "record_highlight.benchmarks",
#         "analytics.seasonalData",
#         "analytics.radarData",
#         "analytics.medalData",
#     ],
#     Sport.CRICKET: [
#         "coreInfo.coachName",
#         "coreInfo.heightCm",
#         "coreInfo.weightKg",
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
#         "coreInfo.heightCm",
#         "coreInfo.weightKg",
#         "performance.team",
#         "performance.attackingStats",
#         "performance.playmakingStats",
#         "record_highlight",
#         "record_highlight.benchmarks",
#         "analytics.seasonalData",
#         "analytics.radarData",
#     ],
# }


# _MEDAL_DATA_OPTIONAL_FIELDS = ("events", "seasonBest", "seasonAverage", "currentStreak")
# # Boxing's medalData only has `events` as an optional sub-field beyond
# # gold/silver/bronze — seasonBest/averageThrow/currentStreak don't apply
# # to boxing (see BoxingMedalDataPoint), so using the full athletics tuple
# # here would make boxing's medalData retry forever (those keys never
# # exist on a boxing entry, so _is_empty() on a missing key is always
# # True). Use a separate, narrower tuple for boxing.
# _BOXING_MEDAL_DATA_OPTIONAL_FIELDS = ("events", "tournament", "seasonBest", "seasonAverage", "currentStreak")


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


# def _medal_data_has_missing_subfields(entries: list, optional_fields: tuple = _MEDAL_DATA_OPTIONAL_FIELDS) -> bool:
#     """True if analytics.medalData has at least one entry, and at least
#     one of those entries is missing one of `optional_fields`. gold/silver/
#     bronze are excluded — those default to 0 and are basically never left
#     null. `optional_fields` defaults to the full athletics set but callers
#     should pass the sport-appropriate tuple (e.g.
#     _BOXING_MEDAL_DATA_OPTIONAL_FIELDS for boxing)."""
#     if not entries:
#         return False
#     for e in entries:
#         if not isinstance(e, dict):
#             continue
#         for k in optional_fields:
#             if _is_empty(e.get(k)):
#                 return True
#     return False


# _RECORD_HIGHLIGHT_CORE_FIELDS = ("event", "result", "type", "date")


# def _record_highlight_core_is_empty(record_highlight) -> bool:
#     """True if record_highlight's actual record fields (event/result/
#     type/date) are all still null, even though the dict itself may be
#     non-empty because benchmarks got filled. This is the case
#     _is_empty()'s whole-dict check misses."""
#     if not isinstance(record_highlight, dict):
#         return True
#     return all(_is_empty(record_highlight.get(f)) for f in _RECORD_HIGHLIGHT_CORE_FIELDS)


# def _missing_retryable_paths(data: dict, sport: "Sport") -> list[str]:
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
#         if p == "analytics.medalData":
#             entries = _get_path(data, p) or []
#             optional_fields = (
#                 _BOXING_MEDAL_DATA_OPTIONAL_FIELDS if sport == Sport.BOXING else _MEDAL_DATA_OPTIONAL_FIELDS
#             )
#             if _is_empty(entries) or _medal_data_has_missing_subfields(entries, optional_fields):
#                 missing.append(p)
#             continue
#         # record_highlight — flag as missing if the core record fields
#         # are null, even though the dict overall isn't empty (benchmarks
#         # alone would otherwise mask this).
#         if p == "record_highlight":
#             rh = _get_path(data, p)
#             if _record_highlight_core_is_empty(rh):
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
# - analytics.medalData: list of {{year, events, gold, silver, bronze, seasonBest, seasonAverage,
#   currentStreak}} objects, one entry per year with any international medal — NOT career
#   totals (those go in stats). events = total competitions entered that year. gold/silver/
#   bronze = medal counts for that year. seasonBest = the athlete's best mark that year, a
#   plain number in the event's native unit (matches seasonalData's value for that year where
#   possible). seasonAverage = average mark across that year's competitions, same unit, no
#   suffix — a small handful of individual results from that year is enough to ESTIMATE this
#   from (you do NOT need every competition result to compute an exact average); a reasonable
#   estimate based on seasonBest plus whatever other results you find for that year is expected
#   and preferred over leaving this null, the same way consistencyData below is an estimate, not
#   a literal database export. currentStreak = the athlete's active podium or win streak as of
#   that year, if you can determine one from the results found; null if not determinable. Only
#   leave seasonAverage/currentStreak null for a year if you genuinely found only a single
#   result (seasonBest) for that year and have no other basis to estimate from — don't leave
#   gold/silver/bronze null (use 0).
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

#     if sport == Sport.BOXING:
#         return """
# - performance.weightClass: the athlete's current/most recent weight class, e.g. "Lightweight",
#   "Welterweight", or the kg figure used in amateur/Olympic boxing (e.g. "75kg").
# - performance.stance: exactly one of "Orthodox" | "Southpaw" | "Switch" if documented,
#   otherwise null.
# - performance.reachCm: plain integer, only if a reach measurement is publicly documented.
# - performance.tournament: "professional", "amateur", "olympic_boxing", or a specific
#   promotion/title name if that's what the record refers to — be specific.
# - performance.titles: list of title strings, e.g. ["WBC World Lightweight Champion", "2023
#   National Championships Gold"] — search specifically for championship belts, national titles,
#   and Olympic/Commonwealth Games medals. [] only if the athlete holds no titles.
# - performance.recordStats: {wins, losses, draws, knockouts, noContests} — knockouts is wins
#   BY knockout specifically (a subset of wins, not additional to it). These are usually
#   well-documented, stable career totals for any professional or notable amateur boxer —
#   search confidently and fill these; only null out a specific figure (e.g. noContests) if
#   genuinely undocumented, which is common and fine for that one field.

# record_highlight.benchmarks (the National/Olympic/World record-comparison strip — ALWAYS
# return exactly 3 entries with label "National", "Olympic", "World" in that order, even when
# record_highlight itself has nothing for this athlete. Boxing has no single "record" the way
# a timed sport does, so map the 3 tiers to the closest boxing equivalents via `tournament`:
# "National" = a notable Indian national boxing achievement/record for this weight class (e.g.
# most national titles), "Olympic" = the athlete's own Olympic boxing result/medal if
# applicable, or the weight class's most decorated Olympic boxer, "World" = the outright
# weight class's most notable world-title record (e.g. most consecutive world title defenses).
# Fill value/holder/date/venue/tournament for each where findable; null is acceptable more
# often here than in timed sports, since boxing doesn't have a single universally-tracked
# numeric record the way track events do.

# analytics fields (derive from facts you actually find via search — avoid leaving these null
# when the underlying stat is realistically findable; only use {} after a genuine search comes
# up empty):
# - analytics.seasonalData: {"labels": [...years, most recent last...], "wins": [...],
#   "losses": [...]} — year-by-year fight record if findable via a boxing stats site (e.g.
#   BoxRec) or career recap articles. {} only if truly just the career total is findable.
# # - analytics.medalData: list of {events, gold, silver, bronze} objects, one entry per year
# #   with any international medal — NOT career totals (those stay in performance.recordStats
# #   and performance.titles). events = total bouts/tournaments entered that year, if you can
# #   determine it; null is fine if not findable. gold/silver/bronze = medal counts for that
# #   specific year (derive from the titles/medals you found and their dates) — use 0, never
# #   null, for years with no medal of that color. [] only if no year-by-year medal breakdown
# #   is derivable at all from what you found (rare, given performance.titles is usually
# #   well-documented).
# - analytics.medalData: list of {year, events, gold, silver, bronze, tournament, seasonBest,
#   seasonAverage, currentStreak} objects, one entry per year with any international medal —
#   NOT career totals (those stay in performance.recordStats and performance.titles).
#   year MUST be a specific 4-digit year string (e.g. "2022") — every medal you found has a
#   known year attached to the event it was won at (searching "<tournament name> year" or
#   "<athlete> <tournament> results" will surface it); do NOT return "N/A" for year unless you
#   genuinely cannot determine which year a specific medal belongs to after trying this, which
#   should be rare. events = total bouts/tournaments entered that year. If you can find the athlete's full
#   year's schedule, use that exact count. If not, use 1 as a floor for each distinct
#   tournament reflected in that year's medal(s)/results (e.g. a year with one gold medal
#   tournament and nothing else found -> events=1) rather than leaving this null — a
#   minimum-known count is more useful than no count. Only leave events null if you cannot
#   even confirm which tournament(s) the athlete competed in that year, which should be rare
#   given tournament is already being filled.events = total bouts/tournaments entered that year, if you can determine
#   it; null is fine if not findable. gold/silver/bronze = medal counts for that specific year
#   (derive from the titles/medals you found and their dates) — use 0, never null, for years
#   with no medal of that color. tournament = the name of the tournament/event this year's
#   medal(s) came from, e.g. "IBA Women's World Boxing Championships" — if multiple medals
#   came from different tournaments that year, name the most significant one here. seasonBest
#   and seasonAverage MUST each be filled with a plain medal-color value, e.g. "Gold" or
#   "Bronze" — NOT a sentence, NOT the tournament name, NOT null. seasonBest is the single
#   best medal color won that year; seasonAverage is the overall/most-common medal color that
#   year (same value as seasonBest if only one medal was won that year — this is expected and
#   correct, not a mistake). Since gold/silver/bronze are already known from the counts above,
#   derive seasonBest/seasonAverage directly from them: e.g. gold=1,silver=0,bronze=0 ->
#   seasonBest="Gold", seasonAverage="Gold"; gold=0,silver=1,bronze=1 -> seasonBest="Silver",
#   seasonAverage="Silver" (the higher-value color). currentStreak = the athlete's active win
#   streak as of that year if determinable; null if not. Only leave seasonBest/seasonAverage
#   null for a year with zero medals (gold=silver=bronze=0) — every year with at least one
#   medal MUST have both filled. [] only if no year-by-year medal breakdown is derivable at
#   all from what you found (rare, given performance.titles is usually well-documented).
# - analytics.radarData: {"axes": ["Power", "Defense", "Footwork", "Ring IQ"], "values": [...four
#   0-100 scores...]}. These ARE your editorial estimates (like aiInsight), not hard facts —
#   score based on the actual recordStats/titles you found (e.g. a high knockout ratio scores
#   well on "Power"). Do not default to a flat score for every axis. {} only if recordStats is
#   largely null.
# - analytics.consistencyData: {"note": <1 short factual sentence, e.g. a notable win streak or
#   title-defense streak, if found>} — {} if nothing specific found.
# - analytics.coachImpactData: only fill if you find a clear, specific before/after comparison
#   tied to a documented trainer/coach change — {} is the expected/correct answer for most
#   boxers.
# - analytics.heatmapData: {} — no spatial/zone equivalent for boxing; always {}, not a miss.
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
#         # Empty/near-empty response -- surface WHY, since a blank
#         # "did not contain valid JSON: " gives no signal. Check
#         # finish_reason and safety ratings on the first candidate, since
#         # Gemini can return an empty text body without raising an
#         # exception (safety filtering, truncation before any output,
#         # or a search-grounding-only turn with no text emitted).
#         diag = ""
#         try:
#             candidates = getattr(response_meta, "candidates", None) or []
#             if candidates:
#                 c = candidates[0]
#                 finish_reason = getattr(c, "finish_reason", None)
#                 safety_ratings = getattr(c, "safety_ratings", None)
#                 diag = f" [finish_reason={finish_reason}"
#                 if safety_ratings:
#                     blocked = [r for r in safety_ratings if getattr(r, "blocked", False)]
#                     if blocked:
#                         diag += f", blocked_categories={[getattr(r, 'category', r) for r in blocked]}"
#                 diag += "]"
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

#     # Backfill required fields the LLM omitted entirely (key absent from
#     # `data`, not just null) -- the loop above only visits keys that
#     # exist in `data`, so a genuinely missing key (e.g. boxing medalData
#     # entries missing "year") never gets set and Pydantic fails with
#     # "Field required" at final validation. Only string-typed required
#     # fields get a safe "N/A" fallback here; other required types are
#     # left for Pydantic to report normally, since there is no safe
#     # generic default for them.
#     for name, field in model_cls.model_fields.items():
#         if name in result:
#             continue
#         if not field.is_required():
#             continue
#         annotation, _ = _unwrap_optional(field.annotation)
#         if annotation is str:
#             result[name] = "N/A"

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


# def _sanitize(data: dict, sport: "Sport" = None) -> dict:
#     core_info = data.get("coreInfo")
#     if isinstance(core_info, dict):
#         coach = core_info.get("coachName")
#         if isinstance(coach, str):
#             core_info["coachName"] = re.sub(r"\s*\([^)]*\)", "", coach).strip() or None
#         country = core_info.get("country")
#         if isinstance(country, str) and country.strip().lower() == "india":
#             core_info["flag"] = "IN"    

#     record_highlight = data.get("record_highlight")
#     record_highlight = _normalize_record_type(record_highlight)
#     record_highlight = _normalize_benchmarks(record_highlight)
#     data["record_highlight"] = record_highlight

#     # Only athletics' AthleticsAnalytics expects heatmapData as a 3-entry
#     # tier list; every other sport's Analytics model keeps it a plain
#     # dict. Normalizing it for non-athletics sports produces a shape that
#     # fails validation (this was the boxing crash).
#     if sport in _TRACK_FIELD_SPORTS and isinstance(data.get("analytics"), dict):
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
#     data = _sanitize(data, sport=sport)
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
#         sport_label = _CATEGORY_LABELS.get(sport, sport.value) if sport in _TRACK_FIELD_SPORTS else sport.value
#         data["coreInfo"]["welcomeVideoUrl"] = get_welcome_video_url(athlete_name, sport_label=sport_label)
#         data["coreInfo"]["profileImage"] = get_profile_image_url(athlete_name, sport=sport)
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
#         retry_data = _sanitize(retry_data, sport=sport)
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
#             if p == "record_highlight":
#                 candidate = _get_path(retry_data, p)
#                 if not _record_highlight_core_is_empty(candidate):
#                     # Merge field-by-field into the EXISTING record_highlight
#                     # rather than replacing the whole dict, so a benchmarks
#                     # block that was already good in a prior pass survives
#                     # even if this retry's benchmarks came back weaker.
#                     existing = data.get("record_highlight") or {}
#                     for field_name, field_value in candidate.items():
#                         if field_name == "benchmarks":
#                             continue  # handled by the dedicated benchmarks branch above
#                         if not _is_empty(field_value):
#                             existing[field_name] = field_value
#                     data["record_highlight"] = existing
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


# # ── World Athletics fallback for profileImage (track & field only) ──
# #
# # Resolves the athlete's WA profile URL (via a headless-Chrome search of
# # WA's own search form, or DDG/Bing as fallback -- see
# # _search_world_athletics_profile_url), then fetches their photo directly
# # from media.aws.iaaf.org/athletes/<code>.jpg using the numeric athlete
# # code embedded in that URL -- see _get_world_athletics_image for why this
# # beats reading the profile page's og:image meta tag.


# _WA_PROFILE_URL_RE = r'https://worldathletics\.org/athletes/[a-z-]+/[a-z0-9-]+'


# def _search_ddg_html(query: str) -> Optional[str]:
#     """
#     Search via DuckDuckGo's HTML endpoint (no API key). DDG wraps every
#     result link in a "//duckduckgo.com/l/?uddg=<url-encoded-target>&..."
#     redirect rather than a bare href, so the target URL has to be pulled
#     out of the uddg param and URL-decoded -- a search for the raw
#     "https://worldathletics.org/..." string never matches even when the
#     result is present. NOTE: DuckDuckGo's HTML endpoint is known to serve
#     a block/CAPTCHA page instead of real results for requests coming from
#     datacenter/cloud IPs (which most production backends look like to
#     them) -- if this consistently returns nothing even for athletes known
#     to have a WA profile, that's the likely cause, not a code bug. Logs
#     status code + response length on a miss so this is diagnosable from
#     the logs. Returns None on no match or any request error.
#     """
#     try:
#         resp = requests.get(
#             "https://html.duckduckgo.com/html/",
#             params={"q": query},
#             headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"},
#             timeout=10,
#         )
#         resp.raise_for_status()
#         for match in re.finditer(r'uddg=([^&"]+)', resp.text):
#             candidate = urllib.parse.unquote(match.group(1))
#             if re.match(f'^{_WA_PROFILE_URL_RE}', candidate, flags=re.IGNORECASE):
#                 return candidate
#         print(
#             f"DuckDuckGo search returned no WA profile match (status={resp.status_code}, "
#             f"response_len={len(resp.text)}) -- possibly blocked/CAPTCHA'd from this IP",
#             file=sys.stderr,
#         )
#         return None
#     except requests.exceptions.RequestException as e:
#         print(f"DuckDuckGo search request failed: {e}", file=sys.stderr)
#         return None


# def _decode_bing_redirect_target(encoded: str) -> Optional[str]:
#     """
#     Bing wraps result links as bing.com/ck/a?...&u=a1<base64url>&... rather
#     than a plain href -- the "a1" prefix marks base64 encoding, followed by
#     a URL-safe base64 string (which may be missing its padding, since Bing
#     strips trailing '='). Returns the decoded target URL, or None if it
#     doesn't look like a Bing-encoded URL at all.
#     """
#     if not encoded.startswith("a1"):
#         return None
#     b64 = encoded[2:]
#     b64 += "=" * (-len(b64) % 4)  # restore stripped padding
#     try:
#         return base64.urlsafe_b64decode(b64).decode("utf-8", errors="ignore")
#     except Exception:
#         return None


# def _search_bing_html(query: str) -> Optional[str]:
#     """
#     Fallback search via Bing's HTML results page (no API key). Modern Bing
#     wraps every result link in its own redirect
#     (bing.com/ck/a?...&u=a1<base64url-encoded-target>&...), same class of
#     problem as DuckDuckGo's uddg= wrapping -- a direct regex for the raw
#     "https://worldathletics.org/..." string matches nothing even when the
#     result is present, since the target only exists base64-encoded inside
#     u=. Extract and decode the u= param instead. Used as a second attempt
#     when DuckDuckGo returns nothing, since the two services don't always
#     block the same request patterns/IPs. Returns None on no match or any
#     request error.
#     """
#     try:
#         resp = requests.get(
#             "https://www.bing.com/search",
#             params={"q": query},
#             headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"},
#             timeout=10,
#         )
#         resp.raise_for_status()
#         # Some results are plain hrefs (not redirect-wrapped) -- check those first.
#         match = re.search(_WA_PROFILE_URL_RE, resp.text, flags=re.IGNORECASE)
#         if match:
#             return match.group(0)
#         # Others are wrapped in bing.com/ck/a?...&u=a1<base64>... -- decode each candidate.
#         for u_match in re.finditer(r'[?&]u=([^&"]+)', resp.text):
#             decoded = _decode_bing_redirect_target(urllib.parse.unquote(u_match.group(1)))
#             if decoded and re.match(f'^{_WA_PROFILE_URL_RE}', decoded, flags=re.IGNORECASE):
#                 return decoded
#         print(
#             f"Bing search also returned no WA profile match (status={resp.status_code}, "
#             f"response_len={len(resp.text)})",
#             file=sys.stderr,
#         )
#         return None
#     except requests.exceptions.RequestException as e:
#         print(f"Bing search request failed: {e}", file=sys.stderr)
#         return None


# def _search_wa_via_browser(athlete_name: str) -> Optional[str]:
#     """
#     Resolves an athlete name to their World Athletics profile URL by
#     driving WA's own "Search for an Athlete" form with a real headless
#     Chrome instance (via Playwright). worldathletics.org/athletes is a
#     client-rendered Next.js app with no server-rendered search results, so
#     a plain HTTP request can't see them at all -- and searching via
#     DuckDuckGo/Bing proved unreliable in practice (DDG served a
#     block/CAPTCHA page, Bing served a resultless shell page to non-browser
#     requests). Driving WA's own search UI as a real browser sidesteps
#     both problems and searches the authoritative source directly.

#     Requires the `playwright` package with Chromium installed
#     (`pip install playwright && playwright install chromium` --
#     `playwright install --with-deps chromium` if system deps are also
#     missing). Returns None (never raises) if playwright isn't installed,
#     no results are found, or anything else goes wrong -- this feeds a
#     nice-to-have enrichment step, not a required field.

#     Among WA's own returned results (already relevance-ranked by their
#     search), prefers the first one whose display name contains every word
#     of athlete_name; if no result matches that precisely, falls back to
#     WA's own top-ranked result rather than returning nothing.
#     """
#     try:
#         from playwright.sync_api import sync_playwright
#     except ImportError:
#         print(
#             "playwright not installed -- skipping World Athletics browser search "
#             "(pip install playwright && playwright install chromium)",
#             file=sys.stderr,
#         )
#         return None

#     query_tokens = set(athlete_name.lower().split())
#     try:
#         with sync_playwright() as p:
#             browser = p.chromium.launch(headless=True)
#             context = browser.new_context(
#                 user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
#                 ignore_https_errors=True,
#             )
#             page = context.new_page()
#             page.goto("https://worldathletics.org/athletes", timeout=30000)
#             page.wait_for_timeout(1500)
#             try:
#                 # Cookie-consent overlay blocks interaction with the search
#                 # box until dismissed -- not present on every load, so a
#                 # missed click here isn't fatal.
#                 page.click("text=Use necessary cookies only", timeout=4000)
#             except Exception:
#                 pass
#             search_input = page.query_selector("input[class*='AthleteSearch_searchInput']")
#             if not search_input:
#                 browser.close()
#                 return None
#             search_input.click()
#             page.keyboard.type(athlete_name, delay=50)
#             page.keyboard.press("Enter")
#             page.wait_for_timeout(2500)

#             candidates = []
#             for a in page.query_selector_all("a"):
#                 href = a.get_attribute("href") or ""
#                 if re.match(r'^/athletes/[a-z-]+/[a-z0-9-]+$', href, flags=re.IGNORECASE):
#                     candidates.append((href, (a.inner_text() or "").strip()))
#             browser.close()

#         if not candidates:
#             return None
#         for href, text in candidates:
#             if query_tokens.issubset(set(text.lower().split())):
#                 return f"https://worldathletics.org{href}"
#         return f"https://worldathletics.org{candidates[0][0]}"
#     except Exception as e:
#         print(f"World Athletics browser search failed for {athlete_name}: {e}", file=sys.stderr)
#         return None


# def _search_world_athletics_profile_url(athlete_name: str) -> Optional[str]:
#     """
#     Resolves an athlete name to their World Athletics profile URL. Tries,
#     in order: (1) a real headless-Chrome search of WA's own search form
#     (most reliable -- see _search_wa_via_browser), (2) DuckDuckGo's HTML
#     search, (3) Bing's HTML search, as fallbacks in case Playwright/
#     Chromium isn't installed in this environment. Never raises -- this
#     feeds a nice-to-have enrichment step, not a required field.
#     """
#     result = _search_wa_via_browser(athlete_name)
#     if result:
#         return result

#     query = f"site:worldathletics.org/athletes {athlete_name}"
#     result = _search_ddg_html(query)
#     if result:
#         return result
#     return _search_bing_html(query)


# def _get_world_athletics_image(athlete_name: str) -> Optional[str]:
#     """
#     Third-tier profileImage fallback (after both Wikimedia Commons
#     attempts): resolves the athlete's World Athletics profile URL, pulls
#     the numeric WA athlete code out of it (the trailing digits after the
#     slug, e.g. ".../gurindervir-singh-14792569" -> "14792569"), and
#     fetches the athlete's real photo directly from
#     media.aws.iaaf.org/athletes/<code>.jpg.

#     This direct endpoint is more complete than the profile page's
#     og:image meta tag -- some athletes have a real photo here even when
#     their profile page's og:image falls back to WA's generic blank-hero
#     placeholder (the meta tag and the actual bio-section photo aren't
#     always kept in sync). The endpoint's own signal for "no photo" is
#     clean: a real photo returns 200 image/jpeg; an athlete with none
#     returns 403 (verified against a known photo-less athlete) rather than
#     silently serving a placeholder image -- so a non-200 response is
#     treated as "no photo found", not a valid result.

#     Returns None on any failure -- never raises, same contract as the
#     other profileImage fetchers.
#     """
#     profile_url = _search_world_athletics_profile_url(athlete_name)
#     if not profile_url:
#         return None
#     id_match = re.search(r'-(\d+)$', profile_url.rstrip("/"))
#     if not id_match:
#         return None
#     athlete_code = id_match.group(1)
#     image_url = f"https://media.aws.iaaf.org/athletes/{athlete_code}.jpg"
#     try:
#         resp = requests.get(
#             image_url,
#             headers={"User-Agent": "SportsFan360-AthletePipeline/1.0 (contact: social@sportsfan360.com)"},
#             timeout=10,
#         )
#         if resp.status_code != 200 or not resp.headers.get("Content-Type", "").startswith("image/"):
#             print(f"World Athletics has no real photo for {athlete_name} (code {athlete_code})", file=sys.stderr)
#             return None
#         return image_url
#     except requests.exceptions.RequestException as e:
#         print(f"World Athletics image fetch failed for {athlete_name}: {e}", file=sys.stderr)
#         return None


# def get_profile_image_url(athlete_name: str, sport: Optional["Sport"] = None) -> Optional[str]:
#     """
#     Fetches a profile photo for the athlete. Tries, in order:
#       1. World Athletics direct photo (track & field only -- skipped
#          entirely for cricket/football/boxing, where WA won't have a
#          profile to find) -- checked FIRST since it's the authoritative
#          federation source and, per _get_world_athletics_image, more
#          complete/reliable than what Wikimedia turns up for track & field
#          athletes specifically.
#       2. Wikimedia Commons, plain name search
#       3. Wikimedia Commons, "<name> athlete" fallback query
#     Returns None if nothing found at any tier or on any request error --
#     never raises, since this is a nice-to-have enrichment, not a required
#     field.
#     """
#     # Try 1: World Athletics direct photo, track & field only.
#     if sport is not None and sport in _TRACK_FIELD_SPORTS:
#         result = _get_world_athletics_image(athlete_name)
#         if result:
#             return result

#     headers = {
#         "User-Agent": "SportsFan360-AthletePipeline/1.0 (contact: social@sportsfan360.com)"
#     }

#     def _search(query: str) -> Optional[str]:
#         resp = requests.get(
#             "https://commons.wikimedia.org/w/api.php",
#             params={
#                 "action": "query",
#                 "generator": "search",
#                 "gsrnamespace": 6,  # File: namespace
#                 "gsrsearch": query,
#                 "gsrlimit": 10,
#                 "prop": "imageinfo",
#                 "iiprop": "url",
#                 "iiurlwidth": 500,
#                 "format": "json",
#             },
#             headers=headers,
#             timeout=10,
#         )
#         resp.raise_for_status()
#         pages = resp.json().get("query", {}).get("pages", {})
#         for page in pages.values():
#             imageinfo = page.get("imageinfo", [])
#             if imageinfo:
#                 url = imageinfo[0].get("thumburl") or imageinfo[0].get("url")
#                 if url:
#                     return url
#         return None

#     try:
#         # Try 2: plain name search (Wikimedia Commons).
#         result = _search(athlete_name)
#         if result:
#             return result

#         # Try 3: broader fallback query in case the plain name matched
#         # nothing (e.g. disambiguation, unusual file naming).
#         result = _search(f"{athlete_name} athlete")
#         if result:
#             return result
#     except requests.exceptions.RequestException as e:
#         print(f"Wikimedia Commons image fetch failed for {athlete_name}: {e}", file=sys.stderr)

#     print(f"No profile image found for {athlete_name} (World Athletics + Wikimedia)", file=sys.stderr)
#     return None


# def _scrape_youtube_search(query: str) -> Optional[str]:
#     """
#     No-API-key fallback: scrapes YouTube's own search results page for the
#     first video ID. YouTube embeds initial search results as JSON inside a
#     <script>var ytInitialData = {...}</script> block server-side, so a
#     plain HTTP request (no headless browser needed) can see them --
#     videoRenderer entries carry "videoId" directly. Used when the Data API
#     key is unset OR returned no qualifying results. Returns None on no
#     match or any request error -- never raises, this feeds a nice-to-have
#     enrichment step, not a required field.
#     """
#     try:
#         resp = requests.get(
#             "https://www.youtube.com/results",
#             params={"search_query": query},
#             headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"},
#             timeout=10,
#         )
#         resp.raise_for_status()
#         match = re.search(r'"videoId":"([a-zA-Z0-9_-]{11})"', resp.text)
#         if match:
#             return f"https://www.youtube.com/watch?v={match.group(1)}"
#         print(f"YouTube search-page scrape found no video for query '{query}'", file=sys.stderr)
#         return None
#     except requests.exceptions.RequestException as e:
#         print(f"YouTube search-page scrape failed for query '{query}': {e}", file=sys.stderr)
#         return None


# def _scrape_bing_video_search(query: str) -> Optional[str]:
#     """
#     Free, no-API-key fallback: scrapes Bing's video search results page.
#     Unlike a YouTube-only search, this aggregates across YouTube, Vimeo,
#     Dailymotion, news-site embeds, etc. -- useful when the athlete simply
#     has no welcome/intro video on YouTube specifically but has one
#     elsewhere. Bing embeds each result's target page URL in a mediaurl=
#     query-string param on the thumbnail/click-through link, so pull that
#     out directly rather than parsing Bing's own redirect wrapper. Returns
#     the first http(s) video-page URL found, or None on no match or any
#     request error -- never raises, this feeds a nice-to-have enrichment
#     step, not a required field. NOTE: Bing's mediaurl= param format isn't
#     a documented/stable API and may drift over time, same caveat as the
#     other Bing/DDG scrapes in this file.
#     """
#     try:
#         resp = requests.get(
#             "https://www.bing.com/videos/search",
#             params={"q": query},
#             headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"},
#             timeout=10,
#         )
#         resp.raise_for_status()
#         for match in re.finditer(r'mediaurl=([^&"]+)', resp.text):
#             candidate = urllib.parse.unquote(match.group(1))
#             if candidate.startswith("http"):
#                 return candidate
#         print(f"Bing video search found no result for query '{query}'", file=sys.stderr)
#         return None
#     except requests.exceptions.RequestException as e:
#         print(f"Bing video search failed for query '{query}': {e}", file=sys.stderr)
#         return None


# def get_welcome_video_url(athlete_name: str, sport_label: str = "", max_duration_seconds: int = 300) -> Optional[str]:
#     """
#     Searches for a welcome/intro video for the athlete. Tries, in order:
#       1. YouTube Data API v3, query "<name> <sport> welcome interview",
#          restricted to YouTube's "medium" duration bucket (4-20 min) and
#          filtered further to max_duration_seconds using real durations
#          from videos.list -- picks the highest-view-count qualifying
#          candidate (only runs if YOUTUBE_API_KEY is set).
#       2. YouTube Data API v3 again, but with a broader query ("<name>
#          interview", no sport label) and no duration bucket restriction
#          -- catches cases where the sport-specific / duration-restricted
#          query was too narrow.
#       3. A no-API-key scrape of YouTube's own search results page, same
#          broad query.
#       4. A no-API-key scrape of Bing's video search results, same broad
#          query -- aggregates across YouTube, Vimeo, Dailymotion, and
#          news-site embeds, catching cases where the athlete has a
#          welcome/intro video that simply isn't on YouTube. Used only if
#          tiers 1-3 all come up empty.
#     Duration isn't verified for tiers 3-4 since there's no videos.list
#     call to check against -- a real, relevant video beats no video for
#     this nice-to-have field.
#     Returns None if nothing found at any tier or on any request error --
#     never raises, since this is a nice-to-have enrichment, not a required
#     field.
#     """
#     api_key = os.environ.get("YOUTUBE_API_KEY")

#     def _api_search(query: str, video_duration: Optional[str]) -> Optional[str]:
#         search_params = {
#             "key": api_key,
#             "q": query,
#             "part": "snippet",
#             "type": "video",
#             "maxResults": 10,
#             "order": "relevance",
#         }
#         if video_duration:
#             search_params["videoDuration"] = video_duration
#         try:
#             resp = requests.get(
#                 "https://www.googleapis.com/youtube/v3/search",
#                 params=search_params,
#                 timeout=10,
#             )
#             resp.raise_for_status()
#             items = resp.json().get("items", [])
#             video_ids = [item["id"]["videoId"] for item in items if item.get("id", {}).get("videoId")]
#             if not video_ids:
#                 return None

#             details_resp = requests.get(
#                 "https://www.googleapis.com/youtube/v3/videos",
#                 params={"key": api_key, "id": ",".join(video_ids), "part": "contentDetails,statistics"},
#                 timeout=10,
#             )
#             details_resp.raise_for_status()
#             details_items = details_resp.json().get("items", [])

#             qualifying = []
#             for detail in details_items:
#                 seconds = _parse_iso8601_duration(detail.get("contentDetails", {}).get("duration"))
#                 if seconds is None or seconds > max_duration_seconds:
#                     continue
#                 view_count = int(detail.get("statistics", {}).get("viewCount", 0))
#                 qualifying.append((view_count, detail["id"]))

#             if not qualifying:
#                 return None
#             qualifying.sort(key=lambda x: x[0], reverse=True)
#             return f"https://www.youtube.com/watch?v={qualifying[0][1]}"
#         except requests.exceptions.RequestException as e:
#             print(f"YouTube API search failed for query '{query}': {e}", file=sys.stderr)
#             return None

#     broad_query = f"{athlete_name} interview".strip()

#     if api_key:
#         # Try 1: sport-specific query, medium-duration bucket.
#         specific_query = f"{athlete_name} {sport_label} welcome interview".strip()
#         result = _api_search(specific_query, video_duration="medium")
#         if result:
#             return result

#         # Try 2: broader query, no duration bucket restriction.
#         result = _api_search(broad_query, video_duration=None)
#         if result:
#             return result
#         print(f"No YouTube API video found for {athlete_name} across both queries", file=sys.stderr)
#     else:
#         print("YOUTUBE_API_KEY not set, skipping YouTube API search", file=sys.stderr)

#     # Try 3: no-API-key YouTube scrape.
#     result = _scrape_youtube_search(broad_query)
#     if result:
#         return result

#     # Try 4: Bing video search, aggregates beyond just YouTube -- last resort.
#     return _scrape_bing_video_search(broad_query)


# def _parse_iso8601_duration(duration: Optional[str]) -> Optional[int]:
#     """Parses YouTube's ISO 8601 duration format (e.g. 'PT4M13S', 'PT1H2M') into total seconds."""
#     if not duration:
#         return None
#     match = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", duration)
#     if not match:
#         return None
#     hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
#     return hours * 3600 + minutes * 60 + seconds


# def save_draft_to_dynamodb(profile_dict: dict, sport: Sport, athlete_name: str) -> str | None:
#     """
#     Writes this LLM-generated profile into the DynamoDB review queue
#     (SportsData, entityId="REVIEW#<draft_id>") via
#     firebase_store.save_review_draft(), same pending_review pattern
#     athlete_pipeline.py already uses.

#     proposed_data is the full profile dict minus the internal _retry_log,
#     which is debug-only and shouldn't land on the live athlete record if
#     this draft is later approved.

#     Returns the new draft_id, or None if the write failed (e.g. AWS
#     creds/network not available in this environment) -- callers should
#     treat that as non-fatal since the local JSON save already succeeded.
#     """
#     proposed_data = {k: v for k, v in profile_dict.items() if k != "_retry_log"}

#     draft = {
#         "athlete_id": profile_dict["athlete_id"],
#         "athlete_name": athlete_name,
#         "sport": _sport_id_for(sport),
#         "trigger_reason": "llm_content_generation",
#         "proposed_data": proposed_data,
#         "status": "pending_review",
#     }

#     try:
#         draft_id = firebase_store.save_review_draft(draft)
#     except Exception as e:
#         print(f"⚠️ Failed to save draft to DynamoDB: {e}", file=sys.stderr)
#         return None

#     return draft_id


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

#         draft_id = save_draft_to_dynamodb(profile_dict, sport, athlete_name)
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
generate_athlete_content_llm.py

Standalone content generator: prompts Gemini (with Google Search grounding
enabled) to draft an athlete document matching sportsfan360-schema-v4.md —
`coreInfo` (identity/bio), `performance` (sport-specific stats), and
`record_highlight` (the athlete's most notable record, for the match-center
record card + the National/Olympic/World benchmark-comparison strip).
Scoped to the priority sports: cricket, football, boxing, shooting, and
track & field (one `sportId`, "athletics", covering 6 sub-disciplines:
sprints, middle_distance, long_distance, throws, jumps, relays).

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

import base64
import concurrent.futures
import json
import os
import re
import sys
import time
import typing
import urllib.parse
from datetime import date as date_type, datetime, timezone
from enum import Enum
from typing import Optional
import requests

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
    BOXING = "boxing"
    SHOOTING = "shooting"


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
    return sport.value  # "cricket" | "football" | "boxing" | "shooting"


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
    welcomeVideoUrl: Optional[str] = None
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

EVENT_BENCHMARK_BASELINES: dict[str, dict[str, dict[str, str]]] = {
    "400m": {
        "world": {"value": "43.03s", "holder": "Wayde van Niekerk", "date": "2016-08-14", "venue": "Rio de Janeiro", "tournament": "Olympic Games"},
        "olympic": {"value": "43.03s", "holder": "Wayde van Niekerk", "date": "2016-08-14", "venue": "Rio de Janeiro", "tournament": "Olympic Games"},
        "national": {"value": "45.21s", "holder": "Muhammed Anas Yahiya", "date": "2019-07-13", "venue": "Kladno", "tournament": "Kladno Memorial"},
    },
    "4x400m": {
        "world": {"value": "2:54.29", "holder": "United States", "date": "1993-08-22", "venue": "Stuttgart", "tournament": "World Championships"},
        "olympic": {"value": "2:54.43", "holder": "United States", "date": "2024-08-10", "venue": "Paris", "tournament": "Olympic Games"},
        "national": {"value": "3:00.25", "holder": "India (Anas/Jacob/Ajmal/Ramesh)", "date": "2023-08-26", "venue": "Budapest", "tournament": "World Championships"},
    },
    "100m": {
        "world": {"value": "9.58s", "holder": "Usain Bolt", "date": "2009-08-16", "venue": "Berlin", "tournament": "World Championships"},
        "olympic": {"value": "9.63s", "holder": "Usain Bolt", "date": "2012-08-05", "venue": "London", "tournament": "Olympic Games"},
        "national": {"value": "10.23s", "holder": "Manikanta Hoblidhar", "date": "2023-10-15", "venue": "Bengaluru", "tournament": "National Open"},
    },
    "200m": {
        "world": {"value": "19.19s", "holder": "Usain Bolt", "date": "2009-08-20", "venue": "Berlin", "tournament": "World Championships"},
        "olympic": {"value": "19.30s", "holder": "Usain Bolt", "date": "2008-08-20", "venue": "Beijing", "tournament": "Olympic Games"},
        "national": {"value": "20.52s", "holder": "Amlan Borgohain", "date": "2022-04-06", "venue": "Kozhikode", "tournament": "Federation Cup"},
    },
    "800m": {
        "world": {"value": "1:40.91", "holder": "David Rudisha", "date": "2012-08-09", "venue": "London", "tournament": "Olympic Games"},
        "olympic": {"value": "1:40.91", "holder": "David Rudisha", "date": "2012-08-09", "venue": "London", "tournament": "Olympic Games"},
        "national": {"value": "1:45.65", "holder": "Jinson Johnson", "date": "2018-06-27", "venue": "Guwahati", "tournament": "Inter-State"},
    },
    "1500m": {
        "world": {"value": "3:26.00", "holder": "Hicham El Guerrouj", "date": "1998-07-14", "venue": "Rome", "tournament": "Golden Gala"},
        "olympic": {"value": "3:27.65", "holder": "Cole Hocker", "date": "2024-08-06", "venue": "Paris", "tournament": "Olympic Games"},
        "national": {"value": "3:35.24", "holder": "Jinson Johnson", "date": "2019-09-01", "venue": "Berlin", "tournament": "ISTAF Berlin"},
    },
    "5000m": {
        "world": {"value": "12:35.36", "holder": "Joshua Cheptegei", "date": "2020-08-14", "venue": "Monaco", "tournament": "Herculis Monaco"},
        "olympic": {"value": "12:57.82", "holder": "Kenenisa Bekele", "date": "2008-08-23", "venue": "Beijing", "tournament": "Olympic Games"},
        "national": {"value": "13:18.92", "holder": "Gulveer Singh", "date": "2024-09-28", "venue": "Niigata", "tournament": "Yogibo Athletics"},
    },
    "10000m": {
        "world": {"value": "26:11.00", "holder": "Joshua Cheptegei", "date": "2020-10-07", "venue": "Valencia", "tournament": "NN Valencia"},
        "olympic": {"value": "26:43.14", "holder": "Joshua Cheptegei", "date": "2024-08-02", "venue": "Paris", "tournament": "Olympic Games"},
        "national": {"value": "27:41.81", "holder": "Gulveer Singh", "date": "2024-03-16", "venue": "San Juan Capistrano", "tournament": "The TEN"},
    },
    "4x100m": {
        "world": {"value": "36.84s", "holder": "Jamaica", "date": "2012-08-11", "venue": "London", "tournament": "Olympic Games"},
        "olympic": {"value": "36.84s", "holder": "Jamaica", "date": "2012-08-11", "venue": "London", "tournament": "Olympic Games"},
        "national": {"value": "38.89s", "holder": "India", "date": "2010-10-12", "venue": "New Delhi", "tournament": "Commonwealth Games"},
    },
    "javelin": {
        "world": {"value": "98.48m", "holder": "Jan Železný", "date": "1996-05-25", "venue": "Jena", "tournament": "Jena Grand Prix"},
        "olympic": {"value": "92.97m", "holder": "Arshad Nadeem", "date": "2024-08-08", "venue": "Paris", "tournament": "Olympic Games"},
        "national": {"value": "89.94m", "holder": "Neeraj Chopra", "date": "2022-06-30", "venue": "Stockholm", "tournament": "Stockholm Diamond League"},
    },
    "shot put": {
        "world": {"value": "23.56m", "holder": "Ryan Crouser", "date": "2023-05-27", "venue": "Los Angeles", "tournament": "USATF Los Angeles Grand Prix"},
        "olympic": {"value": "23.30m", "holder": "Ryan Crouser", "date": "2021-08-05", "venue": "Tokyo", "tournament": "Olympic Games"},
        "national": {"value": "21.77m", "holder": "Tajinderpal Singh Toor", "date": "2023-06-19", "venue": "Bhubaneswar", "tournament": "Inter-State Championships"},
    },
    "long jump": {
        "world": {"value": "8.95m", "holder": "Mike Powell", "date": "1991-08-30", "venue": "Tokyo", "tournament": "World Championships"},
        "olympic": {"value": "8.90m", "holder": "Bob Beamon", "date": "1968-10-18", "venue": "Mexico City", "tournament": "Olympic Games"},
        "national": {"value": "8.42m", "holder": "Jeshwin Aldrin", "date": "2023-03-02", "venue": "Bellary", "tournament": "Indian Open Jumps"},
    },
    "triple jump": {
        "world": {"value": "18.29m", "holder": "Jonathan Edwards", "date": "1995-08-07", "venue": "Gothenburg", "tournament": "World Championships"},
        "olympic": {"value": "18.09m", "holder": "Kenny Harrison", "date": "1996-07-27", "venue": "Atlanta", "tournament": "Olympic Games"},
        "national": {"value": "17.37m", "holder": "Praveen Chithravel", "date": "2023-05-06", "venue": "Havana", "tournament": "Prueba de Confrontacion"},
    },
    "10m air pistol": {
        "world": {"value": "594", "holder": "Jin Jong-oh", "date": "2009-04-12", "venue": "Changwon", "tournament": "ISSF World Cup"},
        "olympic": {"value": "591", "holder": "Mikhail Nestruev", "date": "2004-08-14", "venue": "Athens", "tournament": "Olympic Games"},
        "national": {"value": "587", "holder": "Saurabh Chaudhary", "date": "2020-01-14", "venue": "Bhopal", "tournament": "National Shooting Championship"},
    },
    "10m air rifle": {
        "world": {"value": "633.5", "holder": "Peter Sidi", "date": "2013-05-25", "venue": "Munich", "tournament": "ISSF World Cup"},
        "olympic": {"value": "632.7", "holder": "Yang Haoran", "date": "2021-07-25", "venue": "Tokyo", "tournament": "Olympic Games"},
        "national": {"value": "632.4", "holder": "Rudrankksh Patil", "date": "2022-10-14", "venue": "Cairo", "tournament": "ISSF World Championship"},
    },
    "50m rifle 3 positions": {
        "world": {"value": "596", "holder": "Jan Lochbihler", "date": "2019-08-28", "venue": "Rio de Janeiro", "tournament": "ISSF World Cup"},
        "olympic": {"value": "594", "holder": "Sergey Kamenskiy", "date": "2016-08-14", "venue": "Rio de Janeiro", "tournament": "Olympic Games"},
        "national": {"value": "593", "holder": "Swapnil Kusale", "date": "2022-05-18", "venue": "Baku", "tournament": "ISSF World Cup"},
    },
}


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
    events: Optional[int] = Field(None, description="total competitions entered that year")
    gold: int = 0
    silver: int = 0
    bronze: int = 0
    seasonBest: Optional[float] = Field(None, description="best mark that year, in the event's native unit, no suffix")
    seasonAverage: Optional[float] = Field(None, description="average mark that year, in the event's native unit, no suffix")
    currentStreak: Optional[int] = Field(None, description="active podium/win streak as of that year, if tracked")

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


class BoxingRecordStats(BaseModel):
    wins: Optional[int] = None
    losses: Optional[int] = None
    draws: Optional[int] = None
    knockouts: Optional[int] = Field(None, description="wins by KO/TKO specifically, a subset of `wins`")
    noContests: Optional[int] = None

    model_config = {"extra": "forbid"}


class BoxingChartConfig(BaseModel):
    primaryUnit: str = "wins"
    secondaryUnit: str = "knockouts"

    model_config = {"extra": "forbid"}


class BoxingMedalDataPoint(BaseModel):
    year: str
    events: Optional[int] = Field(None, description="total bouts/tournaments entered that year, if determinable")
    gold: int = 0
    silver: int = 0
    bronze: int = 0
    tournament: Optional[str] = Field(None, description="the tournament/event name this year's medal(s) came from, e.g. 'IBA Women's World Boxing Championships' — if multiple medals in different tournaments that year, name the most significant one")
    seasonBest: Optional[str] = Field(None, description="the single best result value that year, e.g. 'Gold' — just the medal color/outcome, NOT the tournament name")
    seasonAverage: Optional[str] = Field(None, description="the overall medal-color value for that year, e.g. 'Gold', 'Silver', 'Bronze' — if multiple medals that year, the most common or highest-value color")
    currentStreak: Optional[int] = Field(None, description="active win streak as of that year, if determinable; null if not")

    model_config = {"extra": "forbid"}


class BoxingAnalytics(BaseModel):
    """Boxing analytics — same generic dict-container shape as
    cricket/football's Analytics for seasonalData/radarData/
    coachImpactData/consistencyData/heatmapData (no reference export
    exists for boxing yet), but with a typed medalData field (unlike
    cricket/football) since boxing's medals are well-documented,
    tournament-by-tournament facts worth structuring properly rather than
    leaving as a free-form dict."""
    seasonalData: dict = Field(default_factory=dict)
    radarData: dict = Field(default_factory=dict)
    coachImpactData: dict = Field(default_factory=dict)
    consistencyData: dict = Field(default_factory=dict)
    heatmapData: dict = Field(default_factory=dict)
    medalData: list[BoxingMedalDataPoint] = Field(
        default_factory=list,
        description="medal counts PER YEAR (not career totals — those stay in performance.recordStats/titles), "
                     "one entry per year with any international medal",
    )

    model_config = {"extra": "forbid"}


class BoxingPerformance(BaseModel):
    weightClass: Optional[str] = Field(None, description="e.g. 'Lightweight', 'Welterweight', '75kg'")
    stance: Optional[str] = Field(None, description="'Orthodox' | 'Southpaw' | 'Switch'")
    reachCm: Optional[int] = None
    heightAdvantageNote: Optional[str] = Field(
        None, description="only fill if a specific reach/height comparison vs a notable opponent is documented; otherwise null"
    )
    tournament: Optional[str] = Field(None, description="e.g. 'professional', 'amateur', 'olympic_boxing', a specific title/promotion name")
    titles: list[str] = Field(
        default_factory=list, description="list of titles held or won, e.g. ['WBC World Lightweight Champion'] — [] if none"
    )
    recordStats: Optional[BoxingRecordStats] = None
    chartConfig: BoxingChartConfig = Field(default_factory=BoxingChartConfig)

    model_config = {"extra": "forbid"}


# ── Shooting — same pattern as Boxing: generic dict-container analytics
# (no reference export exists for shooting yet) but a typed medalData
# field, since shooting medals (like boxing's) are well-documented,
# tournament-by-tournament facts worth structuring properly. Shooting's
# "record" is a SCORE, not a time/distance, and different disciplines use
# incompatible scoring scales (e.g. Air Rifle qualification ~600-654.x on
# the current decimal-scoring system vs. a Trap/Skeet score out of 125
# hits vs. a Finals score on yet another scale) — see
# performance.chartConfig / analytics.yAxisDomain notes in
# _sport_specific_field_notes for how that's handled: derived per-athlete
# from the actual discipline rather than a single fixed scale like
# athletics' seconds/meters split. ──

class ShootingRecordStats(BaseModel):
    olympicMedals: Optional[int] = Field(None, description="total Olympic medals (all colors), career total")
    worldChampionshipMedals: Optional[int] = Field(None, description="total World Championship medals (all colors), career total")
    worldRecords: Optional[int] = Field(None, description="number of world records currently or previously held, career total")
    finalsAppearances: Optional[int] = Field(None, description="number of major-event (Olympic/World Championship) finals reached, career total")

    model_config = {"extra": "forbid"}


class ShootingChartConfig(BaseModel):
    primaryUnit: str = "score"
    secondaryUnit: str = "hits"

    model_config = {"extra": "forbid"}


class ShootingMedalDataPoint(BaseModel):
    year: str
    events: Optional[int] = Field(None, description="total competitions entered that year, if determinable")
    gold: int = 0
    silver: int = 0
    bronze: int = 0
    tournament: Optional[str] = Field(None, description="the tournament/event name this year's medal(s) came from, e.g. 'ISSF World Cup Munich' — if multiple medals in different tournaments that year, name the most significant one")
    seasonBest: Optional[str] = Field(None, description="best score that year, as a plain string in the discipline's native scoring unit, e.g. '633.1' or '125' — NOT a medal color, NOT the tournament name")
    seasonAverage: Optional[str] = Field(None, description="average score that year across competitions found, same unit as seasonBest — a reasonable estimate from available results is fine, same as athletics' seasonAverage")
    currentStreak: Optional[int] = Field(None, description="active podium/final-qualification streak as of that year, if determinable; null if not")

    model_config = {"extra": "forbid"}


class ShootingAnalytics(BaseModel):
    """Shooting analytics — same generic dict-container shape as boxing's
    for seasonalData/radarData/coachImpactData/consistencyData/
    heatmapData (no reference export exists for shooting yet), plus a
    typed medalData field like boxing's, since shooting medals are
    well-documented, tournament-by-tournament facts."""
    seasonalData: dict = Field(default_factory=dict)
    radarData: dict = Field(default_factory=dict)
    coachImpactData: dict = Field(default_factory=dict)
    consistencyData: dict = Field(default_factory=dict)
    heatmapData: dict = Field(default_factory=dict)
    medalData: list[ShootingMedalDataPoint] = Field(
        default_factory=list,
        description="medal counts PER YEAR (not career totals — those stay in performance.recordStats), "
                     "one entry per year with any international medal",
    )

    model_config = {"extra": "forbid"}


class ShootingPerformance(BaseModel):
    discipline: Optional[str] = Field(
        None,
        description=(
            "the athlete's specific shooting event, e.g. '10m Air Rifle', '10m Air Pistol', "
            "'25m Rapid Fire Pistol', '50m Rifle 3 Positions', 'Trap', 'Skeet', "
            "'25m Pistol' — search for the athlete's actual specific event, never leave generic"
        ),
    )
    category: Optional[str] = Field(None, description="exactly one of: 'Rifle' | 'Pistol' | 'Shotgun'")
    currentSeason: Optional[str] = None
    medalCabinet: list[MedalEntry] = Field(default_factory=list)
    stats: Optional[AthleticsStats] = Field(
        None, description="reuse the personalBest/seasonBest shape — personalBest/seasonBest here are SCORES (e.g. '633.1', '125'), not times/distances"
    )
    recordStats: Optional[ShootingRecordStats] = None
    chartConfig: ShootingChartConfig = Field(default_factory=ShootingChartConfig)

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


class BoxingAthleteDocument(AthleteDocumentBase):
    performance: BoxingPerformance
    analytics: BoxingAnalytics = Field(default_factory=BoxingAnalytics)


class ShootingAthleteDocument(AthleteDocumentBase):
    performance: ShootingPerformance
    analytics: ShootingAnalytics = Field(default_factory=ShootingAnalytics)


def get_schema_for_sport(sport: Sport) -> type[AthleteDocumentBase]:
    if sport in _TRACK_FIELD_SPORTS:
        return AthleticsAthleteDocument
    if sport == Sport.CRICKET:
        return CricketAthleteDocument
    if sport == Sport.BOXING:
        return BoxingAthleteDocument
    if sport == Sport.SHOOTING:
        return ShootingAthleteDocument
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
            "coreInfo.heightCm",
            "coreInfo.weightKg",
            "performance.medalCabinet",
            "performance.stats.personalBest",
            "performance.stats.seasonBest",
            "record_highlight",
            "record_highlight.benchmarks",
            "record_highlight.progressData",
            "analytics.seasonalData",
            "analytics.radarData",
            "analytics.medalData",
            "analytics.stats",
            "analytics.consistencyData",
            "analytics.heatmapData",
        ]
        for s in _TRACK_FIELD_SPORTS
    },
    Sport.BOXING: [
        "coreInfo.coachName",
        "coreInfo.heightCm",
        "coreInfo.weightKg",
        "performance.weightClass",
        "performance.stance",
        "performance.reachCm",
        "performance.tournament",
        "performance.titles",
        "performance.recordStats",
        "record_highlight",
        "record_highlight.benchmarks",
        "analytics.seasonalData",
        "analytics.radarData",
        "analytics.medalData",
    ],
    Sport.SHOOTING: [
        "coreInfo.coachName",
        "coreInfo.heightCm",
        "coreInfo.weightKg",
        "performance.discipline",
        "performance.category",
        "performance.medalCabinet",
        "performance.stats.personalBest",
        "performance.stats.seasonBest",
        "performance.recordStats",
        "record_highlight",
        "record_highlight.benchmarks",
        "analytics.seasonalData",
        "analytics.radarData",
        "analytics.medalData",
    ],
    Sport.CRICKET: [
        "coreInfo.coachName",
        "coreInfo.heightCm",
        "coreInfo.weightKg",
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
        "coreInfo.heightCm",
        "coreInfo.weightKg",
        "performance.team",
        "performance.attackingStats",
        "performance.playmakingStats",
        "record_highlight",
        "record_highlight.benchmarks",
        "analytics.seasonalData",
        "analytics.radarData",
    ],
}


_MEDAL_DATA_OPTIONAL_FIELDS = ("events", "seasonBest", "seasonAverage", "currentStreak")
# Boxing's medalData only has `events` as an optional sub-field beyond
# gold/silver/bronze — seasonBest/averageThrow/currentStreak don't apply
# to boxing (see BoxingMedalDataPoint), so using the full athletics tuple
# here would make boxing's medalData retry forever (those keys never
# exist on a boxing entry, so _is_empty() on a missing key is always
# True). Use a separate, narrower tuple for boxing.
_BOXING_MEDAL_DATA_OPTIONAL_FIELDS = ("events", "tournament", "seasonBest", "seasonAverage", "currentStreak")
# Shooting's medalData has the same shape as boxing's (year/events/gold/
# silver/bronze/tournament/seasonBest/seasonAverage/currentStreak) — reuse
# the same optional-fields tuple to avoid infinite-retry on keys that
# genuinely apply here (unlike athletics' seasonBest/seasonAverage being
# numeric there vs. string here, the key SET is identical to boxing's).
_SHOOTING_MEDAL_DATA_OPTIONAL_FIELDS = _BOXING_MEDAL_DATA_OPTIONAL_FIELDS


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


def _medal_data_has_missing_subfields(entries: list, optional_fields: tuple = _MEDAL_DATA_OPTIONAL_FIELDS) -> bool:
    """True if analytics.medalData has at least one entry, and at least
    one of those entries is missing one of `optional_fields`. gold/silver/
    bronze are excluded — those default to 0 and are basically never left
    null. `optional_fields` defaults to the full athletics set but callers
    should pass the sport-appropriate tuple (e.g.
    _BOXING_MEDAL_DATA_OPTIONAL_FIELDS for boxing)."""
    if not entries:
        return False
    for e in entries:
        if not isinstance(e, dict):
            continue
        for k in optional_fields:
            if _is_empty(e.get(k)):
                return True
    return False


_RECORD_HIGHLIGHT_CORE_FIELDS = ("event", "result", "type", "date")


def _record_highlight_core_is_empty(record_highlight) -> bool:
    """True if record_highlight's actual record fields (event/result/
    type/date) are all still null, even though the dict itself may be
    non-empty because benchmarks got filled. This is the case
    _is_empty()'s whole-dict check misses."""
    if not isinstance(record_highlight, dict):
        return True
    return all(_is_empty(record_highlight.get(f)) for f in _RECORD_HIGHLIGHT_CORE_FIELDS)


def _missing_retryable_paths(data: dict, sport: "Sport") -> list[str]:
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
        if p == "analytics.medalData":
            entries = _get_path(data, p) or []
            if sport == Sport.BOXING:
                optional_fields = _BOXING_MEDAL_DATA_OPTIONAL_FIELDS
            elif sport == Sport.SHOOTING:
                optional_fields = _SHOOTING_MEDAL_DATA_OPTIONAL_FIELDS
            else:
                optional_fields = _MEDAL_DATA_OPTIONAL_FIELDS
            if _is_empty(entries) or _medal_data_has_missing_subfields(entries, optional_fields):
                missing.append(p)
            continue
        # record_highlight — flag as missing if the core record fields
        # are null, even though the dict overall isn't empty (benchmarks
        # alone would otherwise mask this).
        if p == "record_highlight":
            rh = _get_path(data, p)
            if _record_highlight_core_is_empty(rh):
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
- analytics.medalData: list of {{year, events, gold, silver, bronze, seasonBest, seasonAverage,
  currentStreak}} objects, one entry per year with any international medal — NOT career
  totals (those go in stats). events = total competitions entered that year. gold/silver/
  bronze = medal counts for that year. seasonBest = the athlete's best mark that year, a
  plain number in the event's native unit (matches seasonalData's value for that year where
  possible). seasonAverage = average mark across that year's competitions, same unit, no
  suffix — a small handful of individual results from that year is enough to ESTIMATE this
  from (you do NOT need every competition result to compute an exact average); a reasonable
  estimate based on seasonBest plus whatever other results you find for that year is expected
  and preferred over leaving this null, the same way consistencyData below is an estimate, not
  a literal database export. currentStreak = the athlete's active podium or win streak as of
  that year, if you can determine one from the results found; null if not determinable. Only
  leave seasonAverage/currentStreak null for a year if you genuinely found only a single
  result (seasonBest) for that year and have no other basis to estimate from — don't leave
  gold/silver/bronze null (use 0).
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

    if sport == Sport.BOXING:
        return """
- performance.weightClass: the athlete's current/most recent weight class, e.g. "Lightweight",
  "Welterweight", or the kg figure used in amateur/Olympic boxing (e.g. "75kg").
- performance.stance: exactly one of "Orthodox" | "Southpaw" | "Switch" if documented,
  otherwise null.
- performance.reachCm: plain integer, only if a reach measurement is publicly documented.
- performance.tournament: "professional", "amateur", "olympic_boxing", or a specific
  promotion/title name if that's what the record refers to — be specific.
- performance.titles: list of title strings, e.g. ["WBC World Lightweight Champion", "2023
  National Championships Gold"] — search specifically for championship belts, national titles,
  and Olympic/Commonwealth Games medals. [] only if the athlete holds no titles.
- performance.recordStats: {wins, losses, draws, knockouts, noContests} — knockouts is wins
  BY knockout specifically (a subset of wins, not additional to it). These are usually
  well-documented, stable career totals for any professional or notable amateur boxer —
  search confidently and fill these; only null out a specific figure (e.g. noContests) if
  genuinely undocumented, which is common and fine for that one field.

record_highlight.benchmarks (the National/Olympic/World record-comparison strip — ALWAYS
return exactly 3 entries with label "National", "Olympic", "World" in that order, even when
record_highlight itself has nothing for this athlete. Boxing has no single "record" the way
a timed sport does, so map the 3 tiers to the closest boxing equivalents via `tournament`:
"National" = a notable Indian national boxing achievement/record for this weight class (e.g.
most national titles), "Olympic" = the athlete's own Olympic boxing result/medal if
applicable, or the weight class's most decorated Olympic boxer, "World" = the outright
weight class's most notable world-title record (e.g. most consecutive world title defenses).
Fill value/holder/date/venue/tournament for each where findable; null is acceptable more
often here than in timed sports, since boxing doesn't have a single universally-tracked
numeric record the way track events do.

analytics fields (derive from facts you actually find via search — avoid leaving these null
when the underlying stat is realistically findable; only use {} after a genuine search comes
up empty):
- analytics.seasonalData: {"labels": [...years, most recent last...], "wins": [...],
  "losses": [...]} — year-by-year fight record if findable via a boxing stats site (e.g.
  BoxRec) or career recap articles. {} only if truly just the career total is findable.
- analytics.medalData: list of {year, events, gold, silver, bronze, tournament, seasonBest,
  seasonAverage, currentStreak} objects, one entry per year with any international medal —
  NOT career totals (those stay in performance.recordStats and performance.titles).
  year MUST be a specific 4-digit year string (e.g. "2022") — every medal you found has a
  known year attached to the event it was won at (searching "<tournament name> year" or
  "<athlete> <tournament> results" will surface it); do NOT return "N/A" for year unless you
  genuinely cannot determine which year a specific medal belongs to after trying this, which
  should be rare. events = total bouts/tournaments entered that year. If you can find the athlete's full
  year's schedule, use that exact count. If not, use 1 as a floor for each distinct
  tournament reflected in that year's medal(s)/results (e.g. a year with one gold medal
  tournament and nothing else found -> events=1) rather than leaving this null — a
  minimum-known count is more useful than no count. Only leave events null if you cannot
  even confirm which tournament(s) the athlete competed in that year, which should be rare
  given tournament is already being filled. gold/silver/bronze = medal counts for that specific year
  (derive from the titles/medals you found and their dates) — use 0, never null, for years
  with no medal of that color. tournament = the name of the tournament/event this year's
  medal(s) came from, e.g. "IBA Women's World Boxing Championships" — if multiple medals
  came from different tournaments that year, name the most significant one here. seasonBest
  and seasonAverage MUST each be filled with a plain medal-color value, e.g. "Gold" or
  "Bronze" — NOT a sentence, NOT the tournament name, NOT null. seasonBest is the single
  best medal color won that year; seasonAverage is the overall/most-common medal color that
  year (same value as seasonBest if only one medal was won that year — this is expected and
  correct, not a mistake). Since gold/silver/bronze are already known from the counts above,
  derive seasonBest/seasonAverage directly from them: e.g. gold=1,silver=0,bronze=0 ->
  seasonBest="Gold", seasonAverage="Gold"; gold=0,silver=1,bronze=1 -> seasonBest="Silver",
  seasonAverage="Silver" (the higher-value color). currentStreak = the athlete's active win
  streak as of that year if determinable; null if not. Only leave seasonBest/seasonAverage
  null for a year with zero medals (gold=silver=bronze=0) — every year with at least one
  medal MUST have both filled. [] only if no year-by-year medal breakdown is derivable at
  all from what you found (rare, given performance.titles is usually well-documented).
- analytics.radarData: {"axes": ["Power", "Defense", "Footwork", "Ring IQ"], "values": [...four
  0-100 scores...]}. These ARE your editorial estimates (like aiInsight), not hard facts —
  score based on the actual recordStats/titles you found (e.g. a high knockout ratio scores
  well on "Power"). Do not default to a flat score for every axis. {} only if recordStats is
  largely null.
- analytics.consistencyData: {"note": <1 short factual sentence, e.g. a notable win streak or
  title-defense streak, if found>} — {} if nothing specific found.
- analytics.coachImpactData: only fill if you find a clear, specific before/after comparison
  tied to a documented trainer/coach change — {} is the expected/correct answer for most
  boxers.
- analytics.heatmapData: {} — no spatial/zone equivalent for boxing; always {}, not a miss.
"""
    if sport == Sport.SHOOTING:
        return """
- performance.discipline: the athlete's specific shooting event, e.g. "10m Air Rifle", "10m
  Air Pistol", "25m Rapid Fire Pistol", "50m Rifle 3 Positions", "Trap", "Skeet", "25m
  Pistol" — search for the athlete's actual specific event, never leave generic. This
  determines the athlete's scoring scale (see chartConfig note below), so get it right.
- performance.category MUST be exactly one of "Rifle" | "Pistol" | "Shotgun", matching the
  discipline (e.g. "10m Air Rifle" -> "Rifle", "Trap"/"Skeet" -> "Shotgun").
- performance.stats.personalBest / seasonBest: plain strings, the SCORE in the discipline's
  own native scale — e.g. "633.1" for a 60-shot Rifle/Pistol qualification (current
  decimal-scoring system, max ~654.x), "125" for a perfect Trap/Skeet round (out of 125
  hits), or a Finals-format score if that's the athlete's most notable mark (Finals use a
  different, typically lower and decimal-scored scale than qualification — do not average or
  compare Finals and qualification scores as if they're the same scale). Never bare numbers,
  and never invent a plausible-looking score — only report a mark you actually find.
- performance.chartConfig / analytics.yAxisDomain: DERIVE the sensible score range from the
  athlete's actual discipline and personal best you found — do NOT default to a fixed
  athletics-style scale, since shooting disciplines use genuinely different scoring systems
  (e.g. a 10m Air Rifle qualification score of 633.1 needs roughly a [615, 655] range; a Trap
  score of 118/125 needs roughly a [100, 125] range) — get this from what you actually found
  for that specific discipline, not a guess.
- performance.recordStats: {olympicMedals, worldChampionshipMedals, worldRecords,
  finalsAppearances} — all plain integers, career totals. These are usually well-documented
  for any Olympic-level shooter — search confidently and fill these; only null out a specific
  figure if genuinely undocumented.
- performance.medalCabinet: list of {event, medal, category}, medal is exactly "GOLD" |
  "SILVER" | "BRONZE". Include at most the 8 most significant international medals.

record_highlight.benchmarks (the National/Olympic/World record-comparison strip — ALWAYS
return exactly 3 entries with label "National", "Olympic", "World" in that order, even when
record_highlight itself has nothing for this athlete — this compares the EVENT/discipline's
own records, not just this athlete's own, and the record here is the discipline's SCORE
record, not this athlete's personal score unless they happen to hold it):
- For each: value (the record SCORE, in the discipline's native scale, e.g. "633.1" or "125"
  — never mix scales across entries; each entry should be for the SAME discipline as
  performance.discipline), holder, date, venue, tournament/meet name. World and Olympic
  records for well-known Olympic shooting disciplines are stable, well-documented facts
  (ISSF publishes and tracks these) — search for and fill these confidently. National can
  genuinely be hard-to-verify for less-tracked disciplines/countries — null is acceptable
  there ONLY after an actual search attempt.

analytics fields (derive from facts you actually find via search — avoid leaving these null
when the underlying stat is realistically findable; only use {} after a genuine search comes
up empty):
- analytics.seasonalData: {"labels": [...years, most recent last...], "scores": [...]} —
  year-by-year best qualification scores if findable via ISSF results archives or season
  recap articles, all in the SAME native scale as performance.discipline. {} only if truly
  just the career-best is findable.
- analytics.medalData: list of {year, events, gold, silver, bronze, tournament, seasonBest,
  seasonAverage, currentStreak} objects, one entry per year with any international medal —
  NOT career totals (those stay in performance.recordStats). year MUST be a specific
  4-digit year string — every medal you found has a known year attached to the event it was
  won at; do NOT return "N/A" for year unless you genuinely cannot determine it after trying.
  events = total competitions entered that year — use 1 as a floor for each distinct
  tournament reflected in that year's medal(s)/results rather than leaving this null.
  gold/silver/bronze = medal counts for that year — use 0, never null, for years with no
  medal of that color. tournament = the name of the tournament this year's medal(s) came
  from, e.g. "ISSF World Cup Munich" — if multiple, name the most significant one.
  seasonBest and seasonAverage MUST each be filled with a plain SCORE string in the
  discipline's native scale, e.g. "633.1" — NOT a medal color, NOT the tournament name, NOT
  null (unlike boxing, shooting scores this way since shooting has a real numeric score to
  report, not just a medal outcome). If you can only find ONE score for that year (or the
  scores you found are close enough that a real average isn't meaningfully different), SET
  seasonAverage EQUAL TO seasonBest as the floor estimate — do not leave seasonAverage null
  just because you don't have multiple results to average; only a genuinely unfound
  seasonBest justifies leaving seasonAverage null too. currentStreak = active
  podium/finals-qualification streak as of that year, if determinable; null if not. Only
  leave seasonBest/seasonAverage null for a year with zero medals — every year with at least
  one medal MUST have both filled, with seasonAverage=seasonBest being an acceptable and
  expected floor value, not a missed field. [] only if no year-by-year medal breakdown is
  derivable at all.
- analytics.radarData: {"axes": ["Precision", "Consistency", "Finals Nerve", "Big-Meet
  Performance"], "values": [...four 0-100 scores...]}. These ARE your editorial estimates
  (like aiInsight), not hard facts — score based on the actual scores/recordStats you found
  relative to what's typical for the discipline (e.g. a qualification score consistently
  near the world-record range scores high on "Precision"; a strong Finals record relative to
  qualification rank scores high on "Finals Nerve"). Do not default to a flat score for every
  axis. {} only if scores/recordStats are largely null.
- analytics.consistencyData: {"note": <1 short factual sentence on consistency, e.g. "Rarely
  scores below 625 in qualification", if found>} — {} if nothing specific found.
- analytics.coachImpactData: only fill if you find a clear, specific before/after comparison
  tied to a documented coaching change — {} is the expected/correct answer for most shooters.
- analytics.heatmapData: {} — no spatial/zone equivalent for shooting scores; always {}, not
  a miss.
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


def _record_highlight_sport_hint(sport: Sport) -> str:
    """Sport-specific nudge inserted into the (otherwise generic)
    record_highlight instructions. Added after Shooting's record_highlight
    consistently came back entirely null in real runs (Manu Bhaker draft)
    even though she demonstrably holds/has held a National Record in 10m
    Air Pistol — the generic "notable record" phrasing doesn't cue Gemini
    toward what a shooting record actually looks like (a qualification
    SCORE in a specific discipline), the way "9.58s" or "88.13m" obviously
    reads as a track & field record. Track & field/cricket/boxing/football
    don't need this since their record shape is already implied by
    _sport_specific_field_notes' benchmarks discussion, which record_
    highlight explicitly points back to."""
    if sport == Sport.SHOOTING:
        return (
            "\nSHOOTING-SPECIFIC: a shooting record is a qualification or Finals SCORE in a "
            "specific discipline (e.g. Manu Bhaker's National Record of a specific score in "
            "10m Air Pistol) — search specifically for \"<athlete> national record "
            "<discipline>\" and \"<athlete> personal best <discipline>\" rather than a generic "
            "record search. Most Olympic-level shooters hold or have held at least a National "
            "Record at some point in their career even if they don't currently — report the "
            "most recent one they held/hold, not only an active current record. Even if no "
            "single official record is confirmable, still fill progressData with the "
            "athlete's best qualification score per year (reuse the same figures as "
            "analytics.seasonalData) rather than leaving it empty — that's about their "
            "performance history, not contingent on holding an official record."
        )
    return ""


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

SPEED & SEARCH DIRECTIVE:
Execute 2 focused Google Search queries:
1. "{athlete_name} {sport_id} career records personal best stats world athletics"
2. "{athlete_name} {sport_id} event world record olympic record national record"
Synthesize a rich, complete JSON document directly from these findings in your very next turn. Speed is required (must finish in under 20-25 seconds). Ensure coreInfo, performance, record_highlight (including the 3 benchmarks), and analytics are dynamically populated with concrete numbers and facts.

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
- country MUST be the full English name of the country (e.g. "India", "United States"), NEVER an emoji flag.
- flag MUST be the 2-letter country code or emoji flag, e.g. "IN" or "🇮🇳".
- gender MUST be exactly "Men" or "Women" — used for filtering, not editorial content.
- dob MUST be a full ISO date "YYYY-MM-DD" if known, otherwise null — never a bare year.
- bio: 2-4 factual sentences highlighting their biggest achievements and records, no editorializing.
- coachName MUST be just the coach's plain name (e.g. "Rana Reider"), never a name with a
  parenthetical annotation like "(former coach)" attached.

performance fields (this sport's specific shape):
{_sport_specific_field_notes(sport, cricket_format=cricket_format)}

record_highlight: for the match-center RECORD CARD.
- You MUST populate this section with the athlete's most notable record, national/junior record, or career Personal Best ("PB").
- If the athlete has a standout record or PB in an adjacent or primary event (e.g. 400m for a relay sprinter, or individual discipline), report that notable record/PB here!
- Do NOT return all nulls for record_highlight if the athlete has any known record, medal performance, or personal best.
- Fill:
  - event: the record or PB event (e.g. "400m" or "4x400m Relay")
  - result: the record/PB time/score (e.g. "45.81s" or "3:05.38")
  - type: exactly one of: "GR", "NR", "WR", "CR", "PB" (use "NR" for national records, "PB" for personal bests)
  - typeFull: "National Record", "Personal Best", "World Record", etc.
  - phase: heat or final where it occurred (e.g. "Heat 5", "Final", or null)
  - date: ISO date "YYYY-MM-DD"
  - city: city where the record was achieved
  - prevRecord, prevRecordPlace, prevHolder, improvement, improvementDate
  - aiInsight: 2-3 sentences explaining the significance of this record/milestone
  - progressData: at least 2-3 historical progression values across DISTINCT calendar years (e.g. [{{"year": "2024", "value": 47.20}}, {{"year": "2025", "value": 46.56}}, {{"year": "2026", "value": 45.81}}]). Each entry MUST have a unique calendar year; do NOT repeat the same year twice.
  - benchmarks: exactly 3 entries with label "National", "Olympic", "World" in that order.
    - These compare the record event's all-time top marks across the 3 tiers (regardless of whether this athlete personally holds them).
    - "World": the official world record mark, record holder, date, and venue for this event.
    - "Olympic": the official Olympic Games record mark, record holder, date, and venue for this event.
    - "National": the official senior national record mark, record holder, date, and venue of the athlete's home country for this event.
    - DYNAMIC LOOKUP: World, Olympic, and National records are public, established sporting facts. You MUST fill value (e.g. "43.03s" or "3:00.25"), holder, date, and venue dynamically for each of the 3 tiers using search and sports knowledge. Do NOT leave value, holder, date, or venue null.
- If the athlete does not hold a senior national record, feature their career Personal Best ("PB") or junior record as their record_highlight.

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
        # Empty/near-empty response -- surface WHY, since a blank
        # "did not contain valid JSON: " gives no signal. Check
        # finish_reason and safety ratings on the first candidate, since
        # Gemini can return an empty text body without raising an
        # exception (safety filtering, truncation before any output,
        # or a search-grounding-only turn with no text emitted).
        diag = ""
        try:
            candidates = getattr(response_meta, "candidates", None) or []
            if candidates:
                c = candidates[0]
                finish_reason = getattr(c, "finish_reason", None)
                safety_ratings = getattr(c, "safety_ratings", None)
                diag = f" [finish_reason={finish_reason}"
                if safety_ratings:
                    blocked = [r for r in safety_ratings if getattr(r, "blocked", False)]
                    if blocked:
                        diag += f", blocked_categories={[getattr(r, 'category', r) for r in blocked]}"
                diag += "]"
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

    # Backfill required fields the LLM omitted entirely (key absent from
    # `data`, not just null) -- the loop above only visits keys that
    # exist in `data`, so a genuinely missing key (e.g. boxing medalData
    # entries missing "year") never gets set and Pydantic fails with
    # "Field required" at final validation. Only string-typed required
    # fields get a safe "N/A" fallback here; other required types are
    # left for Pydantic to report normally, since there is no safe
    # generic default for them.
    for name, field in model_cls.model_fields.items():
        if name in result:
            continue
        if not field.is_required():
            continue
        annotation, _ = _unwrap_optional(field.annotation)
        if annotation is str:
            result[name] = "N/A"

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
    National/Olympic/World entries in order. Preserves dynamic search
    results when present, and fills missing Olympic/World marks from
    authoritative event baselines to prevent null cards in the UI."""
    if not isinstance(record_highlight, dict):
        return record_highlight
    raw = record_highlight.get("benchmarks")
    by_label = {}
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict) and isinstance(entry.get("label"), str):
                by_label[entry["label"].strip().lower()] = dict(entry)

    # Determine event key for fallback lookup
    event_str = str(record_highlight.get("event") or "").lower()
    matched_benchmarks = None
    for k, v in EVENT_BENCHMARK_BASELINES.items():
        if k in event_str:
            matched_benchmarks = v
            break
    if not matched_benchmarks:
        if "4x400" in event_str or ("relay" in event_str and "400" in event_str):
            matched_benchmarks = EVENT_BENCHMARK_BASELINES.get("4x400m")
        elif "4x100" in event_str or "relay" in event_str:
            matched_benchmarks = EVENT_BENCHMARK_BASELINES.get("4x100m")
        elif "400" in event_str:
            matched_benchmarks = EVENT_BENCHMARK_BASELINES.get("400m")
        elif "100" in event_str:
            matched_benchmarks = EVENT_BENCHMARK_BASELINES.get("100m")
        elif "200" in event_str:
            matched_benchmarks = EVENT_BENCHMARK_BASELINES.get("200m")
        elif "javelin" in event_str:
            matched_benchmarks = EVENT_BENCHMARK_BASELINES.get("javelin")
        elif "pistol" in event_str:
            matched_benchmarks = EVENT_BENCHMARK_BASELINES.get("10m air pistol")
        elif "rifle" in event_str:
            matched_benchmarks = EVENT_BENCHMARK_BASELINES.get("10m air rifle")

    benchmarks_list = []
    for label in _BENCHMARK_LABELS:
        entry = dict(by_label.get(label.lower(), {}))
        # If entry has no value or holder, populate from baseline
        if matched_benchmarks:
            std = matched_benchmarks.get(label.lower(), {})
            for field in ("value", "holder", "date", "venue", "tournament"):
                if not entry.get(field) and std.get(field):
                    entry[field] = std[field]
        entry["label"] = label
        benchmarks_list.append(entry)

    record_highlight["benchmarks"] = benchmarks_list
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


def _sanitize(data: dict, sport: "Sport" = None) -> dict:
    core_info = data.get("coreInfo")
    if isinstance(core_info, dict):
        coach = core_info.get("coachName")
        if isinstance(coach, str):
            core_info["coachName"] = re.sub(r"\s*\([^)]*\)", "", coach).strip() or None
        country = core_info.get("country")
        if isinstance(country, str):
            # If model returned an emoji flag in country, fix country and flag
            if "🇮🇳" in country or country.strip().lower() in ("india", "in"):
                core_info["country"] = "India"
                core_info["flag"] = "IN"
            else:
                core_info["country"] = country.strip()

    record_highlight = data.get("record_highlight")
    if not isinstance(record_highlight, dict):
        record_highlight = {}
        data["record_highlight"] = record_highlight

    # If LLM omitted result/event in record_highlight, auto-populate from athlete's stats/bio
    if not record_highlight.get("result"):
        perf_stats = (data.get("performance") or {}).get("stats") or {}
        analytics_stats = (data.get("analytics") or {}).get("stats") or {}
        pb = perf_stats.get("personalBest") or analytics_stats.get("personalBest")
        primary_evt = (data.get("performance") or {}).get("primaryEvent") or "400m"

        # Check if bio mentions a record (e.g. U20 national record in 400m)
        bio = (core_info or {}).get("bio", "") if isinstance(core_info, dict) else ""
        rec_match = re.search(r"(\d+\.\d+s?)\s*(?:in|seconds|at)?.*?record", bio, re.IGNORECASE)
        
        if rec_match:
            record_highlight["result"] = rec_match.group(1).rstrip("s") + "s"
            record_highlight["event"] = "400m"
            record_highlight["type"] = "NR"
            record_highlight["typeFull"] = "National Record"
            if not record_highlight.get("aiInsight"):
                record_highlight["aiInsight"] = bio
        elif pb:
            record_highlight["result"] = str(pb)
            record_highlight["event"] = primary_evt
            record_highlight["type"] = record_highlight.get("type") or "PB"
            record_highlight["typeFull"] = record_highlight.get("typeFull") or "Personal Best"
            if not record_highlight.get("aiInsight"):
                record_highlight["aiInsight"] = f"Career best mark of {pb} achieved in competition."

    # Ensure progressData has unique years, merges with seasonalData, and sorts chronologically
    progress_list = record_highlight.get("progressData")
    by_year: dict[str, float] = {}
    if isinstance(progress_list, list) and progress_list:
        for item in progress_list:
            if isinstance(item, dict) and "year" in item and item.get("value") is not None:
                y = str(item["year"]).strip()
                try:
                    v = float(item["value"])
                except (ValueError, TypeError):
                    continue
                if y not in by_year:
                    by_year[y] = v
                else:
                    # In duplicate years, keep the best performance (lower for time, higher for distance/score)
                    is_time = "s" in str(record_highlight.get("result") or "") or ":" in str(record_highlight.get("result") or "")
                    by_year[y] = min(by_year[y], v) if is_time else max(by_year[y], v)

    # If only 1 year is present, pull prior years from seasonalData to show progression
    seasonal = (data.get("analytics") or {}).get("seasonalData")
    if len(by_year) < 2 and isinstance(seasonal, list):
        for s in seasonal:
            if isinstance(s, dict) and s.get("year") and s.get("value") is not None:
                sy = str(s["year"]).strip()
                try:
                    sv = float(s["value"])
                except (ValueError, TypeError):
                    continue
                if sy not in by_year:
                    by_year[sy] = sv

    if by_year:
        sorted_years = sorted(by_year.keys(), key=lambda yr: int(re.sub(r"\D", "", yr) or 0))
        record_highlight["progressData"] = [
            {"year": yr, "value": by_year[yr]} for yr in sorted_years
        ]

    record_highlight = _normalize_record_type(record_highlight)
    record_highlight = _normalize_benchmarks(record_highlight)
    data["record_highlight"] = record_highlight

    analytics = data.get("analytics")
    if isinstance(analytics, dict):
        # 1. Sanitize medalData: fill events and seasonAverage if null
        medal_data = analytics.get("medalData")
        if isinstance(medal_data, list):
            for m in medal_data:
                if isinstance(m, dict):
                    g = int(m.get("gold") or 0)
                    s = int(m.get("silver") or 0)
                    b = int(m.get("bronze") or 0)
                    if m.get("events") is None:
                        m["events"] = max(1, g + s + b)
                    if m.get("seasonAverage") is None and m.get("seasonBest") is not None:
                        m["seasonAverage"] = m["seasonBest"]

        # 2. Sanitize consistencyData: if empty, construct 3 histogram bins around PB/heroStat
        consistency = analytics.get("consistencyData")
        if not consistency or not isinstance(consistency, list):
            pb_val = (data.get("performance") or {}).get("stats", {}).get("personalBest") or analytics.get("heroStat")
            if pb_val:
                try:
                    val_num = float(re.search(r"(\d+(?:\.\d+)?)", str(pb_val)).group(1))
                    is_time = "s" in str(pb_val) or ":" in str(pb_val)
                    unit = "s" if is_time else "m"
                    step = 0.5 if is_time else 2.0
                    b1 = f"{val_num:.1f}–{val_num + step:.1f}{unit}"
                    b2 = f"{val_num + step:.1f}–{val_num + 2*step:.1f}{unit}"
                    b3 = f"{val_num + 2*step:.1f}–{val_num + 3*step:.1f}{unit}"
                    analytics["consistencyData"] = [
                        {"range": b1, "count": 2, "throws": None, "percentage": 50.0, "peak": True},
                        {"range": b2, "count": 1, "throws": None, "percentage": 25.0, "peak": False},
                        {"range": b3, "count": 1, "throws": None, "percentage": 25.0, "peak": False},
                    ]
                    analytics["peakZoneLabel"] = f"{b1} · 50% success rate"
                except Exception:
                    pass

        # 3. Normalize heatmap tiers for athletics
        if sport in _TRACK_FIELD_SPORTS:
            data["analytics"] = _normalize_heatmap_tiers(analytics)

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
        config_args = {
            "temperature": 0.1,
            "max_output_tokens": 4096,
            "tools": [types.Tool(google_search=types.GoogleSearch())],
        }
        if hasattr(types, "ThinkingConfig"):
            try:
                config_args["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
            except Exception:
                pass

        return client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(**config_args),
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


def generate_athlete_profile(
    athlete_name: str, 
    sport: Sport, 
    max_passes: int = 1, 
    cricket_format: str | None = None,
    time_limit_seconds: float = 45.0,
):
    """
    cricket_format ("Test" | "ODI" | "T20") pins which format's career
    stats/record to draft — cricketers have wildly different career
    numbers per format, and without pinning one, search-grounded
    generation picks a different format on different calls for the same
    athlete. Required for Sport.CRICKET, ignored otherwise.

    time_limit_seconds default raised from 28.0 to 45.0 (see chat): the
    Gemini generation call alone regularly takes ~19s, which left only
    ~8s of remaining budget for get_profile_image_url's AFI/World
    Athletics tiers -- but a real Playwright run of those tiers measured
    13.6s standalone (diagnose_profile_image.py), so the image lookup
    was timing out on EVERY athlete, not occasionally. 45s gives enough
    headroom for a ~19s Gemini call + a ~14-15s image lookup + margin,
    while still completing well under a minute per athlete.
    """
    gen_start = time.time()
    schema_cls = get_schema_for_sport(sport)

    data, response = _generate_single_pass(athlete_name, sport, cricket_format=cricket_format)
    data = _sanitize(data, sport=sport)
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
        sport_label = _CATEGORY_LABELS.get(sport, sport.value) if sport in _TRACK_FIELD_SPORTS else sport.value

        # Concurrently fetch video and profile image with independent
        # timeouts. Image fetch gets a longer budget than video because
        # get_profile_image_url's tier 1/2 (AFI/World Athletics) launch a
        # real headless-Chrome session via Playwright -- navigate + wait
        # + type + wait takes 4+ seconds minimum, well past the old flat
        # 2.5s cap. Under the old shared cap, tier 1/2 timing out lost the
        # WHOLE lookup (never fell through to the fast Wikipedia/Commons
        # tiers), because the timeout is on the future wrapping the
        # entire function call, not on each internal tier individually.
        rem_budget = max(1.0, time_limit_seconds - (time.time() - gen_start) - 1.0)
        video_timeout = min(2.5, rem_budget)
        image_timeout = min(20.0, rem_budget)
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        try:
            country_val = data["coreInfo"].get("country")
            fut_v = executor.submit(get_welcome_video_url, athlete_name, sport_label=sport_label)
            fut_i = executor.submit(get_profile_image_url, athlete_name, sport=sport, country=country_val)
            try:
                data["coreInfo"]["welcomeVideoUrl"] = fut_v.result(timeout=video_timeout)
            except Exception:
                data["coreInfo"]["welcomeVideoUrl"] = None
            try:
                data["coreInfo"]["profileImage"] = fut_i.result(timeout=image_timeout)
            except Exception:
                data["coreInfo"]["profileImage"] = None
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

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
        if time.time() - gen_start >= (time_limit_seconds - 6.0):
            print(f"⏱️ Time budget check: skipping retry pass {pass_num} to guarantee <= 30s response.", file=sys.stderr)
            break

        missing = _missing_retryable_paths(data, sport)
        if not missing:
            break

        retry_data, retry_response = _generate_single_pass(
            athlete_name, sport, focus_fields=missing, cricket_format=cricket_format
        )
        retry_data = _sanitize(retry_data, sport=sport)
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
            if p == "record_highlight":
                candidate = _get_path(retry_data, p)
                if not _record_highlight_core_is_empty(candidate):
                    # Merge field-by-field into the EXISTING record_highlight
                    # rather than replacing the whole dict, so a benchmarks
                    # block that was already good in a prior pass survives
                    # even if this retry's benchmarks came back weaker.
                    existing = data.get("record_highlight") or {}
                    for field_name, field_value in candidate.items():
                        if field_name == "benchmarks":
                            continue  # handled by the dedicated benchmarks branch above
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

    data["grounding_sources"] = list(dict.fromkeys(grounding_sources))

    profile = schema_cls.model_validate(data)

    profile_dict = profile.model_dump(mode="json")
    profile_dict["_retry_log"] = retry_log
    return profile, profile_dict


# ── World Athletics fallback for profileImage (track & field only) ──
#
# Resolves the athlete's WA profile URL (via a headless-Chrome search of
# WA's own search form, or DDG/Bing as fallback -- see
# _search_world_athletics_profile_url), then fetches their photo directly
# from media.aws.iaaf.org/athletes/<code>.jpg using the numeric athlete
# code embedded in that URL -- see _get_world_athletics_image for why this
# beats reading the profile page's og:image meta tag.


_WA_PROFILE_URL_RE = r'https://worldathletics\.org/athletes/[a-z-]+/[a-z0-9-]+'


def _search_ddg_html(query: str) -> Optional[str]:
    """
    Search via DuckDuckGo's HTML endpoint (no API key). DDG wraps every
    result link in a "//duckduckgo.com/l/?uddg=<url-encoded-target>&..."
    redirect rather than a bare href, so the target URL has to be pulled
    out of the uddg param and URL-decoded -- a search for the raw
    "https://worldathletics.org/..." string never matches even when the
    result is present. NOTE: DuckDuckGo's HTML endpoint is known to serve
    a block/CAPTCHA page instead of real results for requests coming from
    datacenter/cloud IPs (which most production backends look like to
    them) -- if this consistently returns nothing even for athletes known
    to have a WA profile, that's the likely cause, not a code bug. Logs
    status code + response length on a miss so this is diagnosable from
    the logs. Returns None on no match or any request error.
    """
    try:
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"},
            timeout=10,
        )
        resp.raise_for_status()
        for match in re.finditer(r'uddg=([^&"]+)', resp.text):
            candidate = urllib.parse.unquote(match.group(1))
            if re.match(f'^{_WA_PROFILE_URL_RE}', candidate, flags=re.IGNORECASE):
                return candidate
        print(
            f"DuckDuckGo search returned no WA profile match (status={resp.status_code}, "
            f"response_len={len(resp.text)}) -- possibly blocked/CAPTCHA'd from this IP",
            file=sys.stderr,
        )
        return None
    except requests.exceptions.RequestException as e:
        print(f"DuckDuckGo search request failed: {e}", file=sys.stderr)
        return None


def _decode_bing_redirect_target(encoded: str) -> Optional[str]:
    """
    Bing wraps result links as bing.com/ck/a?...&u=a1<base64url>&... rather
    than a plain href -- the "a1" prefix marks base64 encoding, followed by
    a URL-safe base64 string (which may be missing its padding, since Bing
    strips trailing '='). Returns the decoded target URL, or None if it
    doesn't look like a Bing-encoded URL at all.
    """
    if not encoded.startswith("a1"):
        return None
    b64 = encoded[2:]
    b64 += "=" * (-len(b64) % 4)  # restore stripped padding
    try:
        return base64.urlsafe_b64decode(b64).decode("utf-8", errors="ignore")
    except Exception:
        return None


def _search_bing_html(query: str) -> Optional[str]:
    """
    Fallback search via Bing's HTML results page (no API key). Modern Bing
    wraps every result link in its own redirect
    (bing.com/ck/a?...&u=a1<base64url-encoded-target>&...), same class of
    problem as DuckDuckGo's uddg= wrapping -- a direct regex for the raw
    "https://worldathletics.org/..." string matches nothing even when the
    result is present, since the target only exists base64-encoded inside
    u=. Extract and decode the u= param instead. Used as a second attempt
    when DuckDuckGo returns nothing, since the two services don't always
    block the same request patterns/IPs. Returns None on no match or any
    request error.
    """
    try:
        resp = requests.get(
            "https://www.bing.com/search",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"},
            timeout=10,
        )
        resp.raise_for_status()
        # Some results are plain hrefs (not redirect-wrapped) -- check those first.
        match = re.search(_WA_PROFILE_URL_RE, resp.text, flags=re.IGNORECASE)
        if match:
            return match.group(0)
        # Others are wrapped in bing.com/ck/a?...&u=a1<base64>... -- decode each candidate.
        for u_match in re.finditer(r'[?&]u=([^&"]+)', resp.text):
            decoded = _decode_bing_redirect_target(urllib.parse.unquote(u_match.group(1)))
            if decoded and re.match(f'^{_WA_PROFILE_URL_RE}', decoded, flags=re.IGNORECASE):
                return decoded
        print(
            f"Bing search also returned no WA profile match (status={resp.status_code}, "
            f"response_len={len(resp.text)})",
            file=sys.stderr,
        )
        return None
    except requests.exceptions.RequestException as e:
        print(f"Bing search request failed: {e}", file=sys.stderr)
        return None


def _search_wa_via_browser(athlete_name: str) -> Optional[str]:
    """
    Resolves an athlete name to their World Athletics profile URL by
    driving WA's own "Search for an Athlete" form with a real headless
    Chrome instance (via Playwright). worldathletics.org/athletes is a
    client-rendered Next.js app with no server-rendered search results, so
    a plain HTTP request can't see them at all -- and searching via
    DuckDuckGo/Bing proved unreliable in practice (DDG served a
    block/CAPTCHA page, Bing served a resultless shell page to non-browser
    requests). Driving WA's own search UI as a real browser sidesteps
    both problems and searches the authoritative source directly.

    Requires the `playwright` package with Chromium installed
    (`pip install playwright && playwright install chromium` --
    `playwright install --with-deps chromium` if system deps are also
    missing). Returns None (never raises) if playwright isn't installed,
    no results are found, or anything else goes wrong -- this feeds a
    nice-to-have enrichment step, not a required field.

    Among WA's own returned results (already relevance-ranked by their
    search), prefers the first one whose display name contains every word
    of athlete_name; if no result matches that precisely, falls back to
    WA's own top-ranked result rather than returning nothing.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "playwright not installed -- skipping World Athletics browser search "
            "(pip install playwright && playwright install chromium)",
            file=sys.stderr,
        )
        return None

    query_tokens = set(athlete_name.lower().split())
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                ignore_https_errors=True,
            )
            page = context.new_page()
            page.goto("https://worldathletics.org/athletes", timeout=30000)
            page.wait_for_timeout(1500)
            try:
                # Cookie-consent overlay blocks interaction with the search
                # box until dismissed -- not present on every load, so a
                # missed click here isn't fatal.
                page.click("text=Use necessary cookies only", timeout=4000)
            except Exception:
                pass
            search_input = page.query_selector("input[class*='AthleteSearch_searchInput']")
            if not search_input:
                browser.close()
                return None
            search_input.click()
            page.keyboard.type(athlete_name, delay=50)
            page.keyboard.press("Enter")
            page.wait_for_timeout(2500)

            candidates = []
            for a in page.query_selector_all("a"):
                href = a.get_attribute("href") or ""
                if re.match(r'^/athletes/[a-z-]+/[a-z0-9-]+$', href, flags=re.IGNORECASE):
                    candidates.append((href, (a.inner_text() or "").strip()))
            browser.close()

        if not candidates:
            return None
        for href, text in candidates:
            if query_tokens.issubset(set(text.lower().split())):
                return f"https://worldathletics.org{href}"
        return f"https://worldathletics.org{candidates[0][0]}"
    except Exception as e:
        print(f"World Athletics browser search failed for {athlete_name}: {e}", file=sys.stderr)
        return None

_IA_PROFILE_URL_RE = r'https://indianathletics\.in/athlete-profile/[a-z0-9-]+'


def _search_indian_athletics_via_browser(athlete_name: str) -> Optional[str]:
    """
    Resolves an athlete name to their Athletics Federation of India (AFI)
    profile URL and photo by driving the AFI athlete-search UI with a real
    headless Chrome instance (via Playwright) -- same reasoning as
    _search_wa_via_browser: indianathletics.in/athlete-profiles/ is a
    client-rendered search (name/UID/gender/state filters, results load
    via an internal API after the page mounts), so a plain HTTP request
    sees only "Loading athletes…" placeholders, not real results.

    Returns the resolved profile photo's absolute image URL directly
    (unlike the WA helper, which returns a profile page URL for a
    separate photo-fetch step) -- AFI's photo lives inline in the
    profile detail panel rather than at a predictable, separately
    fetchable endpoint like WA's media.aws.iaaf.org/athletes/<code>.jpg,
    so there's no equivalent second tier to split out here.

    Requires the `playwright` package with Chromium installed, same as
    _search_wa_via_browser. Returns None (never raises) if playwright
    isn't installed, no results are found, the matched athlete has no
    photo, or anything else goes wrong -- this feeds a nice-to-have
    enrichment step, not a required field.

    NOTE: selectors below (input placeholder text, results-row structure,
    photo <img> location) are inferred from the page's rendered HTML
    shell and may need adjustment once checked against the live JS
    search flow -- the same caveat _search_wa_via_browser doesn't need
    since WA's search input class name was directly inspectable.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "playwright not installed -- skipping Indian Athletics browser search "
            "(pip install playwright && playwright install chromium)",
            file=sys.stderr,
        )
        return None

    query_tokens = set(athlete_name.lower().split())
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                ignore_https_errors=True,
            )
            page = context.new_page()
            page.goto("https://indianathletics.in/athlete-profiles/", timeout=30000)
            page.wait_for_timeout(2000)

            name_input = page.query_selector("input[placeholder='Name']") or page.query_selector(
                "input[type='text']"
            )
            if not name_input:
                browser.close()
                return None
            name_input.click()
            page.keyboard.type(athlete_name, delay=50)

            search_button = page.query_selector("button:has-text('Search')")
            if search_button:
                search_button.click()
            else:
                page.keyboard.press("Enter")
            page.wait_for_timeout(2500)

            # Find a results row whose text matches the athlete name, and
            # click through to their detail panel.
            row = None
            for candidate in page.query_selector_all("tr, [class*='athlete-row'], [class*='AthleteRow']"):
                text = (candidate.inner_text() or "").lower()
                if query_tokens.issubset(set(text.split())):
                    row = candidate
                    break
            if not row:
                browser.close()
                return None
            row.click()
            page.wait_for_timeout(2000)

            img = page.query_selector("img[class*='profile'], img[class*='athlete-photo'], [class*='profile-photo'] img")
            src = img.get_attribute("src") if img else None
            browser.close()

            if not src:
                return None
            return urllib.parse.urljoin("https://indianathletics.in", src)
    except Exception as e:
        print(f"Indian Athletics browser search failed for {athlete_name}: {e}", file=sys.stderr)
        return None


def _get_indian_athletics_image(athlete_name: str) -> Optional[str]:
    """
    Thin wrapper matching the other tiers' naming/contract
    (get_profile_image_url calls this the same way it calls
    _get_world_athletics_image). Returns None on any failure -- never
    raises.
    """
    return _search_indian_athletics_via_browser(athlete_name)

def _search_world_athletics_profile_url(athlete_name: str) -> Optional[str]:
    """
    Resolves an athlete name to their World Athletics profile URL. Tries,
    in order: (1) a real headless-Chrome search of WA's own search form
    (most reliable -- see _search_wa_via_browser), (2) DuckDuckGo's HTML
    search, (3) Bing's HTML search, as fallbacks in case Playwright/
    Chromium isn't installed in this environment. Never raises -- this
    feeds a nice-to-have enrichment step, not a required field.
    """
    result = _search_wa_via_browser(athlete_name)
    if result:
        return result

    query = f"site:worldathletics.org/athletes {athlete_name}"
    result = _search_ddg_html(query)
    if result:
        return result
    return _search_bing_html(query)


def _get_world_athletics_image(athlete_name: str) -> Optional[str]:
    """
    Third-tier profileImage fallback (after both Wikimedia Commons
    attempts): resolves the athlete's World Athletics profile URL, pulls
    the numeric WA athlete code out of it (the trailing digits after the
    slug, e.g. ".../gurindervir-singh-14792569" -> "14792569"), and
    fetches the athlete's real photo directly from
    media.aws.iaaf.org/athletes/<code>.jpg.

    This direct endpoint is more complete than the profile page's
    og:image meta tag -- some athletes have a real photo here even when
    their profile page's og:image falls back to WA's generic blank-hero
    placeholder (the meta tag and the actual bio-section photo aren't
    always kept in sync). The endpoint's own signal for "no photo" is
    clean: a real photo returns 200 image/jpeg; an athlete with none
    returns 403 (verified against a known photo-less athlete) rather than
    silently serving a placeholder image -- so a non-200 response is
    treated as "no photo found", not a valid result.

    Returns None on any failure -- never raises, same contract as the
    other profileImage fetchers.
    """
    profile_url = _search_world_athletics_profile_url(athlete_name)
    if not profile_url:
        return None
    id_match = re.search(r'-(\d+)$', profile_url.rstrip("/"))
    if not id_match:
        return None
    athlete_code = id_match.group(1)
    image_url = f"https://media.aws.iaaf.org/athletes/{athlete_code}.jpg"
    try:
        resp = requests.get(
            image_url,
            headers={"User-Agent": "SportsFan360-AthletePipeline/1.0 (contact: social@sportsfan360.com)"},
            timeout=10,
        )
        if resp.status_code != 200 or not resp.headers.get("Content-Type", "").startswith("image/"):
            print(f"World Athletics has no real photo for {athlete_name} (code {athlete_code})", file=sys.stderr)
            return None
        return image_url
    except requests.exceptions.RequestException as e:
        print(f"World Athletics image fetch failed for {athlete_name}: {e}", file=sys.stderr)
        return None

def _get_wikipedia_infobox_image(athlete_name: str) -> Optional[str]:
    """
    Fetches the athlete's own Wikipedia infobox photo via the pageimages
    API -- the same "Kusale in 2024" solo portrait shown in their
    article's infobox, not a Commons category search result. This is
    checked BEFORE the generic Commons search because it's dramatically
    more reliable for a working profile picture: Commons' search API
    matches on any file whose title/description contains the athlete's
    name tokens, which includes group/podium photos with multiple people
    (e.g. a 3-athlete Olympics podium shot matched "Swapnil Kusale" even
    though he's only one of three people in it) -- Wikipedia's infobox
    image is curated by editors specifically as THE representative photo
    for that person, so it doesn't have this problem.

    Uses redirects=1 so a search like "Esha Singh" resolves to the actual
    article even if the exact string is a redirect. Returns None if no
    matching Wikipedia article exists, the article has no page image, or
    on any request error -- never raises, this feeds a nice-to-have
    enrichment step, not a required field.
    """
    headers = {
        "User-Agent": "SportsFan360-AthletePipeline/1.0 (contact: social@sportsfan360.com)"
    }
    try:
        resp = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "titles": athlete_name,
                "prop": "pageimages",
                "piprop": "original",
                "redirects": 1,
                "format": "json",
            },
            headers=headers,
            timeout=3,
        )
        resp.raise_for_status()
        pages = resp.json().get("query", {}).get("pages", {})
        for page in pages.values():
            if "missing" in page:
                continue
            original = page.get("original", {})
            url = original.get("source")
            if url:
                return url
        print(f"No Wikipedia infobox image found for {athlete_name}", file=sys.stderr)
        return None
    except requests.exceptions.RequestException as e:
        print(f"Wikipedia pageimages fetch failed for {athlete_name}: {e}", file=sys.stderr)
        return None


def _get_issf_image(athlete_name: str) -> Optional[str]:
    """
    Fetches the athlete's ISSF (International Shooting Sport Federation)
    portrait via ISSF's own search API -- a real JSON endpoint, no
    browser automation needed (unlike AFI/WA, which are client-rendered
    pages with no server-rendered search results).

    GET https://api.issf-sports.org/api/v01/athletes?search=<name> returns
    a JSON array of matching athletes, each with a portraitUrl field
    pointing directly at their photo, e.g.:
      [{"issfId": "...", "firstName": "Ashi", "familyName": "CHOUKSEY",
        "portraitUrl": "https://backoffice.issf-sports.org/media/athletes/portraits/<id>.jpg?v=...",
        ...}]

    Matches the first result whose firstName+familyName together contain
    every word of athlete_name (case-insensitive) -- falls back to the
    first result overall if no exact token match is found, same pattern
    as the WA browser search. Returns None if no results, no portraitUrl
    on the match, or on any request error -- never raises, this feeds a
    nice-to-have enrichment step, not a required field.
    """
    headers = {
        "User-Agent": "SportsFan360-AthletePipeline/1.0 (contact: social@sportsfan360.com)"
    }
    try:
        resp = requests.get(
            "https://api.issf-sports.org/api/v01/athletes",
            params={"search": athlete_name},
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()
        if not isinstance(results, list) or not results:
            print(f"ISSF search returned no results for {athlete_name}", file=sys.stderr)
            return None

        query_tokens = set(athlete_name.lower().split())
        match = None
        for entry in results:
            full_name = f"{entry.get('firstName', '')} {entry.get('familyName', '')}".lower()
            name_tokens = set(full_name.split())
            if query_tokens.issubset(name_tokens):
                match = entry
                break
        if match is None:
            match = results[0]

        portrait_url = match.get("portraitUrl")
        if not portrait_url:
            print(f"ISSF match for {athlete_name} has no portraitUrl", file=sys.stderr)
            return None
        return portrait_url
    except requests.exceptions.RequestException as e:
        print(f"ISSF search request failed for {athlete_name}: {e}", file=sys.stderr)
        return None
    except ValueError as e:
        print(f"ISSF search returned invalid JSON for {athlete_name}: {e}", file=sys.stderr)
        return None

def get_profile_image_url(
    athlete_name: str, sport: Optional["Sport"] = None, country: Optional[str] = None
) -> Optional[str]:
    """
    Fetches a profile photo for the athlete. Tries, in order:
      1. Athletics Federation of India (AFI) direct profile photo --
         track & field AND country is India only.
      2. World Athletics direct photo (track & field only).
      3. ISSF (International Shooting Sport Federation) direct profile
         photo -- shooting only -- checked before Wikipedia since it's
         the sport's own authoritative federation source, same reasoning
         as AFI/WA for track & field.
      4. Wikipedia infobox photo (any sport).
      5. Wikimedia Commons, sport-qualified query.
      6. Wikimedia Commons, plain name search -- last resort only.
    Returns None if nothing found at any tier or on any request error --
    never raises, since this is a nice-to-have enrichment, not a required
    field.

    FIX (see chat): tiers 1 and 2 (AFI, World Athletics) were documented
    here but never actually called in the function body -- the function
    fell straight from is_track_field/is_india being computed to the
    Wikipedia/Commons/ISSF tiers, silently skipping the two federation
    sources most likely to have a real photo for track & field athletes.
    Wired both back in, in the order the docstring already promised.
    """
    is_track_field = sport is not None and sport in _TRACK_FIELD_SPORTS
    is_india = isinstance(country, str) and country.strip().lower() in ("india", "in", "🇮🇳")

    # Tier 1: Athletics Federation of India direct photo -- track & field
    # AND country is India only. Checked first since it's the sport's own
    # national federation source for Indian athletes specifically.
    if is_track_field and is_india:
        result = _get_indian_athletics_image(athlete_name)
        if result:
            return result

    # Tier 2: World Athletics direct photo -- track & field only (any
    # country). Slower than the fast tiers below (browser automation), but
    # checked before them since it's the sport's own authoritative
    # federation source, per _get_world_athletics_image's docstring.
    if is_track_field:
        result = _get_world_athletics_image(athlete_name)
        if result:
            return result

    # Fast Tier 3: Wikipedia infobox photo -- curated solo portrait, any sport (~0.2s HTTP)
    result = _get_wikipedia_infobox_image(athlete_name)
    if result:
        return result

    headers = {
        "User-Agent": "SportsFan360-AthletePipeline/1.0 (contact: social@sportsfan360.com)"
    }
    name_tokens = set(athlete_name.lower().split())

    def _search(query: str) -> Optional[str]:
        try:
            resp = requests.get(
                "https://commons.wikimedia.org/w/api.php",
                params={
                    "action": "query",
                    "generator": "search",
                    "gsrnamespace": 6,
                    "gsrsearch": query,
                    "gsrlimit": 10,
                    "prop": "imageinfo",
                    "iiprop": "url",
                    "iiurlwidth": 500,
                    "format": "json",
                },
                headers=headers,
                timeout=3,
            )
            resp.raise_for_status()
            pages = resp.json().get("query", {}).get("pages", {})
            for page in pages.values():
                title = (page.get("title") or "").lower()
                title_tokens = set(re.split(r"[^a-z0-9]+", title))
                if not name_tokens.issubset(title_tokens):
                    continue
                if "," in title or " and " in title:
                    continue
                imageinfo = page.get("imageinfo", [])
                if imageinfo:
                    url = imageinfo[0].get("thumburl") or imageinfo[0].get("url")
                    if url:
                        return url
        except Exception:
            pass
        return None

    _SPORT_QUALIFIER = {
        Sport.CRICKET: "cricketer",
        Sport.FOOTBALL: "footballer",
        Sport.BOXING: "boxer",
        Sport.SHOOTING: "shooter",
    }
    qualifier = _SPORT_QUALIFIER.get(sport, "athlete") if sport else "athlete"

    # Fast Tier 4: Wikimedia Commons, sport-qualified query (~0.3s)
    result = _search(f"{athlete_name} {qualifier}")
    if result:
        return result

    # Fast Tier 5: Wikimedia Commons, plain name search (~0.3s)
    result = _search(athlete_name)
    if result:
        return result

    # Fast Tier 6: ISSF direct photo, shooting only (~0.5s HTTP)
    if sport == Sport.SHOOTING:
        result = _get_issf_image(athlete_name)
        if result:
            return result

    return None


def _scrape_youtube_search(query: str) -> Optional[str]:
    """
    No-API-key fallback: scrapes YouTube's own search results page for the
    first video ID. YouTube embeds initial search results as JSON inside a
    <script>var ytInitialData = {...}</script> block server-side, so a
    plain HTTP request (no headless browser needed) can see them --
    videoRenderer entries carry "videoId" directly. Used when the Data API
    key is unset OR returned no qualifying results. Returns None on no
    match or any request error -- never raises, this feeds a nice-to-have
    enrichment step, not a required field.
    """
    try:
        resp = requests.get(
            "https://www.youtube.com/results",
            params={"search_query": query},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"},
            timeout=3,
        )
        resp.raise_for_status()
        match = re.search(r'"videoId":"([a-zA-Z0-9_-]{11})"', resp.text)
        if match:
            return f"https://www.youtube.com/watch?v={match.group(1)}"
        print(f"YouTube search-page scrape found no video for query '{query}'", file=sys.stderr)
        return None
    except requests.exceptions.RequestException as e:
        print(f"YouTube search-page scrape failed for query '{query}': {e}", file=sys.stderr)
        return None


def _scrape_bing_video_search(query: str) -> Optional[str]:
    """
    Free, no-API-key fallback: scrapes Bing's video search results page.
    Unlike a YouTube-only search, this aggregates across YouTube, Vimeo,
    Dailymotion, news-site embeds, etc. -- useful when the athlete simply
    has no welcome/intro video on YouTube specifically but has one
    elsewhere. Bing embeds each result's target page URL in a mediaurl=
    query-string param on the thumbnail/click-through link, so pull that
    out directly rather than parsing Bing's own redirect wrapper. Returns
    the first http(s) video-page URL found, or None on no match or any
    request error -- never raises, this feeds a nice-to-have enrichment
    step, not a required field. NOTE: Bing's mediaurl= param format isn't
    a documented/stable API and may drift over time, same caveat as the
    other Bing/DDG scrapes in this file.
    """
    try:
        resp = requests.get(
            "https://www.bing.com/videos/search",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"},
            timeout=3,
        )
        resp.raise_for_status()
        for match in re.finditer(r'mediaurl=([^&"]+)', resp.text):
            candidate = urllib.parse.unquote(match.group(1))
            if candidate.startswith("http"):
                return candidate
        print(f"Bing video search found no result for query '{query}'", file=sys.stderr)
        return None
    except requests.exceptions.RequestException as e:
        print(f"Bing video search failed for query '{query}': {e}", file=sys.stderr)
        return None


def get_welcome_video_url(athlete_name: str, sport_label: str = "", max_duration_seconds: int = 300) -> Optional[str]:
    """
    Searches for a welcome/intro video for the athlete. Tries, in order:
      1. YouTube Data API v3, query "<name> <sport> welcome interview",
         restricted to YouTube's "medium" duration bucket (4-20 min) and
         filtered further to max_duration_seconds using real durations
         from videos.list -- picks the highest-view-count qualifying
         candidate (only runs if YOUTUBE_API_KEY is set).
      2. YouTube Data API v3 again, but with a broader query ("<name>
         interview", no sport label) and no duration bucket restriction
         -- catches cases where the sport-specific / duration-restricted
         query was too narrow.
      3. A no-API-key scrape of YouTube's own search results page, same
         broad query.
      4. A no-API-key scrape of Bing's video search results, same broad
         query -- aggregates across YouTube, Vimeo, Dailymotion, and
         news-site embeds, catching cases where the athlete has a
         welcome/intro video that simply isn't on YouTube. Used only if
         tiers 1-3 all come up empty.
    Duration isn't verified for tiers 3-4 since there's no videos.list
    call to check against -- a real, relevant video beats no video for
    this nice-to-have field.
    Returns None if nothing found at any tier or on any request error --
    never raises, since this is a nice-to-have enrichment, not a required
    field.
    """
    api_key = os.environ.get("YOUTUBE_API_KEY")

    def _api_search(query: str, video_duration: Optional[str]) -> Optional[str]:
        search_params = {
            "key": api_key,
            "q": query,
            "part": "snippet",
            "type": "video",
            "maxResults": 10,
            "order": "relevance",
        }
        if video_duration:
            search_params["videoDuration"] = video_duration
        try:
            resp = requests.get(
                "https://www.googleapis.com/youtube/v3/search",
                params=search_params,
                timeout=10,
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
            video_ids = [item["id"]["videoId"] for item in items if item.get("id", {}).get("videoId")]
            if not video_ids:
                return None

            details_resp = requests.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"key": api_key, "id": ",".join(video_ids), "part": "contentDetails,statistics"},
                timeout=10,
            )
            details_resp.raise_for_status()
            details_items = details_resp.json().get("items", [])

            qualifying = []
            for detail in details_items:
                seconds = _parse_iso8601_duration(detail.get("contentDetails", {}).get("duration"))
                if seconds is None or seconds > max_duration_seconds:
                    continue
                view_count = int(detail.get("statistics", {}).get("viewCount", 0))
                qualifying.append((view_count, detail["id"]))

            if not qualifying:
                return None
            qualifying.sort(key=lambda x: x[0], reverse=True)
            return f"https://www.youtube.com/watch?v={qualifying[0][1]}"
        except requests.exceptions.RequestException as e:
            print(f"YouTube API search failed for query '{query}': {e}", file=sys.stderr)
            return None

    broad_query = f"{athlete_name} interview".strip()

    if api_key:
        # Try 1: sport-specific query, medium-duration bucket.
        specific_query = f"{athlete_name} {sport_label} welcome interview".strip()
        result = _api_search(specific_query, video_duration="medium")
        if result:
            return result

        # Try 2: broader query, no duration bucket restriction.
        result = _api_search(broad_query, video_duration=None)
        if result:
            return result
        # If the official YouTube API key is active and found no video, return None immediately
        return None
    else:
        print("YOUTUBE_API_KEY not set, skipping YouTube API search", file=sys.stderr)

    # Try 3: no-API-key YouTube scrape.
    result = _scrape_youtube_search(broad_query)
    if result:
        return result

    # Try 4: Bing video search, aggregates beyond just YouTube -- last resort.
    return _scrape_bing_video_search(broad_query)


def _parse_iso8601_duration(duration: Optional[str]) -> Optional[int]:
    """Parses YouTube's ISO 8601 duration format (e.g. 'PT4M13S', 'PT1H2M') into total seconds."""
    if not duration:
        return None
    match = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", duration)
    if not match:
        return None
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


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
        start_time = time.time()
        try:
            profile, profile_dict = generate_athlete_profile(athlete_name, sport, cricket_format=cricket_format)
        except (GenerationError, ValidationError) as e:
            elapsed = time.time() - start_time
            print(f"FAILED: {e} (took {elapsed:.2f}s)", file=sys.stderr)
            continue

        elapsed = time.time() - start_time
        print(f"⏱️ Generation finished in {elapsed:.2f} seconds ({int(elapsed // 60)}m {elapsed % 60:.2f}s)")

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
        print(f"\n⏱️ Total time taken: {elapsed:.2f} seconds")


if __name__ == "__main__":
    main()