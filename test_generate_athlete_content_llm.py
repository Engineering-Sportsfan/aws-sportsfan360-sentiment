# """
# test_generate_athlete_content_llm.py

# Mocks the Gemini call so you can verify the prompt -> JSON -> schema
# validation pipeline works WITHOUT live Vertex/Gemini credentials or
# burning API quota. Run with:

#     python -m pytest test_generate_athlete_content_llm.py -v

# or directly:

#     python test_generate_athlete_content_llm.py
# """

# import json
# from unittest.mock import MagicMock, patch

# from athlete_schemas import Sport, TrackFieldProfile
# import generate_athlete_content_llm as gen


# FAKE_GEMINI_JSON = {
#     "nationality": "India",
#     "date_of_birth": "1997-12-24",
#     "current_team_or_federation": None,
#     "coach": "Klaus Bartonietz",
#     "current_ranking": 2,
#     "career_highlights": [
#         {"title": "Olympic Gold", "event": "Javelin Throw", "year": 2021}
#     ],
#     "bio_summary": "Neeraj Chopra is an Indian javelin thrower and the 2021 Olympic champion.",
#     "quick_facts": {
#         "age": 28,
#         "height": "1.86 m",
#         "weight": "86 kg",
#         "birthplace": "Khandra, Haryana",
#         "dominant_hand": "Right",
#         "personal_best": "90.23 m",
#         "years_active": "2016",
#         "coach": "Klaus Bartonietz",
#         "honors": ["Olympic Champion", "World Champion", "Asian Champion", "CWG Gold", "Diamond League Winner"],
#     },
#     "medal_cabinet": [
#         {"medal_type": "Olympics Gold", "competition": "Tokyo 2020", "year": 2021},
#         {"medal_type": "Olympics Silver", "competition": "Paris 2024", "year": 2024},
#         {"medal_type": "World Championship", "competition": "Budapest", "year": 2023},
#     ],
#     "season_stats": {
#         "season_label": "2026 Season",
#         "events": 8,
#         "gold": 5,
#         "silver": 2,
#         "bronze": 1,
#         "season_best": "88.94 m",
#         "average_mark": "87.72 m",
#         "current_streak": "4 Podiums",
#     },
#     "performance_trend": [
#         {"year": 2021, "value": "87.58 m"},
#         {"year": 2022, "value": "88.13 m"},
#         {"year": 2023, "value": "88.17 m"},
#         {"year": 2024, "value": "89.45 m"},
#         {"year": 2025, "value": "90.23 m"},
#         {"year": 2026, "value": "88.94 m"},
#     ],
#     "event": "Javelin Throw",
#     "personal_best": "90.23 m",
#     "personal_best_date": "2025-08-30",
#     "season_best": "88.94 m",
# }


# def _mock_response(payload: dict):
#     resp = MagicMock()
#     resp.text = json.dumps(payload)
#     return resp


# def test_generate_athlete_profile_valid_response():
#     with patch.object(gen.client.models, "generate_content", return_value=_mock_response(FAKE_GEMINI_JSON)):
#         profile = gen.generate_athlete_profile("Neeraj Chopra", Sport.TRACK_FIELD)

#     assert isinstance(profile, TrackFieldProfile)
#     assert profile.athlete_id == "neeraj_chopra"
#     assert profile.full_name == "Neeraj Chopra"
#     assert profile.quick_facts.personal_best == "90.23 m"
#     assert len(profile.medal_cabinet) == 3
#     assert profile.medal_cabinet[0].medal_type == "Olympics Gold"
#     assert profile.season_stats.gold == 5
#     assert len(profile.performance_trend) == 6
#     assert profile.performance_trend[-1].year == 2026
#     assert profile.source.source_name.startswith("Gemini")


# def test_generate_athlete_profile_handles_markdown_fences():
#     fenced_text = "```json\n" + json.dumps(FAKE_GEMINI_JSON) + "\n```"
#     resp = MagicMock()
#     resp.text = fenced_text

#     with patch.object(gen.client.models, "generate_content", return_value=resp):
#         profile = gen.generate_athlete_profile("Neeraj Chopra", Sport.TRACK_FIELD)

#     assert profile.quick_facts.age == 28


# def test_generate_athlete_profile_invalid_json_raises():
#     resp = MagicMock()
#     resp.text = "Sorry, I cannot help with that."

#     with patch.object(gen.client.models, "generate_content", return_value=resp):
#         try:
#             gen.generate_athlete_profile("Nobody Athlete", Sport.TRACK_FIELD)
#             assert False, "expected GenerationError"
#         except gen.GenerationError:
#             pass


# def test_generate_athlete_profile_bad_field_type_raises_validation_error():
#     bad_payload = dict(FAKE_GEMINI_JSON)
#     bad_payload["quick_facts"] = {"age": "not-a-number"}  # wrong type
#     resp = _mock_response(bad_payload)

