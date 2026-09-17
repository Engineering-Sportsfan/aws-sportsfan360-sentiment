"""
push_live_players_batch.py

Batch version of push_live_player.py. Instead of one --player-file at a
time, pushes every player draft JSON in a folder (llm_player_drafts/) to
MS_Players + MS_Transactions in one run.

Same write logic as push_live_player.py (build_player_profile /
build_player_transaction, same key shapes), just looped over multiple
files with per-file error handling so one bad draft doesn't stop the
other 59.

Usage:
    python push_live_players_batch.py --player-dir llm_player_drafts --level-format-id senior_test
    python push_live_players_batch.py --player-dir llm_player_drafts --level-format-id senior_test --dry-run
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import boto3
from dotenv import load_dotenv

# Standalone script (not run via `next dev`) -> needs explicit .env loading,
# same known gotcha as push_live_player.py / push_live_team.py.
load_dotenv()
load_dotenv(".env.local")

MS_PLAYERS_TABLE = os.environ.get("MS_PLAYERS_TABLE", "MS_Players")
MS_TRANSACTIONS_TABLE = os.environ.get("MS_TRANSACTIONS_TABLE", "MS_Transactions")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")


def _to_dynamo_safe(obj):
    """Recursively convert Python floats to Decimal (boto3's DynamoDB Table
    resource refuses native float)."""
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
    now = datetime.now(timezone.utc).isoformat()
    return {
        "entityId": f"PLAYER#{player_id}",
        "sk": "PROFILE#META",
        "playerId": player_id,
        "name": core.get("name"),
        "role": core.get("role"),
        "battingStyle": core.get("battingStyle"),
        "bowlingStyle": core.get("bowlingStyle"),
        "isCaptain": bool(core.get("isCaptain", False)),
        "currentClubId": draft.get("currentClubId"),
        "sportId": draft.get("sportId", "cricket"),
        "format": draft.get("format", "Test"),
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
    return {
        "entityId": f"PLAYER#{player_id}",
        "sk": f"AFFIL#{sport_id}#{level_format_id}#{fmt}#STATS",
        "playerId": player_id,
        "clubId": draft.get("currentClubId"),
        "sportId": sport_id,
        "levelFormatId": level_format_id,
        "format": fmt,
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


def push_one(dynamodb, path: Path, level_format_id: str, dry_run: bool) -> tuple[bool, str]:
    """Returns (success, message)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            draft = json.load(f)
    except UnicodeDecodeError:
        # Some drafts were saved with Windows-1252 smart quotes/dashes
        # instead of UTF-8 (e.g. byte 0x97 = an em dash). Retry once
        # with cp1252 before giving up on this file.
        try:
            with open(path, "r", encoding="cp1252") as f:
                draft = json.load(f)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            return False, f"could not read/parse file even as cp1252: {e}"
    except (json.JSONDecodeError, OSError) as e:
        return False, f"could not read/parse file: {e}"

    if "playerId" not in draft or "coreInfo" not in draft:
        return False, "missing playerId/coreInfo — doesn't look like a valid draft"

    try:
        player_item = build_player_profile(draft)
        txn_item = build_player_transaction(draft, level_format_id)
    except Exception as e:
        return False, f"failed building items: {e}"

    if dry_run:
        return True, f"[dry-run] would write {player_item['entityId']}"

    try:
        dynamodb.Table(MS_PLAYERS_TABLE).put_item(Item=_to_dynamo_safe(player_item))
        dynamodb.Table(MS_TRANSACTIONS_TABLE).put_item(Item=_to_dynamo_safe(txn_item))
    except Exception as e:
        return False, f"DynamoDB write failed: {e}"

    return True, f"wrote {player_item['entityId']} (profile + stats)"


def main():
    parser = argparse.ArgumentParser(description="Push many verified player drafts to MS_Players + MS_Transactions")
    parser.add_argument("--player-dir", required=True, help="Folder of player draft JSON files (e.g. llm_player_drafts)")
    parser.add_argument("--level-format-id", required=True, help="e.g. senior_test")
    parser.add_argument("--dry-run", action="store_true", help="Print items instead of writing to DynamoDB")
    args = parser.parse_args()

    folder = Path(args.player_dir)
    if not folder.is_dir():
        print(f"Not a folder: {folder}", file=sys.stderr)
        sys.exit(1)

    files = sorted(folder.glob("*.json"))
    if not files:
        print(f"No .json files found in {folder}", file=sys.stderr)
        sys.exit(1)

    dynamodb = None
    if not args.dry_run:
        dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)

    print(f"Found {len(files)} draft files in {folder}\n")

    successes = []
    failures = []

    for path in files:
        ok, msg = push_one(dynamodb, path, args.level_format_id, args.dry_run)
        tag = "OK " if ok else "FAIL"
        print(f"[{tag}] {path.name}: {msg}")
        (successes if ok else failures).append((path.name, msg))

    print("\n--- Summary ---")
    print(f"Succeeded: {len(successes)}/{len(files)}")
    if failures:
        print(f"Failed: {len(failures)}")
        for name, msg in failures:
            print(f"  - {name}: {msg}")
        sys.exit(1)


if __name__ == "__main__":
    main()