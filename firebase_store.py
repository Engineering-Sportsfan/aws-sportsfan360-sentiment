# firebase_store.py

import os
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key, Attr
from dotenv import load_dotenv

load_dotenv()

# ── MIGRATION NOTE ────────────────────────────────────────────────────────
# Per the SportsFan360 Master Database Architecture handbook: there is ONE
# DynamoDB, single-table design, 6 master tables total — no table per
# feature. This module used to create/use 5 of its own dedicated tables
# (sentimentReports, athletes, athleteFingerprints, athleteReviewQueue,
# athleteReviewAuditLog); everything below now lives in SportsData instead,
# via prefixed entityId/sk key patterns, matching how the handbook already
# stores ATHLETE#<id>/PROFILE#META etc. Nothing here writes to a table of
# its own anymore.
#
# Key patterns used by this module (SportsData: PK entityId, SK sk):
#   Sentiment report   entityId="REPORT#<sport>"          sk="REPORT#<timestamp>"
#   Athlete profile     entityId="ATHLETE#<id>"            sk="PROFILE#META"        (unchanged — matches handbook's own example)
#   Athlete fingerprint entityId="ATHLETE#<id>"            sk="FINGERPRINT#META"
#   Review draft        entityId="REVIEW#<draft_id>"       sk="REVIEW#META"
#                        GSI1PK="REVIEW_STATUS#<status>"   GSI1SK="<created_at>#<draft_id>"
#   Audit log entry      entityId="ATHLETE#<athlete_id>"    sk="AUDIT#<timestamp>#<log_id>"
#
# The GSI1 lookup (status -> drafts, newest first) replaces the old
# dedicated status-created_at-index on athleteReviewQueue — see
# setup_dynamodb_tables.py, which now provisions a generic GSI1 on
# SportsData for exactly this kind of cross-entity query pattern.
# ────────────────────────────────────────────────────────────────────────

TABLE_SPORTS_DATA = os.getenv("DDB_TABLE_SPORTS_DATA", "SportsData")
SPORTS_DATA_GSI1 = os.getenv("DDB_SPORTS_DATA_GSI1", "GSI1")

_dynamodb = None


def init_dynamo():
    """Returns a cached boto3 DynamoDB resource, region from env or default chain."""
    global _dynamodb
    if _dynamodb is None:
        region = os.getenv("AWS_REGION") or os.getenv("DDB_REGION")
        _dynamodb = boto3.resource("dynamodb", region_name=region) if region else boto3.resource("dynamodb")
    return _dynamodb


