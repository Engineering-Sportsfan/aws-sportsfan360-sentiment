"""
setup_dynamodb_tables.py

Creates the 6 MASTER tables described in the SportsFan360 Master Database
Architecture handbook — this is a single-table-per-domain design, not one
table per feature. Every dataset (athletes, matches, posts, chat, wallet,
users, config, etc.) routes into one of these 6 tables via a prefixed
PK/SK key pattern (see the handbook's routing matrix, e.g.
entityId="ATHLETE#<id>", sk="PROFILE#META").

    SportsData             PK: entityId   SK: sk   (athletes, records, matches, innings, clubs, stats)
    SocialAndContent       PK: contentId  SK: sk   (ROAR posts, news, spotlight banners, comments)
    RealTimeChat           PK: roomId     SK: sk   (watch-along chat, HostRooms, Ask-AI sessions, DMs)
    GamificationAndWallet  PK: userId     SK: sk   (points wallet, predictions/quizzes, store, FanBattle)
    UserData               PK: userId     SK: sk   (user profiles, admin accounts, follow graph)
    SystemAndConfig        PK: configId   SK: sk   (promo banners, feature flags, automations)

There is ONE DynamoDB used across the whole project — all writes and reads
go through these 6 tables, never a new table per feature/service.

This SUPERSEDES an earlier version of this script that created 5 separate
tables (sentimentReports, athletes, athleteFingerprints, athleteReviewQueue,
athleteReviewAuditLog) matching firebase_store.py's current code — that
code predates/doesn't yet follow this handbook's single-table design and
needs migrating separately; do not run that old script against this
account, and don't treat its 5 tables as correct going forward.

Uses PAY_PER_REQUEST billing (on-demand) — no capacity to size or pay for
up front. Safe to re-run: skips any table that already exists.

USAGE
-----
1. .env (same file every repo's load_dotenv() reads, per the handbook):

       AWS_ACCESS_KEY_ID=...
       AWS_SECRET_ACCESS_KEY=...
       AWS_REGION=us-east-1

2. Run:

       python setup_dynamodb_tables.py

No new packages needed beyond boto3 + python-dotenv (already installed
per the handbook — "Zero Extra Tools Required").
"""

import os
import time

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()

# Table name -> partition key attribute name. Sort key is always "sk" on
# every one of these 6 (per the handbook), so it's not part of this map.
MASTER_TABLES = {
    "SportsData": "entityId",
    "SocialAndContent": "contentId",
    "RealTimeChat": "roomId",
    "GamificationAndWallet": "userId",
    "UserData": "userId",
    "SystemAndConfig": "configId",
}

SORT_KEY = "sk"


def _client():
    region = os.getenv("AWS_REGION") or os.getenv("DDB_REGION")
    if not region:
        raise SystemExit(
            "No AWS_REGION (or DDB_REGION) set in .env — add one (e.g. AWS_REGION=us-east-1) and re-run."
        )
    return boto3.client("dynamodb", region_name=region)


def _table_exists(ddb, name: str) -> bool:
    try:
        ddb.describe_table(TableName=name)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return False
        raise


def _wait_active(ddb, name: str):
    print(f"   waiting for {name} to become ACTIVE...", end="", flush=True)
    while True:
        status = ddb.describe_table(TableName=name)["Table"]["TableStatus"]
        if status == "ACTIVE":
            print(" done.")
            return
        print(".", end="", flush=True)
        time.sleep(2)


def _create_master_table(ddb, name: str, pk_name: str, extra_gsi: list | None = None):
    """PK + SK ('sk'), both String — the shape used by all 6 master tables.
    extra_gsi, if given, is a list of (index_name, gsi_pk_attr, gsi_sk_attr)
    tuples to add as generic single-table-design GSIs (e.g. SportsData's
    GSI1 for the review-queue status-lookup pattern)."""
    if _table_exists(ddb, name):
        print(f"✅ {name} already exists — skipping.")
        return
    print(f"Creating {name} (PK: {pk_name}, SK: {SORT_KEY})" + (f" + GSI(s): {[g[0] for g in extra_gsi]}" if extra_gsi else "") + "...")

    attr_defs = [
        {"AttributeName": pk_name, "AttributeType": "S"},
        {"AttributeName": SORT_KEY, "AttributeType": "S"},
    ]
    kwargs = {}
    if extra_gsi:
        gsis = []
        for index_name, gsi_pk, gsi_sk in extra_gsi:
            attr_defs.append({"AttributeName": gsi_pk, "AttributeType": "S"})
            attr_defs.append({"AttributeName": gsi_sk, "AttributeType": "S"})
            gsis.append({
                "IndexName": index_name,
                "KeySchema": [
                    {"AttributeName": gsi_pk, "KeyType": "HASH"},
                    {"AttributeName": gsi_sk, "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            })
        kwargs["GlobalSecondaryIndexes"] = gsis

    ddb.create_table(
        TableName=name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=attr_defs,
        KeySchema=[
            {"AttributeName": pk_name, "KeyType": "HASH"},
            {"AttributeName": SORT_KEY, "KeyType": "RANGE"},
        ],
        **kwargs,
    )
    _wait_active(ddb, name)


# SportsData needs one generic GSI ("GSI1") beyond its base PK/SK — used by
# the athlete-review-queue status lookup (list drafts pending_review,
# newest first) that used to be a dedicated GSI on its own
# athleteReviewQueue table. Standard single-table-design technique: GSI1PK
# holds a query-pattern-specific value (e.g. "REVIEW_STATUS#pending_review"),
# GSI1SK holds the sort value (created_at). Add more (GSI2, GSI3, ...) here
# the same way if another cross-entity query pattern comes up later.
SPORTS_DATA_GSIS = [("GSI1", "GSI1PK", "GSI1SK")]


def main():
    ddb = _client()
    print("Connected to DynamoDB. Setting up the 6 master tables...\n")

    for name, pk_name in MASTER_TABLES.items():
        extra_gsi = SPORTS_DATA_GSIS if name == "SportsData" else None
        _create_master_table(ddb, name, pk_name, extra_gsi=extra_gsi)

    print("\n✅ All 6 master tables ready:")
    for name, pk_name in MASTER_TABLES.items():
        print(f"   - {name}  (PK: {pk_name}, SK: {SORT_KEY})")
    print(
        "\nEverything routes through these 6 tables via prefixed PK/SK values "
        "(e.g. entityId='ATHLETE#<id>', sk='PROFILE#META') — no new tables "
        "get created for new features."
    )


if __name__ == "__main__":
    main()