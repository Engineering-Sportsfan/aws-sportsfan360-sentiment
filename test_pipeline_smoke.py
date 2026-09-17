# test_pipeline_smoke.py
from athlete_pipeline import run_athlete_pipeline

result = run_athlete_pipeline(
    athlete_id="test_gulveer_001",
    sport="track_field",
    athlete_source_id="15023839",       # Gulveer Singh's World Athletics ID
    is_new_athlete=True,
)

print(result.to_dict())