def _sports_data_table():
    return init_dynamo().Table(TABLE_SPORTS_DATA)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _to_decimal(value):
    """DynamoDB has no native float type — recursively convert floats to Decimal."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_decimal(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_decimal(v) for v in value]
    return value


def _from_decimal(value):
    """Convert Decimal back to int/float for normal Python use on the way out."""
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, dict):
        return {k: _from_decimal(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_decimal(v) for v in value]
    return value


def _strip_keys(item: dict, *keys) -> dict:
    """Drop internal key-pattern attributes (entityId/sk/GSI1PK/GSI1SK)
    before handing an item back to calling code, so callers see the same
    plain shape they did when each dataset had its own dedicated table."""
    return {k: v for k, v in item.items() if k not in keys}


def _report_sport_key(sport: str) -> str:
    return "FIFA_WC_2026" if sport == "FIFA_WC_2026" else "WT20W"


# ── Sentiment reports ────────────────────────────────────────────────────
# entityId="REPORT#<sport_key>"  sk="REPORT#<timestamp>"

def save_report(report: dict, sport: str = "FIFA_WC_2026"):
    if not report:
        print("⚠️ Failed to save report: Report is empty or None.")
        return None

    table = _sports_data_table()
    sport_key = _report_sport_key(sport)

    # Timestamp in IST (UTC+5:30), used in the sort key (matches old Firestore doc id)
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    timestamp = datetime.now(ist_tz).strftime("%Y-%m-%d_%H-%M-%S")

    item = {
        "entityId": f"REPORT#{sport_key}",
        "sk": f"REPORT#{timestamp}",
        "sport": sport_key,
        "timestamp": timestamp,
        "generated_at": _now_iso(),
        "disclaimer": "Answers are AI-generated. SportsFan360 does not claim accuracy of this content.",
        "report": _to_decimal(report),
    }

    table.put_item(Item=item)
    print(f"✅ Report saved to DynamoDB: {TABLE_SPORTS_DATA}/REPORT#{sport_key}/REPORT#{timestamp}")

    return timestamp


def get_latest_report(sport: str = "FIFA_WC_2026"):
    table = _sports_data_table()
    resp = table.query(
        KeyConditionExpression=Key("entityId").eq(f"REPORT#{_report_sport_key(sport)}"),
        ScanIndexForward=False,  # descending by sort key (sk, which embeds timestamp)
        Limit=1,
    )
    items = resp.get("Items", [])
    if not items:
        return None
    return _strip_keys(_from_decimal(items[0]), "entityId", "sk")


def list_reports(sport: str = "FIFA_WC_2026", limit: int = 50):
    table = _sports_data_table()
    resp = table.query(
        KeyConditionExpression=Key("entityId").eq(f"REPORT#{_report_sport_key(sport)}"),
        ScanIndexForward=False,
        Limit=limit,
    )
    return [item["timestamp"] for item in resp.get("Items", [])]


def get_report(sport: str = "FIFA_WC_2026", timestamp: str = None):
    if not timestamp:
        return None
    table = _sports_data_table()
    resp = table.get_item(Key={
        "entityId": f"REPORT#{_report_sport_key(sport)}",
        "sk": f"REPORT#{timestamp}",
    })
    item = resp.get("Item")
    if not item:
        return None
    return _strip_keys(_from_decimal(item), "entityId", "sk")


# ── Athlete Data Automation pipeline ────────────────────────────────────
# Key patterns (all in SportsData):
#   Athlete profile      entityId="ATHLETE#<id>"       sk="PROFILE#META"
#   Athlete fingerprint  entityId="ATHLETE#<id>"       sk="FINGERPRINT#META"
#   Review draft         entityId="REVIEW#<draft_id>"  sk="REVIEW#META"
#   Audit log entry       entityId="ATHLETE#<id>"       sk="AUDIT#<timestamp>#<log_id>"

def list_automated_athletes():
    """
    Athletes onboarded into the automation pipeline (has `automation_enabled`
    set on their live record). Used by the scheduled EventBridge sweep to
    know who to recheck.

    Note: this scans SportsData filtering on entityId prefix + sk +
    automation_enabled. If SportsData grows large, this scan gets more
    expensive over time — an automation_enabled-keyed GSI (GSI2, following
    the same pattern GSI1 uses for review status) would be the fix if this
    becomes a bottleneck, same tradeoff the original single-table version
    of this function already called out.
    """
    table = _sports_data_table()
    items = []
    scan_kwargs = {
        "FilterExpression": (
            Attr("entityId").begins_with("ATHLETE#")
            & Attr("sk").eq("PROFILE#META")
            & Attr("automation_enabled").eq(True)
        )
    }
    while True:
        resp = table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return [_strip_keys(_from_decimal(item), "entityId", "sk") | {"id": item["entityId"].split("#", 1)[1]} for item in items]


def get_live_athlete(athlete_id: str):
    table = _sports_data_table()
    resp = table.get_item(Key={"entityId": f"ATHLETE#{athlete_id}", "sk": "PROFILE#META"})
    item = resp.get("Item")
    if not item:
        return None
    return _strip_keys(_from_decimal(item), "entityId", "sk")


def get_athlete_fingerprint(athlete_id: str):
    table = _sports_data_table()
    resp = table.get_item(Key={"entityId": f"ATHLETE#{athlete_id}", "sk": "FINGERPRINT#META"})
    item = resp.get("Item")
    return item.get("fingerprint") if item else None


def save_athlete_fingerprint(athlete_id: str, fingerprint: str):
    table = _sports_data_table()
    table.put_item(Item={
        "entityId": f"ATHLETE#{athlete_id}",
        "sk": "FINGERPRINT#META",
        "athlete_id": athlete_id,
        "fingerprint": fingerprint,
        "updated_at": _now_iso(),
    })


def save_review_draft(draft: dict) -> str:
    """Writes an AI-drafted proposal to the review queue. Returns the new item's draft_id."""
    if not draft:
        print("⚠️ Failed to save review draft: draft is empty or None.")
        return None
    table = _sports_data_table()
    draft_id = str(uuid.uuid4())
    created_at = _now_iso()
    status = draft.get("status", "pending_review")
    item = {
        **_to_decimal(draft),
        "entityId": f"REVIEW#{draft_id}",
        "sk": "REVIEW#META",
        "draft_id": draft_id,
        "status": status,
        "created_at": created_at,
        "GSI1PK": f"REVIEW_STATUS#{status}",
        "GSI1SK": f"{created_at}#{draft_id}",
    }
    table.put_item(Item=item)
    print(f"✅ Draft saved to review queue: {TABLE_SPORTS_DATA}/REVIEW#{draft_id}")
    return draft_id


