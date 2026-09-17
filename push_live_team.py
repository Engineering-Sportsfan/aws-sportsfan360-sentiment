"""
push_live_team.py

Writes a team profile JSON (the same shape generate_team_content_llm.py
produces) DIRECTLY to the live team record in DynamoDB -- bypassing the
review queue entirely. Team data lives in a DIFFERENT place than athlete
data: the separate multi-sport schema (MS_Sports / MS_LevelFormat /
MS_Clubs / MS_Leagues / MS_Players / MS_Transactions), not the SportsData
table push_live_athlete.py writes to. Specifically:

  - MS_Clubs        (Nations/Clubs master table) -> CLUB#<team_id> /
    CLUB#META -- coreInfo fields (identity/bio), flattened onto the item.
  - MS_Transactions (transaction table)          -> CLUB#<team_id> /
    AFFIL#<sportId>#<levelFormatId>#<format>#STATS -- record_highlight
    and analytics combined into ONE item (not two separate rows) under
    that single sk, since they're always read and updated together for a
    team profile -- a player's per-season stints make sense as separate
    rows (each stint is independently addable), but a team's record +
    analytics don't have that same "grows over time" shape, so one
    combined item is simpler to read/write than splitting them.

squad is INTENTIONALLY DROPPED, same as ingest_india_test.ts -- individual
players go through the separate Players master table / athlete pipeline,
never through this script.

USE THIS ONLY for data you've already manually verified yourself. For
AI-generated drafts that haven't been reviewed by a human, use the normal
review-queue flow instead (save_draft_to_dynamodb() in
generate_team_content_llm.py -> admin approves) so there's a record of
who signed off on it.

Usage:
    python push_live_team.py path/to/india_test.json

Or import and call push_live_team(profile_dict) directly from another
script/shell.

Requires AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION in the
environment (same credentials setup.py/ingest scripts already use in this
repo) -- NOT firebase_store, since MS_Clubs/MS_Transactions aren't part
of firebase_store's SportsData table. Unlike push_live_athlete.py, this
script never imports firebase_store, so nothing loads .env automatically
-- .env is loaded explicitly below via python-dotenv.
"""

import json
import sys
from decimal import Decimal
from typing import Any

from dotenv import load_dotenv

load_dotenv()

import boto3

REGION = "us-east-1"
CLUBS_TABLE = "MS_Clubs"
TRANSACTIONS_TABLE = "MS_Transactions"

# Fields that are pipeline/debug metadata, not part of the actual team
# record -- stripped before writing live so they don't pollute the profile.
_STRIP_FIELDS = {"_retry_log", "squad"}

_SPORT_ID_MAP = {"cricket": "SPORT#001"}  # Cricket, Men -- see setup_multisport_tables.ts
_LEVEL_FORMAT_MAP = {
    "SPORT#001#TEST": "LF#004",
    "SPORT#001#ODI": "LF#002",
    "SPORT#001#T20": "LF#001",
}


def _resolve_sport_id(sport_id_raw: str) -> str:
    return _SPORT_ID_MAP.get(sport_id_raw, f"SPORT#{sport_id_raw.upper()}")


def _resolve_level_format_id(sport_entity_id: str, fmt: str) -> str:
    key = f"{sport_entity_id}#{fmt.upper()}"
    return _LEVEL_FORMAT_MAP.get(key, f"LF#{sport_entity_id.replace('SPORT#', '')}_{fmt.upper()}")


