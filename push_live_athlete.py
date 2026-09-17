# """
# push_live_athlete.py

# Writes an athlete profile JSON (the same shape generate_athlete_content_llm.py
# produces) DIRECTLY to the live athlete record in DynamoDB -- ATHLETE#<id> /
# PROFILE#META in the SportsData table -- bypassing the review queue and the
# resolve_review_draft() approval step entirely.

# USE THIS ONLY for data you've already manually verified yourself. For
# AI-generated drafts that haven't been reviewed by a human, use the normal
# review-queue flow instead (save_review_draft() -> admin approves via
# resolve_review_draft()) so there's a record of who signed off on it.

# Usage:
#     python push_live_athlete.py path/to/neeraj_chopra.json

# Or import and call push_live_athlete(profile_dict) directly from another
# script/shell.
# """

# import json
# import sys

# import firebase_store


# # Fields that are pipeline/debug metadata, not part of the actual athlete
# # record -- stripped before writing live so they don't pollute the profile.
# _STRIP_FIELDS = {"_retry_log"}


# def push_live_athlete(profile_dict: dict) -> None:
#     athlete_id = profile_dict.get("athlete_id")
#     if not athlete_id:
#         raise ValueError("profile_dict must include 'athlete_id'")

#     data_to_publish = {k: v for k, v in profile_dict.items() if k not in _STRIP_FIELDS}

#     table = firebase_store._sports_data_table()
#     item = {
#         **firebase_store._to_decimal(data_to_publish),
#         "entityId": f"ATHLETE#{athlete_id}",
#         "sk": "PROFILE#META",
#     }
#     table.put_item(Item=item)
#     print(f"✅ Pushed directly to LIVE record: SportsData/ATHLETE#{athlete_id}/PROFILE#META")
#     print("   (review queue was bypassed -- no draft_id, no reviewer on record)")


# def main():
#     if len(sys.argv) != 2:
#         print("Usage: python push_live_athlete.py path/to/profile.json", file=sys.stderr)
#         sys.exit(1)

#     path = sys.argv[1]
#     with open(path, "r", encoding="utf-8") as f:
#         profile_dict = json.load(f)

#     push_live_athlete(profile_dict)


# if __name__ == "__main__":
#     main()