#     from pydantic import ValidationError

#     with patch.object(gen.client.models, "generate_content", return_value=resp):
#         try:
#             gen.generate_athlete_profile("Neeraj Chopra", Sport.TRACK_FIELD)
#             assert False, "expected ValidationError"
#         except ValidationError:
#             pass


# def test_generate_athlete_profile_coerces_string_career_highlights():
#     """Regression test: Gemini sometimes returns career_highlights as plain
#     strings instead of {title, event, year} objects (seen for Neeraj Chopra
#     and Lovlina Borgohain in manual testing)."""
#     payload = dict(FAKE_GEMINI_JSON)
#     payload["career_highlights"] = [
#         "Olympic Gold Medalist (Tokyo 2020)",
#         "World Champion (Budapest 2023)",
#     ]
#     resp = _mock_response(payload)

#     with patch.object(gen.client.models, "generate_content", return_value=resp):
#         profile = gen.generate_athlete_profile("Neeraj Chopra", Sport.TRACK_FIELD)

#     assert len(profile.career_highlights) == 2
#     assert profile.career_highlights[0].title == "Olympic Gold Medalist (Tokyo 2020)"


# def test_generate_athlete_profile_coerces_null_list_fields():
#     """Regression test: Gemini returned null (not []) for performance_trend
#     and medal_cabinet for a boxer (Lovlina Borgohain) — sports with no
#     single trackable 'mark'."""
#     payload = dict(FAKE_GEMINI_JSON)
#     # BaseAthleteProfile (used for boxing) has no event/personal_best/
#     # season_best fields — those are TrackFieldProfile-only.
#     for track_only_field in ("event", "personal_best", "personal_best_date", "season_best"):
#         payload.pop(track_only_field, None)
#     payload["performance_trend"] = None
#     payload["medal_cabinet"] = None
#     payload["career_highlights"] = ["Olympic Bronze Medalist (2020 Tokyo)"]
#     resp = _mock_response(payload)

#     with patch.object(gen.client.models, "generate_content", return_value=resp):
#         profile = gen.generate_athlete_profile("Lovlina Borgohain", Sport.BOXING)

#     assert profile.performance_trend == []
#     assert profile.medal_cabinet == []
#     assert profile.career_highlights[0].title == "Olympic Bronze Medalist (2020 Tokyo)"


# def test_generate_athlete_profile_captures_grounding_sources():
#     """When Gemini's response includes search-grounding metadata, the URLs
#     should end up in profile.grounding_sources for reviewer spot-checking."""
#     resp = _mock_response(FAKE_GEMINI_JSON)

#     web1 = MagicMock()
#     web1.uri = "https://en.wikipedia.org/wiki/Neeraj_Chopra"
#     chunk1 = MagicMock()
#     chunk1.web = web1

#     web2 = MagicMock()
#     web2.uri = "https://worldathletics.org/athletes/india/neeraj-chopra"
#     chunk2 = MagicMock()
#     chunk2.web = web2

#     grounding_metadata = MagicMock()
#     grounding_metadata.grounding_chunks = [chunk1, chunk2]

#     candidate = MagicMock()
#     candidate.grounding_metadata = grounding_metadata
#     resp.candidates = [candidate]

#     with patch.object(gen.client.models, "generate_content", return_value=resp):
#         profile = gen.generate_athlete_profile("Neeraj Chopra", Sport.TRACK_FIELD)

#     assert profile.grounding_sources == [
#         "https://en.wikipedia.org/wiki/Neeraj_Chopra",
#         "https://worldathletics.org/athletes/india/neeraj-chopra",
#     ]


# def test_generate_athlete_profile_flattens_nested_personal_best():
#     """Regression test: Gemini returned quick_facts.personal_best as a nested
#     dict for Mirabai Chanu (weightlifting has snatch/clean&jerk/total)."""
#     payload = dict(FAKE_GEMINI_JSON)
#     for track_only_field in ("event", "personal_best", "personal_best_date", "season_best"):
#         payload.pop(track_only_field, None)
#     payload["quick_facts"] = dict(payload["quick_facts"])
#     payload["quick_facts"]["personal_best"] = {"total": "205 kg (49 kg category, World Record)"}
#     resp = _mock_response(payload)

#     with patch.object(gen.client.models, "generate_content", return_value=resp):
#         profile = gen.generate_athlete_profile("Mirabai Chanu", Sport.OTHER)

#     assert isinstance(profile.quick_facts.personal_best, str)
#     assert "205 kg" in profile.quick_facts.personal_best


# def test_generate_athlete_profile_coerces_ranking_and_events_types():
#     """Regression test: Gemini returned current_ranking as 'World No. 1'
#     (Neeraj Chopra) and season_stats.events as a list of competition names
#     instead of a count (Neeraj Chopra, Lovlina Borgohain)."""
#     payload = dict(FAKE_GEMINI_JSON)
#     payload["current_ranking"] = "World No. 1"
#     payload["season_stats"] = dict(payload["season_stats"])
#     payload["season_stats"]["events"] = ["Doha Diamond League", "Paris Olympics"]
#     resp = _mock_response(payload)

