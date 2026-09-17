"""
delete_ms_transactions_item.py

One-off helper: delete a single wrong MS_Transactions item by its
entityId + sk (e.g. after pushing with the wrong --level-format-id).

Usage:
    python delete_ms_transactions_item.py \
        --entity-id "PLAYER#abhishek_sharma" \
        --sk "AFFIL#cricket#senior_international#T20#IPL#STATS" \
        --dry-run

Drop --dry-run to actually delete.
"""

import argparse
import os

import boto3
from dotenv import load_dotenv

load_dotenv()
load_dotenv(".env.local")

MS_TRANSACTIONS_TABLE = os.environ.get("MS_TRANSACTIONS_TABLE", "MS_Transactions")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--entity-id", required=True)
    parser.add_argument("--sk", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
    table = dynamodb.Table(MS_TRANSACTIONS_TABLE)

    key = {"entityId": args.entity_id, "sk": args.sk}

    existing = table.get_item(Key=key).get("Item")
    if not existing:
        print(f"No item found at {key} — nothing to delete.")
        return

    print("Found item:")
    print(existing)

    if args.dry_run:
        print("\n[dry-run] would delete the above item")
        return

    table.delete_item(Key=key)
    print(f"\nDeleted {key}")


if __name__ == "__main__":
    main()