def _to_decimal(value: Any) -> Any:
    """Recursively converts floats to Decimal (DynamoDB requires Decimal,
    rejects native float) -- same conversion firebase_store._to_decimal
    does for the SportsData table, reimplemented here since MS_Clubs/
    MS_Transactions are written directly via boto3, not through
    firebase_store."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_decimal(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_decimal(v) for v in value]
    return value


def push_live_team(profile_dict: dict) -> None:
    team_id = profile_dict.get("team_id")
    if not team_id:
        raise ValueError("profile_dict must include 'team_id'")

    import os
    if not os.environ.get("AWS_ACCESS_KEY_ID") or not os.environ.get("AWS_SECRET_ACCESS_KEY"):
        raise RuntimeError(
            "Missing AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY. Check that .env exists "
            "in this repo's root (aws-sportsfan360-sentiment) with both keys set -- the "
            "same file/keys generate_team_content_llm.py's firebase_store import relies on."
        )

    sport_id_raw = profile_dict.get("sportId", "cricket")
    fmt = profile_dict.get("format")
    if not fmt:
        raise ValueError("profile_dict must include 'format'")

    core_info = profile_dict.get("coreInfo") or {}
    record_highlight = profile_dict.get("record_highlight")
    analytics = profile_dict.get("analytics")

    club_entity_id = f"CLUB#{team_id}"
    sport_entity_id = _resolve_sport_id(sport_id_raw)
    level_format_entity_id = _resolve_level_format_id(sport_entity_id, fmt)
    stint_sk = f"AFFIL#{sport_entity_id}#{level_format_entity_id}#{fmt}"

    dynamodb = boto3.resource("dynamodb", region_name=REGION)
    clubs_table = dynamodb.Table(CLUBS_TABLE)
    transactions_table = dynamodb.Table(TRANSACTIONS_TABLE)

    # 1. MS_Clubs -- team identity/bio. Base fields match the schema doc
    #    (clubName/clubType/country); everything else from coreInfo rides
    #    along as extra attributes, same as ingest_india_test.ts does.
    club_item = _to_decimal({
        "entityId": club_entity_id,
        "sk": "CLUB#META",
        "clubName": core_info.get("teamName"),
        "clubType": "National Team",
        "country": core_info.get("country"),
        "flag": core_info.get("flag"),
        "shortName": core_info.get("shortName"),
        "captain": core_info.get("captain"),
        "viceCaptain": core_info.get("viceCaptain"),
        "headCoach": core_info.get("headCoach"),
        "homeGround": core_info.get("homeGround"),
        "founded": core_info.get("founded"),
        "logoUrl": core_info.get("logoUrl"),
        "teamPhotoUrl": core_info.get("teamPhotoUrl"),
        "bio": core_info.get("bio"),
        "sportId": sport_entity_id,
        "levelFormatId": level_format_entity_id,
        "source": profile_dict.get("source"),
    })
    clubs_table.put_item(Item=club_item)
    print(f"✅ Pushed directly to LIVE record: {CLUBS_TABLE}/{club_entity_id}/CLUB#META")

    # 2. MS_Transactions -- record_highlight + analytics combined into ONE
    #    item under a single #STATS sk (previously two separate rows --
    #    combined since a team's record/analytics are always read and
    #    updated together, unlike a player's per-season stints).
    if record_highlight or analytics:
        stats_item = _to_decimal({
            "entityId": club_entity_id,
            "sk": f"{stint_sk}#STATS",
            "sportId": sport_entity_id,
            "levelFormatId": level_format_entity_id,
            "clubId": club_entity_id,
            "format": fmt,
            "record": record_highlight,
            "analytics": analytics,
            "groundingSources": profile_dict.get("grounding_sources", []),
            "GSI1PK": "LEAGUE#NONE",
            "GSI2PK": club_entity_id,
        })
        transactions_table.put_item(Item=stats_item)
        print(f"✅ Pushed directly to LIVE record: {TRANSACTIONS_TABLE}/{club_entity_id}/{stint_sk}#STATS")

    print("   (review queue was bypassed -- no draft_id, no reviewer on record)")
    print("   (squad was NOT written -- players go through the Players table/athlete pipeline separately)")


def main():
    if len(sys.argv) != 2:
        print("Usage: python push_live_team.py path/to/profile.json", file=sys.stderr)
        sys.exit(1)

    path = sys.argv[1]
    with open(path, "r", encoding="utf-8") as f:
        profile_dict = json.load(f)

    push_live_team(profile_dict)


if __name__ == "__main__":
    main()