#     with patch.object(gen.client.models, "generate_content", return_value=resp):
#         profile = gen.generate_athlete_profile("Neeraj Chopra", Sport.TRACK_FIELD)

#     assert profile.current_ranking == 1
#     assert profile.season_stats.events == 2


# def test_generate_athlete_profile_coerces_years_active_int():
#     """Regression test: Gemini returned quick_facts.years_active as a bare
#     int (14) instead of a string like '2011-present' (Lovlina Borgohain)."""
#     payload = dict(FAKE_GEMINI_JSON)
#     for track_only_field in ("event", "personal_best", "personal_best_date", "season_best"):
#         payload.pop(track_only_field, None)
#     payload["quick_facts"] = dict(payload["quick_facts"])
#     payload["quick_facts"]["years_active"] = 14
#     resp = _mock_response(payload)

#     with patch.object(gen.client.models, "generate_content", return_value=resp):
#         profile = gen.generate_athlete_profile("Lovlina Borgohain", Sport.BOXING)

#     assert profile.quick_facts.years_active == "14"


# def test_slugify():
#     assert gen._slugify("Neeraj Chopra") == "neeraj_chopra"
#     assert gen._slugify("  P.V. Sindhu  ") == "p_v_sindhu"


# if __name__ == "__main__":
#     # Allow running without pytest installed.
#     import traceback

#     tests = [
#         test_generate_athlete_profile_valid_response,
#         test_generate_athlete_profile_handles_markdown_fences,
#         test_generate_athlete_profile_invalid_json_raises,
#         test_generate_athlete_profile_bad_field_type_raises_validation_error,
#         test_generate_athlete_profile_coerces_string_career_highlights,
#         test_generate_athlete_profile_coerces_null_list_fields,
#         test_generate_athlete_profile_captures_grounding_sources,
#         test_generate_athlete_profile_flattens_nested_personal_best,
#         test_generate_athlete_profile_coerces_ranking_and_events_types,
#         test_generate_athlete_profile_coerces_years_active_int,
#         test_slugify,
#     ]
#     failures = 0
#     for t in tests:
#         try:
#             t()
#             print(f"PASS: {t.__name__}")
#         except Exception:
#             failures += 1
#             print(f"FAIL: {t.__name__}")
#             traceback.print_exc()

#     print(f"\n{len(tests) - failures}/{len(tests)} passed")





"""
test_generate_athlete_content_llm.py

Mocks the Gemini call so you can verify the prompt -> JSON -> schema
validation pipeline works WITHOUT live Vertex/Gemini credentials or
burning API quota. Run with:

    python -m pytest test_generate_athlete_content_llm.py -v

or directly:

    python test_generate_athlete_content_llm.py
"""

import json
from unittest.mock import MagicMock, patch

from athlete_schemas import Sport, TrackFieldProfile
import generate_athlete_content_llm as gen


FAKE_GEMINI_JSON = {
    "nationality": "India",
    "date_of_birth": "1997-12-24",
    "current_team_or_federation": None,
    "coach": "Klaus Bartonietz",
    "current_ranking": 2,
    "career_highlights": [
        {"title": "Olympic Gold", "event": "Javelin Throw", "year": 2021}
    ],
    "bio_summary": "Neeraj Chopra is an Indian javelin thrower and the 2021 Olympic champion.",
    "quick_facts": {
        "age": 28,
        "height": "1.86 m",
        "weight": "86 kg",
        "birthplace": "Khandra, Haryana",
        "dominant_hand": "Right",
        "personal_best": "90.23 m",
        "years_active": "2016",
        "coach": "Klaus Bartonietz",
        "honors": ["Olympic Champion", "World Champion", "Asian Champion", "CWG Gold", "Diamond League Winner"],
    },
    "medal_cabinet": [
        {"medal_type": "Olympics Gold", "competition": "Tokyo 2020", "year": 2021},
        {"medal_type": "Olympics Silver", "competition": "Paris 2024", "year": 2024},
        {"medal_type": "World Championship", "competition": "Budapest", "year": 2023},
    ],
    "season_stats": {
        "season_label": "2026 Season",
        "events": 8,
        "gold": 5,
        "silver": 2,
        "bronze": 1,
        "season_best": "88.94 m",
        "average_mark": "87.72 m",
        "current_streak": "4 Podiums",
    },
    "performance_trend": [
        {"year": 2021, "value": "87.58 m"},
        {"year": 2022, "value": "88.13 m"},
        {"year": 2023, "value": "88.17 m"},
        {"year": 2024, "value": "89.45 m"},
        {"year": 2025, "value": "90.23 m"},
        {"year": 2026, "value": "88.94 m"},
    ],
    "event": "Javelin Throw",
    "personal_best": "90.23 m",
    "personal_best_date": "2025-08-30",
    "season_best": "88.94 m",
}


