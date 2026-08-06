"""
Step 4 — classify why a re-extraction was triggered, so the review queue can
show editors a meaningful reason instead of a raw diff.

Keeps this deterministic/cheap (keyword + shallow diff based) rather than
spending an LLM call just to classify — Gemini is reserved for Step 5
(structured extraction).
"""

from athlete_schemas import TriggerType

_KEYWORD_RULES: list[tuple[TriggerType, list[str]]] = [
    (TriggerType.RETIREMENT, ["retire", "retirement", "hangs up", "steps away from"]),
    (TriggerType.NEW_PERSONAL_BEST, ["personal best", "national record", "world record", "pb ", "season best"]),
    (TriggerType.NEW_TITLE, ["wins gold", "champion", "title", "wins the", "clinches"]),
    (TriggerType.COACH_CHANGE, ["new coach", "parts ways with coach", "appoints coach", "coaching change"]),
    (TriggerType.INJURY_UPDATE, ["injury", "injured", "surgery", "recovering from"]),
    (TriggerType.TEAM_CHANGE, ["signs with", "joins", "transfers to", "new team", "new club"]),
    (TriggerType.RANKING_CHANGE, ["ranking", "ranked", "climbs to", "drops to"]),
]


def classify_trigger(raw_text: str, is_new_athlete: bool = False) -> TriggerType:
    """
    raw_text: any free text available pre-extraction (source headline/summary
    snippet) used purely for classification — the full structured extraction
    still happens in Step 5 via Gemini.
    """
    if is_new_athlete:
        return TriggerType.NEW_ATHLETE_ONBOARDING

    text = (raw_text or "").lower()
    for trigger_type, keywords in _KEYWORD_RULES:
        if any(kw in text for kw in keywords):
            return trigger_type

    return TriggerType.GENERAL_BIO_UPDATE