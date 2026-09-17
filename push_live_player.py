# """
# push_live_player.py

# Pushes ONE reviewed/verified player draft JSON (the shape generate_player_content_llm.py
# produces: playerId, sportId, format, currentClubId, coreInfo, record_highlight,
# analytics) directly to MS_Players + MS_Transactions.

# Mirrors push_live_team.py — writes directly, bypassing the review queue, so
# only run this on data you've already verified. One player per run, by design
# (matches how generate_player_content_llm.py's interactive mode drafts one
# player at a time — review one, push one).

# Usage:
#     python push_live_player.py --player-file llm_player_drafts/shubman_gill.json --level-format-id senior_test
#     python push_live_player.py --player-file llm_player_drafts/shubman_gill.json --level-format-id senior_test --dry-run
# """

# import argparse
# import json
# import os
# import sys
# from datetime import datetime, timezone
# from decimal import Decimal

# import boto3
# from dotenv import load_dotenv

# # Standalone script (not run via `next dev`) -> needs explicit .env loading,
# # same known gotcha as push_live_team.py / push_squad_players.py. This
# # script doesn't import firebase_store either, so nothing else loads it.
# load_dotenv()
# load_dotenv(".env.local")

# MS_PLAYERS_TABLE = os.environ.get("MS_PLAYERS_TABLE", "MS_Players")
# MS_TRANSACTIONS_TABLE = os.environ.get("MS_TRANSACTIONS_TABLE", "MS_Transactions")
# AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")


# def _to_dynamo_safe(obj):
#     """
#     Recursively converts Python floats to Decimal (boto3's DynamoDB Table
#     resource refuses native float — TypeError: Float types are not
#     supported. Use Decimal types instead). json.loads/dumps round-trip
#     is the simplest reliable way to walk an arbitrarily-nested dict/list
#     from a loaded JSON draft.
#     """
#     if isinstance(obj, float):
#         return Decimal(str(obj))
#     if isinstance(obj, dict):
#         return {k: _to_dynamo_safe(v) for k, v in obj.items()}
#     if isinstance(obj, list):
#         return [_to_dynamo_safe(v) for v in obj]
#     return obj


# def build_player_profile(draft: dict) -> dict:
#     core = draft.get("coreInfo", {})
#     player_id = draft["playerId"]
#     now = datetime.now(timezone.utc).isoformat()
#     return {
#         "entityId": f"PLAYER#{player_id}",
#         "sk": "PROFILE#META",
#         "playerId": player_id,
#         "name": core.get("name"),
#         "role": core.get("role"),
#         "battingStyle": core.get("battingStyle"),
#         "bowlingStyle": core.get("bowlingStyle"),
#         "isCaptain": bool(core.get("isCaptain", False)),
#         "currentClubId": draft.get("currentClubId"),
#         "sportId": draft.get("sportId", "cricket"),
#         "format": draft.get("format", "Test"),
#         "testCaps": core.get("testCaps"),
#         "dateOfBirth": core.get("dateOfBirth"),
#         "birthPlace": core.get("birthPlace"),
#         "heightCm": core.get("heightCm"),
#         "jerseyNo": core.get("jerseyNo"),
#         "debutDate": core.get("debutDate"),
#         "profileImage": core.get("profileImage"),
#         "bio": core.get("bio"),
#         "country": core.get("country"),
#         "flag": core.get("flag"),
#         "updatedAt": now,
#     }