def _mock_response(payload: dict):
    resp = MagicMock()
    resp.text = json.dumps(payload)
    return resp


def test_generate_athlete_profile_valid_response():
    with patch.object(gen.client.models, "generate_content", return_value=_mock_response(FAKE_GEMINI_JSON)):
        profile = gen.generate_athlete_profile("Neeraj Chopra", Sport.TRACK_FIELD)

    assert isinstance(profile, TrackFieldProfile)
    assert profile.athlete_id == "neeraj_chopra"
    assert profile.full_name == "Neeraj Chopra"
    assert profile.quick_facts.personal_best == "90.23 m"
    assert len(profile.medal_cabinet) == 3
    assert profile.medal_cabinet[0].medal_type == "Olympics Gold"
    assert profile.season_stats.gold == 5
    assert len(profile.performance_trend) == 6
    assert profile.performance_trend[-1].year == 2026
    assert profile.source.source_name.startswith("Gemini")


def test_generate_athlete_profile_handles_markdown_fences():
    fenced_text = "```json\n" + json.dumps(FAKE_GEMINI_JSON) + "\n```"
    resp = MagicMock()
    resp.text = fenced_text

    with patch.object(gen.client.models, "generate_content", return_value=resp):
        profile = gen.generate_athlete_profile("Neeraj Chopra", Sport.TRACK_FIELD)

    assert profile.quick_facts.age == 28


def test_generate_athlete_profile_invalid_json_raises():
    resp = MagicMock()
    resp.text = "Sorry, I cannot help with that."

    with patch.object(gen.client.models, "generate_content", return_value=resp):
        try:
            gen.generate_athlete_profile("Nobody Athlete", Sport.TRACK_FIELD)
            assert False, "expected GenerationError"
        except gen.GenerationError:
            pass


def test_generate_athlete_profile_unparseable_date_still_raises_validation_error():
    """The generic coercion engine deliberately leaves date/bool/enum/etc.
    fields untouched (it only coerces str/int/float and known list/model
    shapes) so genuinely malformed data in those fields still gets
    rejected by Pydantic rather than silently passed through."""
    bad_payload = dict(FAKE_GEMINI_JSON)
    bad_payload["date_of_birth"] = "not-a-real-date"
    resp = _mock_response(bad_payload)

    from pydantic import ValidationError

    with patch.object(gen.client.models, "generate_content", return_value=resp):
        try:
            gen.generate_athlete_profile("Neeraj Chopra", Sport.TRACK_FIELD)
            assert False, "expected ValidationError"
        except ValidationError:
            pass


def test_generate_athlete_profile_gracefully_nulls_unparseable_scalar():
    """A str/int/float field the engine CAN'T confidently parse (e.g. a
    non-numeric age string) is coerced to None rather than raising — the
    goal is that a new athlete/sport never crashes the whole draft over
    one bad field; the review queue is where a human catches an
    unexpectedly-null field, not a stack trace."""
    payload = dict(FAKE_GEMINI_JSON)
    payload["quick_facts"] = dict(payload["quick_facts"])
    payload["quick_facts"]["age"] = "not-a-number"
    resp = _mock_response(payload)

    with patch.object(gen.client.models, "generate_content", return_value=resp):
        profile = gen.generate_athlete_profile("Neeraj Chopra", Sport.TRACK_FIELD)

    assert profile.quick_facts.age is None


def test_generate_athlete_profile_coerces_string_career_highlights():
    """Regression test: Gemini sometimes returns career_highlights as plain
    strings instead of {title, event, year} objects (seen for Neeraj Chopra
    and Lovlina Borgohain in manual testing)."""
    payload = dict(FAKE_GEMINI_JSON)
    payload["career_highlights"] = [
        "Olympic Gold Medalist (Tokyo 2020)",
        "World Champion (Budapest 2023)",
    ]
    resp = _mock_response(payload)

    with patch.object(gen.client.models, "generate_content", return_value=resp):
        profile = gen.generate_athlete_profile("Neeraj Chopra", Sport.TRACK_FIELD)

    assert len(profile.career_highlights) == 2
    assert profile.career_highlights[0].title == "Olympic Gold Medalist (Tokyo 2020)"


def test_generate_athlete_profile_coerces_null_list_fields():
    """Regression test: Gemini returned null (not []) for performance_trend
    and medal_cabinet for a boxer (Lovlina Borgohain) — sports with no
    single trackable 'mark'."""
    payload = dict(FAKE_GEMINI_JSON)
    # BaseAthleteProfile (used for boxing) has no event/personal_best/
    # season_best fields — those are TrackFieldProfile-only.
    for track_only_field in ("event", "personal_best", "personal_best_date", "season_best"):
        payload.pop(track_only_field, None)
    payload["performance_trend"] = None
    payload["medal_cabinet"] = None
    payload["career_highlights"] = ["Olympic Bronze Medalist (2020 Tokyo)"]
    resp = _mock_response(payload)

    with patch.object(gen.client.models, "generate_content", return_value=resp):
        profile = gen.generate_athlete_profile("Lovlina Borgohain", Sport.BOXING)

    assert profile.performance_trend == []
    assert profile.medal_cabinet == []
    assert profile.career_highlights[0].title == "Olympic Bronze Medalist (2020 Tokyo)"


