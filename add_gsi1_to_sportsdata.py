"""
add_gsi1_to_sportsdata.py

One-off migration: adds the missing GSI1 (GSI1PK/GSI1SK) index to the
already-existing SportsData table.

Confirmed via AWS console (2026-08-04) that SportsData currently has 2 GSIs
-- clubId-createdAt-index and clubProfileId-seasonYear-index -- but NOT
GSI1. GSI1 is required by firebase_store.py's review-queue functions:

    list_pending_reviews()   -- queries IndexName="GSI1" directly
    resolve_review_draft()   -- writes GSI1PK/GSI1SK attributes on update

Without this index, list_pending_reviews() raises a ValidationException
(index does not exist) the first time it's called.

setup_dynamodb_tables.py's create_table() can't fix this -- it skips any
table that already exists, and DynamoDB doesn't let you add a GSI at
table-creation time to a table that's already there. This script uses
update_table instead, which is the only way to add a GSI after the fact.

IMPORTANT DYNAMODB CONSTRAINTS
-------------------------------
- You can only submit ONE GSI create/delete per update_table call. This
  script does exactly one, so no looping/batching concern here.
- Adding a GSI to a table with existing data triggers an online backfill.
  The index will show status "CREATING" and is not queryable until it
  reaches "ACTIVE" -- for a small table this is usually under a minute,
  but can take longer depending on item count. This script polls and
  waits.
- This does NOT touch the 2 existing GSIs (clubId-createdAt-index,
  clubProfileId-seasonYear-index) or any existing data -- it strictly
  adds one new index alongside them.
- Safe to re-run: if GSI1 already exists (e.g. someone else added it, or
  a prior run of this script succeeded), it exits without making changes.

USAGE
-----
Same .env as the rest of the project (AWS_ACCESS_KEY_ID,
AWS_SECRET_ACCESS_KEY, AWS_REGION):

    python add_gsi1_to_sportsdata.py
"""

import os
import time

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()

TABLE_NAME = os.getenv("DDB_TABLE_SPORTS_DATA", "SportsData")
GSI_NAME = os.getenv("DDB_SPORTS_DATA_GSI1", "GSI1")
GSI_PK = "GSI1PK"
GSI_SK = "GSI1SK"


def _client():
    region = os.getenv("AWS_REGION") or os.getenv("DDB_REGION")
    if not region:
        raise SystemExit(
            "No AWS_REGION (or DDB_REGION) set in .env — add one (e.g. AWS_REGION=us-east-1) and re-run."
        )
    return boto3.client("dynamodb", region_name=region)


def _existing_gsi_names(ddb, table_name: str) -> set[str]:
    desc = ddb.describe_table(TableName=table_name)["Table"]
    return {gsi["IndexName"] for gsi in desc.get("GlobalSecondaryIndexes", [])}


def _wait_gsi_active(ddb, table_name: str, index_name: str):
    print(f"   waiting for {index_name} to become ACTIVE (backfilling)...", end="", flush=True)
    while True:
        desc = ddb.describe_table(TableName=table_name)["Table"]
        gsis = {g["IndexName"]: g["IndexStatus"] for g in desc.get("GlobalSecondaryIndexes", [])}
        status = gsis.get(index_name)
        if status == "ACTIVE":
            print(" done.")
            return
        print(".", end="", flush=True)
        time.sleep(5)


def main():
    ddb = _client()

    try:
        ddb.describe_table(TableName=TABLE_NAME)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            raise SystemExit(
                f"Table '{TABLE_NAME}' does not exist — run setup_dynamodb_tables.py first."
            )
        raise

    existing = _existing_gsi_names(ddb, TABLE_NAME)
    if GSI_NAME in existing:
        print(f"✅ {GSI_NAME} already exists on {TABLE_NAME} — nothing to do.")
        return

    print(f"Adding {GSI_NAME} ({GSI_PK}/{GSI_SK}) to {TABLE_NAME}...")
    print(f"   existing GSIs left untouched: {sorted(existing) or '(none)'}")

    ddb.update_table(
        TableName=TABLE_NAME,
        AttributeDefinitions=[
            {"AttributeName": GSI_PK, "AttributeType": "S"},
            {"AttributeName": GSI_SK, "AttributeType": "S"},
        ],
        GlobalSecondaryIndexUpdates=[
            {
                "Create": {
                    "IndexName": GSI_NAME,
                    "KeySchema": [
                        {"AttributeName": GSI_PK, "KeyType": "HASH"},
                        {"AttributeName": GSI_SK, "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            }
        ],
    )

    _wait_gsi_active(ddb, TABLE_NAME, GSI_NAME)
    print(f"\n✅ {GSI_NAME} is ACTIVE on {TABLE_NAME}. "
          f"firebase_store.list_pending_reviews()/resolve_review_draft() should now work.")


if __name__ == "__main__":
    main()