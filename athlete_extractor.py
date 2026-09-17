"""
Step 5 (now the only real extraction step) — Gemini, with Google Search
grounding enabled, drafts a full structured athlete profile directly —
no raw licensed-source payload to hand it anymore, so the prompt asks it
to search and extract in one call, same as generate_athlete_content_llm.py.

Malformed output is coerced (not just rejected) via the generic
schema-driven coercion engine ported from that side-track script, then
handed to Pydantic for final validation in athlete_pipeline.py Step 6.
"""

import json
import os
import re
from typing import Any, Optional

from google import genai
from google.genai import types

from athlete_schemas import Sport, get_schema_for_sport

# ── Gemini client setup (unchanged from before) ─────────────────────────────
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

MAX_OUTPUT_TOKENS = 16384  # raised from 8192 after Lovlina Borgohain truncation


class ExtractionError(Exception):
    pass


# ── Per-sport field applicability ───────────────────────────────────────────
# Which fields structurally don't apply for a sport (coerced to "N/A", not
# null) vs. which to explicitly push Gemini to search harder for rather than
# accept null.
#
# FIX: this used to key off Sport.BOXING / Sport.WRESTLING / Sport.SHOOTING,
# none of which exist on the current Sport enum anymore (athlete_schemas.py
# dropped them when track & field was split into 6 sub-disciplines and
# wrestling/boxing/shooting/archery/weightlifting were removed). Referencing
# a nonexistent enum member raises AttributeError at import time, which took
# this whole module down. Rules below are remapped onto the sports that
# still exist; the old boxing/wrestling/shooting judgment calls (no
# personal_best-style fields, search harder for ranking/medals/coach) are
# preserved under Sport.KABADDI/Sport.BASKETBALL/Sport.FOOTBALL only where
# they still make sense — reconfirm these against real drafts and adjust as
# needed, this is a best-effort remap, not a verified-correct one.
FIELD_APPLICABILITY: dict[Sport, dict[str, list[str]]] = {
    Sport.CRICKET: {
        "na_fields": ["personal_best", "personal_best_date", "season_best", "performance_trend"],
        "search_harder": ["current_team_or_federation", "career_highlights"],
    },
    Sport.HOCKEY: {
        "na_fields": ["personal_best", "personal_best_date", "season_best", "performance_trend"],
        "search_harder": ["current_team_or_federation", "medal_cabinet"],
    },
    Sport.KABADDI: {
        "na_fields": ["personal_best", "personal_best_date", "season_best", "performance_trend"],
        "search_harder": ["current_ranking", "medal_cabinet", "coach"],
    },
    Sport.BASKETBALL: {
        "na_fields": ["personal_best", "personal_best_date", "season_best", "performance_trend"],
        "search_harder": ["current_team_or_federation", "medal_cabinet"],
    },
    Sport.FOOTBALL: {
        "na_fields": ["personal_best", "personal_best_date", "season_best", "performance_trend"],
        "search_harder": ["current_team_or_federation", "medal_cabinet"],
    },
}


def _sport_specific_guidance(sport: Sport) -> str:
    rules = FIELD_APPLICABILITY.get(sport)
    if not rules:
        return ""
    lines = []
    if rules.get("na_fields"):
        lines.append(
            f"These fields do not apply to {sport.value} — return the string "
            f"\"N/A\" for each, not null: {', '.join(rules['na_fields'])}."
        )
    if rules.get("search_harder"):
        lines.append(
            f"These fields ARE expected to exist for {sport.value} — search "
            f"specifically for them rather than returning null if not found "
            f"on first pass: {', '.join(rules['search_harder'])}."
        )
    return "\n    ".join(lines)


def _build_prompt(sport: Sport, athlete_id: str, athlete_name: str) -> str:
    schema_cls = get_schema_for_sport(sport)
    field_names = [
        name for name in schema_cls.model_fields.keys()
        if name not in ("source", "athlete_id", "sport")
    ]

    return f"""
    You are drafting a structured athlete profile for SportsFan360, a sports
    fan engagement platform. Use Google Search to find current, factual
    information about this athlete. Extract ONLY information you can find —
    do not infer, guess, or fabricate. Leave a field null if genuinely
    unfindable (unless a rule below says otherwise for this sport).

    Sport: {sport.value}
    Athlete ID: {athlete_id}
    Athlete name: {athlete_name}

    Fields to extract (JSON keys, use exactly these names): {field_names}

    List length limits: career_highlights ≤6, medal_cabinet ≤8,
    performance_trend ≤6 years.

    {_sport_specific_guidance(sport)}

    Respond with ONLY a single JSON object containing these fields, plus a
    "grounding_sources" array of the URLs you actually used. No prose, no
    markdown fences, no commentary.
    """


def _call_gemini(prompt: str) -> str:
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            tools=[types.Tool(google_search=types.GoogleSearch())],
        ),
    )
    return (response.text or "").strip()