def test_generate_athlete_profile_captures_grounding_sources():
    """When Gemini's response includes search-grounding metadata, the URLs
    should end up in profile.grounding_sources for reviewer spot-checking."""
    resp = _mock_response(FAKE_GEMINI_JSON)

    web1 = MagicMock()
    web1.uri = "https://en.wikipedia.org/wiki/Neeraj_Chopra"
    chunk1 = MagicMock()
    chunk1.web = web1

    web2 = MagicMock()
    web2.uri = "https://worldathletics.org/athletes/india/neeraj-chopra"
    chunk2 = MagicMock()
    chunk2.web = web2

    grounding_metadata = MagicMock()
    grounding_metadata.grounding_chunks = [chunk1, chunk2]

    candidate = MagicMock()
    candidate.grounding_metadata = grounding_metadata
    resp.candidates = [candidate]

    with patch.object(gen.client.models, "generate_content", return_value=resp):
        profile = gen.generate_athlete_profile("Neeraj Chopra", Sport.TRACK_FIELD)

    assert profile.grounding_sources == [
        "https://en.wikipedia.org/wiki/Neeraj_Chopra",
        "https://worldathletics.org/athletes/india/neeraj-chopra",
    ]


def test_generate_athlete_profile_flattens_nested_personal_best():
    """Regression test: Gemini returned quick_facts.personal_best as a nested
    dict for Mirabai Chanu (weightlifting has snatch/clean&jerk/total)."""
    payload = dict(FAKE_GEMINI_JSON)
    for track_only_field in ("event", "personal_best", "personal_best_date", "season_best"):
        payload.pop(track_only_field, None)
    payload["quick_facts"] = dict(payload["quick_facts"])
    payload["quick_facts"]["personal_best"] = {"total": "205 kg (49 kg category, World Record)"}
    resp = _mock_response(payload)

    with patch.object(gen.client.models, "generate_content", return_value=resp):
        profile = gen.generate_athlete_profile("Mirabai Chanu", Sport.OTHER)

    assert isinstance(profile.quick_facts.personal_best, str)
    assert "205 kg" in profile.quick_facts.personal_best


def test_generate_athlete_profile_coerces_ranking_and_events_types():
    """Regression test: Gemini returned current_ranking as 'World No. 1'
    (Neeraj Chopra) and season_stats.events as a list of competition names
    instead of a count (Neeraj Chopra, Lovlina Borgohain)."""
    payload = dict(FAKE_GEMINI_JSON)
    payload["current_ranking"] = "World No. 1"
    payload["season_stats"] = dict(payload["season_stats"])
    payload["season_stats"]["events"] = ["Doha Diamond League", "Paris Olympics"]
    resp = _mock_response(payload)

    with patch.object(gen.client.models, "generate_content", return_value=resp):
        profile = gen.generate_athlete_profile("Neeraj Chopra", Sport.TRACK_FIELD)

    assert profile.current_ranking == 1
    assert profile.season_stats.events == 2


def test_generate_athlete_profile_coerces_years_active_int():
    """Regression test: Gemini returned quick_facts.years_active as a bare
    int (14) instead of a string like '2011-present' (Lovlina Borgohain)."""
    payload = dict(FAKE_GEMINI_JSON)
    for track_only_field in ("event", "personal_best", "personal_best_date", "season_best"):
        payload.pop(track_only_field, None)
    payload["quick_facts"] = dict(payload["quick_facts"])
    payload["quick_facts"]["years_active"] = 14
    resp = _mock_response(payload)

    with patch.object(gen.client.models, "generate_content", return_value=resp):
        profile = gen.generate_athlete_profile("Lovlina Borgohain", Sport.BOXING)

    assert profile.quick_facts.years_active == "14"


def test_generate_athlete_profile_retries_after_truncated_response():
    """Regression test: a MAX_TOKENS-truncated response (seen for Lovlina
    Borgohain) should trigger one automatic retry rather than failing
    immediately, and succeed if the retry returns clean JSON."""
    truncated_resp = MagicMock()
    truncated_resp.text = '{"bio_summary": "cut off mid-sentence and no closing brace'
    bad_candidate = MagicMock()
    bad_candidate.finish_reason = "MAX_TOKENS"
    truncated_resp.candidates = [bad_candidate]

    good_resp = _mock_response(FAKE_GEMINI_JSON)

    with patch.object(gen.client.models, "generate_content", side_effect=[truncated_resp, good_resp]):
        profile = gen.generate_athlete_profile("Neeraj Chopra", Sport.TRACK_FIELD)

    assert profile.full_name == "Neeraj Chopra"