"""
push_live_athlete.py

Pushes ONE reviewed/verified athlete draft JSON (the shape
generate_athlete_content_llm.py produces: athlete_id, sportId, coreInfo,
performance, analytics, record_highlight) directly to MS_Players +
MS_Transactions.

Athletes share the same MS_Players table as regular players (there's no
separate MS_Athletes table in the real DynamoDB setup) -- the profile row
just has an ATHLETE#<id> entityId instead of PLAYER#<id>.

Mirrors push_live_player.py — writes directly, bypassing the review queue,
so only run this on data you've already verified. One athlete per run, by
design (matches how generate_athlete_content_llm.py's interactive mode
drafts one athlete at a time — review one, push one).

This REPLACES the earlier firebase_store-based push_live_athlete.py (which
wrote a single PROFILE#META item straight into the old single-table
SportsData design). That version is superseded now that athletes live in
the multi-sport schema (MS_Players for the universal profile row +
MS_Transactions for per-sport/level-format/season stint stats), same as
push_live_player.py already does for players.

Usage:
    python push_live_athlete.py --athlete-file llm_athlete_drafts/neeraj_chopra.json --level-format-id senior_international
    python push_live_athlete.py --athlete-file llm_athlete_drafts/neeraj_chopra.json --level-format-id senior_international --dry-run
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from dotenv import load_dotenv

# Standalone script (not run via `next dev`) -> needs explicit .env loading,
# same known gotcha as push_live_player.py / push_live_team.py /
# push_squad_players.py. This script doesn't import firebase_store either,
# so nothing else loads it.
load_dotenv()
load_dotenv(".env.local")

MS_PLAYERS_TABLE = os.environ.get("MS_PLAYERS_TABLE", "MS_Players")
MS_TRANSACTIONS_TABLE = os.environ.get("MS_TRANSACTIONS_TABLE", "MS_Transactions")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Pipeline/debug metadata fields that aren't part of the actual athlete
# record -- stripped before writing live so they don't pollute the profile.
_STRIP_FIELDS = {"_retry_log"}


def _to_dynamo_safe(obj):
    """
    Recursively converts Python floats to Decimal (boto3's DynamoDB Table
    resource refuses native float — TypeError: Float types are not
    supported. Use Decimal types instead). json.loads/dumps round-trip
    is the simplest reliable way to walk an arbitrarily-nested dict/list
    from a loaded JSON draft. Same helper as push_live_player.py.
    """
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _to_dynamo_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_dynamo_safe(v) for v in obj]
    return obj


def build_athlete_profile(draft: dict) -> dict:
    core = draft.get("coreInfo", {})
    athlete_id = draft["athlete_id"]
    now = datetime.now(timezone.utc).isoformat()
    return {
        "entityId": f"ATHLETE#{athlete_id}",
        "sk": "PROFILE#META",
        "athleteId": athlete_id,
        "name": core.get("name"),
        "sportId": draft.get("sportId"),
        "dateOfBirth": core.get("dob"),
        "birthPlace": core.get("birthplace"),
        "nationality": core.get("country"),
        "country": core.get("country"),
        "flag": core.get("flag"),
        "gender": core.get("gender"),
        "heightCm": core.get("heightCm"),
        "weightKg": core.get("weightKg"),
        "coachName": core.get("coachName"),
        "bio": core.get("bio"),
        "firstOlympicGames": core.get("firstOlympicGames"),
        "profileImage": core.get("profileImage"),
        "welcomeVideoUrl": core.get("welcomeVideoUrl"),
        "updatedAt": now,
    }


def build_athlete_transaction(draft: dict, level_format_id: str) -> dict:
    athlete_id = draft["athlete_id"]
    sport_id = draft.get("sportId")
    return {
        "entityId": f"ATHLETE#{athlete_id}",
        "sk": f"AFFIL#{sport_id}#{level_format_id}#STATS",
        "athleteId": athlete_id,
        "sportId": sport_id,
        "levelFormatId": level_format_id,
        "performance": draft.get("performance", {}),
        "analytics": draft.get("analytics", {}),
        "recordHighlight": draft.get("record_highlight"),
        "source": {
            "source_name": "Verified and pushed live from a Gemini draft",
            "source_url": "https://sportsfan360.internal/llm-draft",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        },
    }


def push_live_athlete(draft: dict, level_format_id: str, dry_run: bool = False) -> None:
    if "athlete_id" not in draft or "coreInfo" not in draft:
        raise ValueError("draft doesn't look like a valid athlete draft (missing athlete_id/coreInfo)")

    data_to_publish = {k: v for k, v in draft.items() if k not in _STRIP_FIELDS}
    athlete_item = build_athlete_profile(data_to_publish)
    txn_item = build_athlete_transaction(data_to_publish, level_format_id)

    if dry_run:
        print(json.dumps({"MS_Players": athlete_item, "MS_Transactions": txn_item}, indent=2))
        return

    dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
    dynamodb.Table(MS_PLAYERS_TABLE).put_item(Item=_to_dynamo_safe(athlete_item))
    print(f"Wrote profile to {MS_PLAYERS_TABLE}: {athlete_item['entityId']}")

    dynamodb.Table(MS_TRANSACTIONS_TABLE).put_item(Item=_to_dynamo_safe(txn_item))
    print(f"Wrote stats to {MS_TRANSACTIONS_TABLE}: {txn_item['entityId']} / {txn_item['sk']}")
    print("   (review queue was bypassed -- no draft_id, no reviewer on record)")


def main():
    parser = argparse.ArgumentParser(description="Push one verified athlete draft to MS_Athletes + MS_Transactions")
    parser.add_argument("--athlete-file", required=True, help="Path to a single athlete draft JSON")
    parser.add_argument("--level-format-id", required=True, help="e.g. senior_international")
    parser.add_argument("--dry-run", action="store_true", help="Print items instead of writing to DynamoDB")
    args = parser.parse_args()

    with open(args.athlete_file, "r", encoding="utf-8") as f:
        draft = json.load(f)

    try:
        push_live_athlete(draft, args.level_format_id, dry_run=args.dry_run)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()