# def build_player_transaction(draft: dict, level_format_id: str) -> dict:
#     player_id = draft["playerId"]
#     sport_id = draft.get("sportId", "cricket")
#     fmt = draft.get("format", "Test")
#     return {
#         "entityId": f"PLAYER#{player_id}",
#         "sk": f"AFFIL#{sport_id}#{level_format_id}#{fmt}#STATS",
#         "playerId": player_id,
#         "clubId": draft.get("currentClubId"),
#         "sportId": sport_id,
#         "levelFormatId": level_format_id,
#         "format": fmt,
#         "battingStats": draft.get("analytics", {}).get("battingStats"),
#         "bowlingStats": draft.get("analytics", {}).get("bowlingStats"),
#         "seasonalData": draft.get("analytics", {}).get("seasonalData", []),
#         "recordHighlight": draft.get("record_highlight"),
#         "source": {
#             "source_name": "Verified and pushed live from a Gemini draft",
#             "source_url": "https://sportsfan360.internal/llm-draft",
#             "fetched_at": datetime.now(timezone.utc).isoformat(),
#         },
#     }


# def main():
#     parser = argparse.ArgumentParser(description="Push one verified player draft to MS_Players + MS_Transactions")
#     parser.add_argument("--player-file", required=True, help="Path to a single player draft JSON")
#     parser.add_argument("--level-format-id", required=True, help="e.g. senior_test")
#     parser.add_argument("--dry-run", action="store_true", help="Print items instead of writing to DynamoDB")
#     args = parser.parse_args()

#     with open(args.player_file, "r", encoding="utf-8") as f:
#         draft = json.load(f)

#     if "playerId" not in draft or "coreInfo" not in draft:
#         print("Player file doesn't look like a valid draft (missing playerId/coreInfo).", file=sys.stderr)
#         sys.exit(1)

#     player_item = build_player_profile(draft)
#     txn_item = build_player_transaction(draft, args.level_format_id)

#     if args.dry_run:
#         print(json.dumps({"MS_Players": player_item, "MS_Transactions": txn_item}, indent=2))
#         return

#     dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
#     dynamodb.Table(MS_PLAYERS_TABLE).put_item(Item=_to_dynamo_safe(player_item))
#     print(f"Wrote profile to {MS_PLAYERS_TABLE}: {player_item['entityId']}")

#     dynamodb.Table(MS_TRANSACTIONS_TABLE).put_item(Item=_to_dynamo_safe(txn_item))
#     print(f"Wrote stats to {MS_TRANSACTIONS_TABLE}: {txn_item['entityId']} / {txn_item['sk']}")


# if __name__ == "__main__":
#     main()