def test_generate_athlete_profile_strips_coach_parentheticals():
    """Regression test: Gemini returned coach as 'Jaspal Rana (former coach,
    deceased)' instead of a plain name (Manu Bhaker)."""
    payload = dict(FAKE_GEMINI_JSON)
    for track_only_field in ("event", "personal_best", "personal_best_date", "season_best"):
        payload.pop(track_only_field, None)
    payload["quick_facts"] = dict(payload["quick_facts"])
    payload["quick_facts"]["coach"] = "Jaspal Rana (former coach, deceased)"
    resp = _mock_response(payload)

    with patch.object(gen.client.models, "generate_content", return_value=resp):
        profile = gen.generate_athlete_profile("Manu Bhaker", Sport.SHOOTING)

    assert profile.quick_facts.coach == "Jaspal Rana"


def test_generate_athlete_profile_coerces_string_na_list_fields():
    """Regression test: Gemini returned the literal string "N/A" for the
    whole performance_trend field instead of [] for a boxer (Lovlina
    Borgohain) — likely cross-contamination from the N/A guidance given
    for scalar fields in the same prompt. Should coerce to [], same as a
    null value would, not crash validation."""
    payload = dict(FAKE_GEMINI_JSON)
    for track_only_field in ("event", "personal_best", "personal_best_date", "season_best"):
        payload.pop(track_only_field, None)
    payload["performance_trend"] = "N/A"
    payload["medal_cabinet"] = "N/A"
    resp = _mock_response(payload)

    with patch.object(gen.client.models, "generate_content", return_value=resp):
        profile = gen.generate_athlete_profile("Lovlina Borgohain", Sport.BOXING)

    assert profile.performance_trend == []
    assert profile.medal_cabinet == []


def test_generate_athlete_profile_coerces_null_trend_point_value():
    """Regression test: Gemini returned a performance_trend entry with a
    known year but null value (e.g. {"year": 2023, "value": null}) for
    Murali Sreeshankar — PerformanceTrendPoint.value is a required string,
    so this crashed validation. Should coerce to 'N/A', not drop the point
    or raise."""
    payload = dict(FAKE_GEMINI_JSON)
    payload["performance_trend"] = [
        {"year": 2022, "value": "8.36 m"},
        {"year": 2023, "value": None},
        {"year": 2024, "value": None},
    ]
    resp = _mock_response(payload)

    with patch.object(gen.client.models, "generate_content", return_value=resp):
        profile = gen.generate_athlete_profile("Murali Sreeshankar", Sport.TRACK_FIELD)

    assert len(profile.performance_trend) == 3
    assert profile.performance_trend[0].value == "8.36 m"
    assert profile.performance_trend[1].value == "N/A"
    assert profile.performance_trend[2].value == "N/A"


def test_generate_athlete_profile_coerces_numeric_trend_point_value():
    """Regression test: Gemini returned performance_trend values as raw
    numbers (e.g. 590, a shooting score) instead of strings for Manu
    Bhaker (shooting) — PerformanceTrendPoint.value requires a string.
    Should coerce to string, not crash."""
    payload = dict(FAKE_GEMINI_JSON)
    for track_only_field in ("event", "personal_best", "personal_best_date", "season_best"):
        payload.pop(track_only_field, None)
    payload["performance_trend"] = [
        {"year": 2023, "value": 590},
        {"year": 2024, "value": 580.5},
    ]
    resp = _mock_response(payload)

    with patch.object(gen.client.models, "generate_content", return_value=resp):
        profile = gen.generate_athlete_profile("Manu Bhaker", Sport.SHOOTING)

    assert profile.performance_trend[0].value == "590"
    assert profile.performance_trend[1].value == "580.5"


def test_generate_athlete_profile_folds_extra_medal_event_key():
    """Regression test: Gemini added an extra "event" key to medal_cabinet
    entries (e.g. "10m Air Pistol Women") not present in the Medal schema
    (medal_type/competition/year only, extra="forbid") — seen for Manu
    Bhaker (shooting). Should fold it into competition rather than crash."""
    payload = dict(FAKE_GEMINI_JSON)
    for track_only_field in ("event", "personal_best", "personal_best_date", "season_best"):
        payload.pop(track_only_field, None)
    payload["medal_cabinet"] = [
        {"medal_type": "Bronze", "competition": "Olympic Games", "year": 2024, "event": "10m Air Pistol Women"},
        {"medal_type": "Gold", "year": 2018, "event": "10m Air Pistol Women"},
    ]
    resp = _mock_response(payload)

    with patch.object(gen.client.models, "generate_content", return_value=resp):
        profile = gen.generate_athlete_profile("Manu Bhaker", Sport.SHOOTING)

    assert profile.medal_cabinet[0].competition == "Olympic Games — 10m Air Pistol Women"
    assert profile.medal_cabinet[1].competition == "10m Air Pistol Women"