def _with_id_alias(item: dict) -> dict:
    """
    Back-compat shim: Firestore review-queue drafts used to carry the doc id
    under "id"; the item's own identifier is "draft_id". Keep both keys
    populated so existing frontend code reading draft.id doesn't break.
    Remove this once the admin panel is updated to use draft_id directly.
    """
    if item and "draft_id" in item:
        item = {**item, "id": item["draft_id"]}
    return item


def list_pending_reviews(sport: str = None, limit: int = 50):
    table = _sports_data_table()
    query_kwargs = {
        "IndexName": SPORTS_DATA_GSI1,
        "KeyConditionExpression": Key("GSI1PK").eq("REVIEW_STATUS#pending_review"),
        "ScanIndexForward": False,  # descending by GSI1SK (created_at#draft_id)
        "Limit": limit,
    }
    if sport:
        query_kwargs["FilterExpression"] = Attr("sport").eq(sport)
    resp = table.query(**query_kwargs)
    return [_with_id_alias(_strip_keys(_from_decimal(item), "entityId", "sk", "GSI1PK", "GSI1SK")) for item in resp.get("Items", [])]


def get_review_draft(draft_id: str):
    table = _sports_data_table()
    resp = table.get_item(Key={"entityId": f"REVIEW#{draft_id}", "sk": "REVIEW#META"})
    item = resp.get("Item")
    if not item:
        return None
    return _with_id_alias(_strip_keys(_from_decimal(item), "entityId", "sk", "GSI1PK", "GSI1SK"))


def resolve_review_draft(draft_id: str, decision: str, reviewed_by: str, final_data: dict = None):
    """
    decision: 'approved' | 'rejected' | 'edited'
    On approval (or edited-then-approved), writes final_data to the live
    athlete record and appends an audit-log entry. Nothing reaches the live
    profile without going through this function.
    """
    table = _sports_data_table()
    draft_resp = table.get_item(Key={"entityId": f"REVIEW#{draft_id}", "sk": "REVIEW#META"})
    draft_item = draft_resp.get("Item")
    if not draft_item:
        print(f"⚠️ Review draft not found: {draft_id}")
        return False

    draft_data = _from_decimal(draft_item)
    reviewed_at = _now_iso()

    table.update_item(
        Key={"entityId": f"REVIEW#{draft_id}", "sk": "REVIEW#META"},
        UpdateExpression="SET #s = :status, reviewed_by = :reviewed_by, reviewed_at = :reviewed_at, GSI1PK = :gsi1pk",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":status": decision,
            ":reviewed_by": reviewed_by,
            ":reviewed_at": reviewed_at,
            ":gsi1pk": f"REVIEW_STATUS#{decision}",  # keep GSI1 consistent with the new status
        },
    )

    if decision in ("approved", "edited"):
        athlete_id = draft_data["athlete_id"]
        data_to_publish = final_data or draft_data.get("proposed_data", {})

        # Firestore's set(..., merge=True) merged top-level keys; replicate that
        # with a top-level UpdateExpression rather than overwriting the item.
        update_fields = _to_decimal(data_to_publish)
        expr_names = {f"#k{i}": key for i, key in enumerate(update_fields)}
        expr_values = {f":v{i}": value for i, value in enumerate(update_fields.values())}
        set_clause = ", ".join(f"#k{i} = :v{i}" for i in range(len(update_fields)))

        if update_fields:
            table.update_item(
                Key={"entityId": f"ATHLETE#{athlete_id}", "sk": "PROFILE#META"},
                UpdateExpression="SET " + set_clause,
                ExpressionAttributeNames=expr_names,
                ExpressionAttributeValues=expr_values,
            )
        save_athlete_fingerprint(athlete_id, draft_data.get("fingerprint"))

    log_id = str(uuid.uuid4())
    timestamp = _now_iso()
    athlete_id_for_log = draft_data.get("athlete_id", "unknown")
    table.put_item(Item={
        "entityId": f"ATHLETE#{athlete_id_for_log}",
        "sk": f"AUDIT#{timestamp}#{log_id}",
        "log_id": log_id,
        "draft_id": draft_id,
        "athlete_id": draft_data.get("athlete_id"),
        "decision": decision,
        "reviewed_by": reviewed_by,
        "timestamp": timestamp,
    })
    print(f"✅ Review draft {draft_id} resolved: {decision} by {reviewed_by}")
    return True


if __name__ == "__main__":
    print("Testing DynamoDB connection...")
    ddb = init_dynamo()
    print("✅ DynamoDB client created. Table in use:")
    print(f"   SportsData table = {TABLE_SPORTS_DATA} (GSI1 = {SPORTS_DATA_GSI1})")