"""
push_live_player.py

Pushes ONE reviewed/verified player draft JSON to MS_Players + MS_Transactions.
One player+format per run — run it twice per player (once for the T20I
draft, once for the IPL draft).

CHANGES vs the original:
  - build_player_profile()'s sk now includes `tournament` (e.g. "T20I" /
    "IPL"), not just `format` — your T20I and IPL drafts both have
    format="T20", so keying only on format would make the IPL push
    overwrite the T20I profile item. sk is now "PROFILE#META#<tournament>"
    (falls back to format if tournament is missing, e.g. for non-cricket
    or Test-cricket drafts that don't set it).
  - Added coreInfo.gender onto the profile item.
  - build_player_transaction() sk now also includes tournament, so T20I
    and IPL stats land as separate MS_Transactions items.

Usage:
    python push_live_player.py --player-file llm_cricketplayers_drafts/jasprit_bumrah_t20i.json --level-format-id senior_international
    python push_live_player.py --player-file llm_cricketplayers_drafts/jasprit_bumrah_ipl.json --level-format-id domestic_t20
    (add --dry-run to preview without writing)
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from dotenv import load_dotenv

load_dotenv()
load_dotenv(".env.local")

MS_PLAYERS_TABLE = os.environ.get("MS_PLAYERS_TABLE", "MS_Players")
MS_TRANSACTIONS_TABLE = os.environ.get("MS_TRANSACTIONS_TABLE", "MS_Transactions")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")


def _to_dynamo_safe(obj):
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _to_dynamo_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_dynamo_safe(v) for v in obj]
    return obj


def build_player_profile(draft: dict) -> dict:
    core = draft.get("coreInfo", {})
    player_id = draft["playerId"]
    fmt = draft.get("format", "Test")
    tournament = draft.get("tournament")
    sk_key = tournament or fmt
    now = datetime.now(timezone.utc).isoformat()
    return {
        "entityId": f"PLAYER#{player_id}",
        "sk": f"PROFILE#META#{sk_key}",
        "playerId": player_id,
        "name": core.get("name"),
        "gender": core.get("gender"),
        "role": core.get("role"),
        "battingStyle": core.get("battingStyle"),
        "bowlingStyle": core.get("bowlingStyle"),
        "isCaptain": bool(core.get("isCaptain", False)),
        "currentClubId": draft.get("currentClubId"),
        "sportId": draft.get("sportId", "cricket"),
        "format": fmt,
        "tournament": tournament,
        "testCaps": core.get("testCaps"),
        "dateOfBirth": core.get("dateOfBirth"),
        "birthPlace": core.get("birthPlace"),
        "heightCm": core.get("heightCm"),
        "jerseyNo": core.get("jerseyNo"),
        "debutDate": core.get("debutDate"),
        "profileImage": core.get("profileImage"),
        "bio": core.get("bio"),
        "country": core.get("country"),
        "flag": core.get("flag"),
        "updatedAt": now,
    }


def build_player_transaction(draft: dict, level_format_id: str) -> dict:
    player_id = draft["playerId"]
    sport_id = draft.get("sportId", "cricket")
    fmt = draft.get("format", "Test")
    tournament = draft.get("tournament")
    sk_suffix = f"{fmt}#{tournament}" if tournament else fmt
    return {
        "entityId": f"PLAYER#{player_id}",
        "sk": f"AFFIL#{sport_id}#{level_format_id}#{sk_suffix}#STATS",
        "playerId": player_id,
        "clubId": draft.get("currentClubId"),
        "sportId": sport_id,
        "levelFormatId": level_format_id,
        "format": fmt,
        "tournament": tournament,
        "battingStats": draft.get("analytics", {}).get("battingStats"),
        "bowlingStats": draft.get("analytics", {}).get("bowlingStats"),
        "seasonalData": draft.get("analytics", {}).get("seasonalData", []),
        "recordHighlight": draft.get("record_highlight"),
        "source": {
            "source_name": "Verified and pushed live from a Gemini draft",
            "source_url": "https://sportsfan360.internal/llm-draft",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Push one verified player draft to MS_Players + MS_Transactions")
    parser.add_argument("--player-file", required=True, help="Path to a single player draft JSON")
    parser.add_argument("--level-format-id", required=True, help="e.g. senior_international, domestic_t20")
    parser.add_argument("--dry-run", action="store_true", help="Print items instead of writing to DynamoDB")
    args = parser.parse_args()

    try:
        with open(args.player_file, "r", encoding="utf-8") as f:
            draft = json.load(f)
    except UnicodeDecodeError:
        with open(args.player_file, "r", encoding="cp1252") as f:
            draft = json.load(f)

    if "playerId" not in draft or "coreInfo" not in draft:
        print("Player file doesn't look like a valid draft (missing playerId/coreInfo).", file=sys.stderr)
        sys.exit(1)

    player_item = build_player_profile(draft)
    txn_item = build_player_transaction(draft, args.level_format_id)

    if args.dry_run:
        print(json.dumps({"MS_Players": player_item, "MS_Transactions": txn_item}, indent=2))
        return

    dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
    dynamodb.Table(MS_PLAYERS_TABLE).put_item(Item=_to_dynamo_safe(player_item))
    print(f"Wrote profile to {MS_PLAYERS_TABLE}: {player_item['entityId']} / {player_item['sk']}")

    dynamodb.Table(MS_TRANSACTIONS_TABLE).put_item(Item=_to_dynamo_safe(txn_item))
    print(f"Wrote stats to {MS_TRANSACTIONS_TABLE}: {txn_item['entityId']} / {txn_item['sk']}")


if __name__ == "__main__":
    main()