def test_generate_athlete_profile_marks_na_for_inapplicable_boxing_fields():
    """New: boxing's personal_best/season_best/average_mark/current_streak
    don't structurally apply — should come back as the string 'N/A', not
    an ambiguous null, so reviewers can tell 'not applicable' apart from
    'Gemini failed to find it'. performance_trend stays [] (its existing
    null-list normalization already unambiguously means 'no entries')."""
    payload = dict(FAKE_GEMINI_JSON)
    for track_only_field in ("event", "personal_best", "personal_best_date", "season_best"):
        payload.pop(track_only_field, None)
    payload["quick_facts"] = dict(payload["quick_facts"])
    payload["quick_facts"]["personal_best"] = None
    payload["season_stats"] = dict(payload["season_stats"])
    payload["season_stats"]["current_streak"] = None
    payload["season_stats"]["average_mark"] = None
    payload["season_stats"]["season_best"] = None
    payload["performance_trend"] = None
    resp = _mock_response(payload)

    with patch.object(gen.client.models, "generate_content", return_value=resp):
        profile = gen.generate_athlete_profile("Lovlina Borgohain", Sport.BOXING)

    assert profile.quick_facts.personal_best == "N/A"
    assert profile.season_stats.current_streak == "N/A"
    assert profile.season_stats.average_mark == "N/A"
    assert profile.season_stats.season_best == "N/A"
    assert profile.performance_trend == []


def test_generate_athlete_profile_marks_na_dominant_hand_for_shooting():
    """New: shooting's dominant_hand/average_mark/current_streak don't
    apply and should become 'N/A'. current_ranking IS a 'push' field for
    shooting and, when Gemini actually supplied a value, must be left
    untouched rather than overwritten."""
    payload = dict(FAKE_GEMINI_JSON)
    for track_only_field in ("event", "personal_best", "personal_best_date", "season_best"):
        payload.pop(track_only_field, None)
    payload["quick_facts"] = dict(payload["quick_facts"])
    payload["quick_facts"]["dominant_hand"] = None
    payload["season_stats"] = dict(payload["season_stats"])
    payload["season_stats"]["average_mark"] = None
    payload["season_stats"]["current_streak"] = None
    payload["current_ranking"] = 5
    resp = _mock_response(payload)

    with patch.object(gen.client.models, "generate_content", return_value=resp):
        profile = gen.generate_athlete_profile("Manu Bhaker", Sport.SHOOTING)

    assert profile.quick_facts.dominant_hand == "N/A"
    assert profile.season_stats.average_mark == "N/A"
    assert profile.season_stats.current_streak == "N/A"
    assert profile.current_ranking == 5  # push field, untouched since it was found


def test_generate_athlete_profile_does_not_overwrite_filled_na_fields():
    """New: N/A substitution must only apply to genuinely-null values — a
    field Gemini actually populated (even one flagged 'na' for this sport)
    should never get overwritten with 'N/A'."""
    payload = dict(FAKE_GEMINI_JSON)
    payload["season_stats"] = dict(payload["season_stats"])
    payload["season_stats"]["current_streak"] = "3 Podiums"  # track_field's "na" field, but filled

    resp = _mock_response(payload)
    with patch.object(gen.client.models, "generate_content", return_value=resp):
        profile = gen.generate_athlete_profile("Neeraj Chopra", Sport.TRACK_FIELD)

    assert profile.season_stats.current_streak == "3 Podiums"


def test_generate_athlete_profile_leaves_push_fields_null_when_genuinely_unfound():
    """New: a 'push' field (one Gemini is told to search harder for) that
    still comes back null after search should stay null, not get silently
    converted to 'N/A' — that would hide a real search miss from reviewers."""
    payload = dict(FAKE_GEMINI_JSON)
    payload["current_ranking"] = None  # push field for track_field, genuinely not found

    resp = _mock_response(payload)
    with patch.object(gen.client.models, "generate_content", return_value=resp):
        profile = gen.generate_athlete_profile("Neeraj Chopra", Sport.TRACK_FIELD)

    assert profile.current_ranking is None


def test_generate_athlete_profile_coerces_dict_ranking_to_best_value():
    """Regression test: Gemini returned current_ranking as a per-event
    breakdown dict, e.g. {"10m Air Pistol Women": 3, "25m Pistol Women":
    10}, instead of a single int (seen for Manu Bhaker, shooting). Should
    extract the best (lowest) rank across events rather than crash."""
    payload = dict(FAKE_GEMINI_JSON)
    for track_only_field in ("event", "personal_best", "personal_best_date", "season_best"):
        payload.pop(track_only_field, None)
    payload["current_ranking"] = {"10m Air Pistol Women": 3, "25m Pistol Women": 10}
    resp = _mock_response(payload)

    with patch.object(gen.client.models, "generate_content", return_value=resp):
        profile = gen.generate_athlete_profile("Manu Bhaker", Sport.SHOOTING)

    assert profile.current_ranking == 3


