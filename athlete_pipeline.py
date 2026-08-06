"""
Athlete Data Automation pipeline.

Steps (post-redesign — no licensed-source fetch, no fingerprint skip):
1. Trigger: NEW_ATHLETE_ONBOARDING (add-athlete form) |
   SCHEDULED_RECHECK (weekly EventBridge sweep) |
   MANUAL_RECHECK (admin "recheck now" button)
2. Gemini (with Google Search grounding) drafts + coerces the profile
3. Pydantic schema validation on the coerced dict
4. Valid draft -> review queue (Firestore, admin screen)
5. Human approves/edits/rejects (handled by the admin API, not here)
6. Only on approval -> live Firestore record (firebase_store.resolve_review_draft)
"""

from pydantic import ValidationError

import firebase_store
from athlete_extractor import ExtractionError, extract_athlete_profile
from athlete_schemas import ReviewDraft, Sport, SourceRef, TriggerType, get_schema_for_sport


class PipelineResult:
    def __init__(self, status: str, message: str, draft_id: str = None):
        self.status = status  # "drafted" | "error"
        self.message = message
        self.draft_id = draft_id

    def to_dict(self):
        return {"status": self.status, "message": self.message, "draft_id": self.draft_id}


def run_athlete_pipeline(
    athlete_id: str,
    sport: str,
    athlete_name: str,
    trigger_type: TriggerType = TriggerType.SCHEDULED_RECHECK,
) -> PipelineResult:
    """
    Main entry point.
    - add-athlete form -> trigger_type=NEW_ATHLETE_ONBOARDING
    - weekly EventBridge sweep -> trigger_type=SCHEDULED_RECHECK
    - admin "recheck now" button -> trigger_type=MANUAL_RECHECK

    athlete_name replaces the old athlete_source_id — Gemini's search
    grounding looks the athlete up by name, there's no external numeric ID
    to resolve anymore.
    """
    try:
        sport_enum = Sport(sport)
    except ValueError:
        return PipelineResult("error", f"Unknown sport '{sport}'")

    # ── Gemini extraction (search grounding + coercion happen inside) ──────
    try:
        extracted = extract_athlete_profile(sport_enum, athlete_id, athlete_name)
    except ExtractionError as e:
        return PipelineResult("error", f"Extraction failed: {e}")

    # ── Pydantic schema validation ──────────────────────────────────────────
    schema_cls = get_schema_for_sport(sport_enum)
    source_ref = SourceRef(
        fetched_at=_now_iso_placeholder(),  # set by whichever util you already use
    )
    try:
        validated = schema_cls(
            athlete_id=athlete_id,
            sport=sport_enum,
            source=source_ref,
            **{k: v for k, v in extracted.items() if k not in ("athlete_id", "sport", "source") and v is not None},
        )
    except ValidationError as e:
        return PipelineResult("error", f"Schema validation failed — draft rejected before review: {e}")

    # ── Write draft to review queue (NOT the live record) ──────────────────
    is_new = trigger_type == TriggerType.NEW_ATHLETE_ONBOARDING
    current_data = None if is_new else firebase_store.get_live_athlete(athlete_id)

    draft = ReviewDraft(
        athlete_id=athlete_id,
        sport=sport_enum,
        trigger_type=trigger_type,
        trigger_reason=trigger_type.value.replace("_", " ").title(),
        proposed_data=validated.model_dump(mode="json"),
        current_data=current_data,
        source=source_ref,
    )

    draft_id = firebase_store.save_review_draft(draft.model_dump(mode="json"))

    return PipelineResult("drafted", "Draft created and sent to review queue.", draft_id=draft_id)


def _now_iso_placeholder() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()