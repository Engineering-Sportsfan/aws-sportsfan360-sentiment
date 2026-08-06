import os
import time
from datetime import datetime, timezone, timedelta
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv
from dynamodb_store import get_table
from boto3.dynamodb.conditions import Key

load_dotenv()

def init_firebase():
    if not firebase_admin._apps:
        cred = credentials.Certificate({
            "type": "service_account",
            "project_id": os.getenv("FIREBASE_PROJECT_ID"),
            "private_key": os.getenv("FIREBASE_PRIVATE_KEY").replace("\\n", "\n"),
            "client_email": os.getenv("FIREBASE_CLIENT_EMAIL"),
            "token_uri": "https://oauth2.googleapis.com/token"
        })
        firebase_admin.initialize_app(cred)
    return firestore.client()

def get_dynamo_keys(sport: str, timestamp: str):
    pk = "SENTIMENT#FIFA" if sport == "FIFA_WC_2026" else "SENTIMENT#WT20"
    sk = f"SENTIMENT#{timestamp}"
    return pk, sk

def save_report(report: dict, sport: str = "FIFA_WC_2026"):
    if not report:
        print("⚠️ Failed to save report: Report is empty or None.")
        return None

    # Calculate timestamp in IST (UTC+5:30)
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    timestamp = datetime.now(ist_tz).strftime("%Y-%m-%d_%H-%M-%S")
    now_ms = int(time.time() * 1000)

    doc_data = {
        "timestamp": timestamp,
        "sport": sport,
        "generated_at": now_ms,
        "disclaimer": "Answers are AI-generated. SportsFan360 does not claim accuracy of this content.",
        "report": report
    }

    # 1. Write to DynamoDB (Primary)
    try:
        pk, sk = get_dynamo_keys(sport, timestamp)
        table = get_table("SocialAndContent")
        table.put_item(Item={
            "contentId": pk,
            "sk": sk,
            **doc_data
        })
        print(f"✅ Report saved to DynamoDB: {pk}/{sk}")
    except Exception as e:
        print(f"⚠️ DynamoDB save failed for report: {e}")

    # 2. Dual-write to Firebase (Fallback/Sync)
    try:
        db = init_firebase()
        collection_name = "fifaSentiments" if sport == "FIFA_WC_2026" else "wt20wSentiments"
        fb_data = {**doc_data, "generated_at": firestore.SERVER_TIMESTAMP}
        db.collection(collection_name).document(timestamp).set(fb_data)
        print(f"✅ Report saved to Firebase: {collection_name}/{timestamp}")
    except Exception as e:
        print(f"⚠️ Firebase save fallback failed: {e}")

    return timestamp

def get_latest_report(sport: str = "FIFA_WC_2026"):
    # 1. Try DynamoDB
    try:
        pk = "SENTIMENT#FIFA" if sport == "FIFA_WC_2026" else "SENTIMENT#WT20"
        table = get_table("SocialAndContent")
        res = table.query(
            KeyConditionExpression=Key("contentId").eq(pk),
            ScanIndexForward=False, # Descending order of sort key (timestamps order naturally)
            Limit=1
        )
        if res.get("Items"):
            return res["Items"][0]
    except Exception as e:
        print(f"⚠️ DynamoDB get_latest_report failed: {e}")

    # 2. Fallback to Firebase
    try:
        db = init_firebase()
        collection_name = "fifaSentiments" if sport == "FIFA_WC_2026" else "wt20wSentiments"
        docs = db.collection(collection_name)\
                  .order_by("generated_at", direction=firestore.Query.DESCENDING)\
                  .limit(1)\
                  .stream()
        for doc in docs:
            return doc.to_dict()
    except Exception as e:
        print(f"⚠️ Firebase get_latest_report fallback failed: {e}")

    return None

def list_reports(sport: str = "FIFA_WC_2026", limit: int = 50):
    # 1. Try DynamoDB
    try:
        pk = "SENTIMENT#FIFA" if sport == "FIFA_WC_2026" else "SENTIMENT#WT20"
        table = get_table("SocialAndContent")
        res = table.query(
            KeyConditionExpression=Key("contentId").eq(pk),
            ScanIndexForward=False,
            Limit=limit
        )
        if res.get("Items"):
            return [item["sk"].replace("SENTIMENT#", "") for item in res["Items"]]
    except Exception as e:
        print(f"⚠️ DynamoDB list_reports failed: {e}")

    # 2. Fallback to Firebase
    try:
        db = init_firebase()
        collection_name = "fifaSentiments" if sport == "FIFA_WC_2026" else "wt20wSentiments"
        docs = db.collection(collection_name)\
                 .order_by("generated_at", direction=firestore.Query.DESCENDING)\
                 .limit(limit)\
                 .stream()
        return [doc.id for doc in docs]
    except Exception as e:
        print(f"⚠️ Firebase list_reports fallback failed: {e}")

    return []

def get_report(sport: str = "FIFA_WC_2026", timestamp: str = None):
    if not timestamp:
        return None

    # 1. Try DynamoDB
    try:
        pk, sk = get_dynamo_keys(sport, timestamp)
        table = get_table("SocialAndContent")
        res = table.get_item(Key={"contentId": pk, "sk": sk})
        if res.get("Item"):
            return res["Item"]
    except Exception as e:
        print(f"⚠️ DynamoDB get_report failed: {e}")

    # 2. Fallback to Firebase
    try:
        db = init_firebase()
        collection_name = "fifaSentiments" if sport == "FIFA_WC_2026" else "wt20wSentiments"
        doc_ref = db.collection(collection_name).document(timestamp)
        doc = doc_ref.get()
        if doc.exists:
            return doc.to_dict()
    except Exception as e:
        print(f"⚠️ Firebase get_report fallback failed: {e}")

    return None

if __name__ == "__main__":
    print("Testing connections...")
    try:
        table = get_table("SocialAndContent")
        print("✅ DynamoDB connected successfully!")
    except Exception as e:
        print(f"❌ DynamoDB connection failed: {e}")
    try:
        db = init_firebase()
        print("✅ Firebase connected successfully!")
    except Exception as e:
        print(f"❌ Firebase connection failed: {e}")