def test_generic_engine_handles_novel_combination_never_seen_before():
    """Proof test: throws several shape mismatches at once, on cricket (a
    sport with no prior bug history in this suite), in combinations that
    were never individually coded for. If this still validates cleanly,
    it's the generic schema-driven engine doing the work — not a
    memorized per-bug patch — which is the actual point of that engine:
    a brand-new sport shouldn't need a brand-new fix."""
    payload = dict(FAKE_GEMINI_JSON)
    for track_only_field in ("event", "personal_best", "personal_best_date", "season_best"):
        payload.pop(track_only_field, None)

    payload["current_ranking"] = "Ranked #14 in the world"  # string -> int
    payload["career_highlights"] = "World Cup Winner 2023"  # bare string -> [] wrapped? actually whole-field string
    payload["quick_facts"] = {
        "age": 29.0,  # float -> int
        "years_active": 9,  # int -> str
        "personal_best": ["180", "not out"],  # list -> flattened str
        "honors": None,  # null -> []
        "coach": "Rahul Dravid (also NCA head)",
    }
    payload["medal_cabinet"] = [
        {"medal_type": "Gold", "competition": "World Cup", "year": "2023", "format": "ODI"},  # year as str + extra key
    ]
    payload["season_stats"] = {
        "season_label": "2026 Season",
        "events": ["IPL 2026", "Asia Cup 2026", "World Cup 2026"],  # list -> count
        "gold": None, "silver": None, "bronze": None,
        "season_best": 98,  # int -> str
        "average_mark": None,
        "current_streak": None,
    }
    payload["performance_trend"] = [{"year": "2024", "value": 450}]  # year as str, value as int

    resp = _mock_response(payload)
    with patch.object(gen.client.models, "generate_content", return_value=resp):
        # Should not raise — every mismatch above is a shape the generic
        # engine handles without any cricket-specific code existing for it.
        profile = gen.generate_athlete_profile("Fictional Cricketer", Sport.CRICKET)

    assert profile.current_ranking == 14
    assert profile.quick_facts.age == 29
    assert profile.quick_facts.years_active == "9"
    assert "180" in profile.quick_facts.personal_best
    assert profile.quick_facts.honors == []
    assert profile.medal_cabinet[0].year == 2023
    # medal's extra "format" key got folded into competition rather than crashing
    assert "ODI" in profile.medal_cabinet[0].competition
    assert profile.season_stats.events == 3
    assert profile.season_stats.season_best == "98"
    assert profile.performance_trend[0].year == 2024
    assert profile.performance_trend[0].value == "450"


def test_slugify():
    assert gen._slugify("Neeraj Chopra") == "neeraj_chopra"
    assert gen._slugify("  P.V. Sindhu  ") == "p_v_sindhu"


if __name__ == "__main__":
    # Allow running without pytest installed.
    import traceback

    tests = [
        test_generate_athlete_profile_valid_response,
        test_generate_athlete_profile_handles_markdown_fences,
        test_generate_athlete_profile_invalid_json_raises,
        test_generate_athlete_profile_unparseable_date_still_raises_validation_error,
        test_generate_athlete_profile_gracefully_nulls_unparseable_scalar,
        test_generate_athlete_profile_coerces_string_career_highlights,
        test_generate_athlete_profile_coerces_null_list_fields,
        test_generate_athlete_profile_captures_grounding_sources,
        test_generate_athlete_profile_flattens_nested_personal_best,
        test_generate_athlete_profile_coerces_ranking_and_events_types,
        test_generate_athlete_profile_coerces_years_active_int,
        test_generate_athlete_profile_retries_after_truncated_response,
        test_generate_athlete_profile_strips_coach_parentheticals,
        test_generate_athlete_profile_coerces_string_na_list_fields,
        test_generate_athlete_profile_coerces_null_trend_point_value,
        test_generate_athlete_profile_coerces_numeric_trend_point_value,
        test_generate_athlete_profile_folds_extra_medal_event_key,
        test_generate_athlete_profile_marks_na_for_inapplicable_boxing_fields,
        test_generate_athlete_profile_marks_na_dominant_hand_for_shooting,
        test_generate_athlete_profile_does_not_overwrite_filled_na_fields,
        test_generate_athlete_profile_leaves_push_fields_null_when_genuinely_unfound,
        test_generate_athlete_profile_coerces_dict_ranking_to_best_value,
        test_generic_engine_handles_novel_combination_never_seen_before,
        test_slugify,
    ]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
        except Exception:
            failures += 1
            print(f"FAIL: {t.__name__}")
            traceback.print_exc()

    print(f"\n{len(tests) - failures}/{len(tests)} passed")