def _parse_json_block(raw: str) -> dict:
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start == -1 or end == 0:
        raise ExtractionError(f"Gemini response did not contain valid JSON: {raw[:300]}")
    return json.loads(raw[start:end])


# ── Domain-specific sanitize layer (runs BEFORE generic coercion) ──────────
_COACH_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _sanitize(data: dict, sport: Sport) -> dict:
    # Fold medal_cabinet entries with a stray "event" key into competition,
    # before generic coercion drops unknown keys.
    medal_cabinet = data.get("medal_cabinet")
    if isinstance(medal_cabinet, list):
        for entry in medal_cabinet:
            if isinstance(entry, dict) and "event" in entry:
                event_val = entry.pop("event")
                if event_val and not entry.get("competition"):
                    entry["competition"] = event_val

    # Strip parenthetical annotations from coach name, e.g. "(deceased)".
    qf = data.get("quick_facts")
    if isinstance(qf, dict) and isinstance(qf.get("coach"), str):
        qf["coach"] = _COACH_PAREN_RE.sub("", qf["coach"]).strip()
    if isinstance(data.get("coach"), str):
        data["coach"] = _COACH_PAREN_RE.sub("", data["coach"]).strip()

    return data


# ── Generic schema-driven coercion engine ───────────────────────────────────
def _time_or_number_to_seconds(mark: str) -> float | None:
    parts = mark.split(":")
    try:
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
        if len(parts) == 2:
            m, s = parts
            return int(m) * 60 + float(s)
        return float(mark)
    except (TypeError, ValueError):
        return None


def _generic_coerce_scalar(value: Any, expected_type: type, na_ok: bool) -> Any:
    """Best-effort coercion of a single value toward expected_type."""
    if value is None:
        return "N/A" if na_ok else None

    origin = getattr(expected_type, "__origin__", None)

    # list-expected but got a scalar/string "N/A" -> []
    if origin in (list,) or expected_type is list:
        if isinstance(value, str):
            return []
        if isinstance(value, dict):
            # dict -> best single value, wrapped as a 1-item list if needed
            return list(value.values())
        return value

    # str-expected
    if expected_type is str or expected_type == Optional[str]:
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, dict):
            # nested object -> flat string, e.g. {"snatch": .., "clean_jerk": ..}
            return ", ".join(f"{k}: {v}" for k, v in value.items())
        if isinstance(value, list):
            return ", ".join(str(v) for v in value) if value else ("N/A" if na_ok else None)
        return value

    # int-expected
    if expected_type is int or expected_type == Optional[int]:
        if isinstance(value, str):
            digits = re.sub(r"[^\d]", "", value)
            return int(digits) if digits else None
        if isinstance(value, dict):
            # dict of per-event rankings -> best (lowest) numeric value
            nums = [v for v in value.values() if isinstance(v, (int, float))]
            return int(min(nums)) if nums else None
        if isinstance(value, list):
            return len(value)  # e.g. season_stats.events list -> count
        return value

    return value


def _coerce_to_model(data: dict, schema_cls: type, na_fields: list[str] | None = None) -> dict:
    """
    Walks schema_cls.model_fields and coerces each present value toward its
    expected type. Required fields left None after coercion fall back to
    "N/A" rather than failing validation downstream.
    """
    na_fields = set(na_fields or [])
    coerced: dict[str, Any] = {}

    for field_name, field_info in schema_cls.model_fields.items():
        if field_name not in data:
            continue
        value = data[field_name]
        expected_type = field_info.annotation
        na_ok = field_name in na_fields
        coerced[field_name] = _generic_coerce_scalar(value, expected_type, na_ok)

        if coerced[field_name] is None and field_info.is_required():
            coerced[field_name] = "N/A"

    # carry over any keys the loop didn't touch (nested models etc. handled
    # by Pydantic itself on final validation)
    for k, v in data.items():
        coerced.setdefault(k, v)

    return coerced


def extract_athlete_profile(sport: Sport, athlete_id: str, athlete_name: str) -> dict:
    """
    Returns a coerced (but not yet Pydantic-validated) dict. Final
    validation happens in athlete_pipeline.py Step 6.
    """
    schema_cls = get_schema_for_sport(sport)
    prompt = _build_prompt(sport, athlete_id, athlete_name)
    na_fields = FIELD_APPLICABILITY.get(sport, {}).get("na_fields", [])

    for attempt in (1, 2):  # one retry on truncation/parse failure
        try:
            raw_text = _call_gemini(prompt)
            parsed = _parse_json_block(raw_text)
            break
        except (ExtractionError, json.JSONDecodeError) as e:
            if attempt == 2:
                raise ExtractionError(f"Gemini extraction failed after retry: {e}") from e
            continue
        except Exception as e:
            raise ExtractionError(f"Gemini extraction call failed: {e}") from e

    sanitized = _sanitize(parsed, sport)
    return _coerce_to_model(sanitized, schema_cls, na_fields=na_fields)