import os
from datetime import datetime, timezone, timedelta
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

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

def save_report(report: dict, sport: str = "FIFA_WC_2026"):
    if not report:
        print("⚠️ Failed to save report to Firebase: Report is empty or None.")
        return None
    db = init_firebase()
    
    # Calculate timestamp in IST (UTC+5:30)
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    timestamp = datetime.now(ist_tz).strftime("%Y-%m-%d_%H-%M-%S")
    
    doc_data = {
        "timestamp": timestamp,
        "sport": sport,
        "generated_at": firestore.SERVER_TIMESTAMP,
        "disclaimer": "Answers are AI-generated. SportsFan360 does not claim accuracy of this content.",
        "report": report
    }
    
    collection_name = "fifaSentiments" if sport == "FIFA_WC_2026" else "wt20wSentiments"
    db.collection(collection_name).document(timestamp).set(doc_data)
    print(f"✅ Report saved to Firebase: {collection_name}/{timestamp}")
    
    return timestamp

def get_latest_report(sport: str = "FIFA_WC_2026"):
    db = init_firebase()
    
    # Bug 2 fixed: use correct collection based on sport
    collection_name = "fifaSentiments" if sport == "FIFA_WC_2026" else "wt20wSentiments"
    docs = db.collection(collection_name)\
              .order_by("generated_at", direction=firestore.Query.DESCENDING)\
              .limit(1)\
              .stream()
    
    for doc in docs:
        return doc.to_dict()
    return None

def list_reports(sport: str = "FIFA_WC_2026", limit: int = 50):
    db = init_firebase()
    collection_name = "fifaSentiments" if sport == "FIFA_WC_2026" else "wt20wSentiments"
    docs = db.collection(collection_name)\
             .order_by("generated_at", direction=firestore.Query.DESCENDING)\
             .limit(limit)\
             .stream()
    return [doc.id for doc in docs]

def get_report(sport: str = "FIFA_WC_2026", timestamp: str = None):
    if not timestamp:
        return None
    db = init_firebase()
    collection_name = "fifaSentiments" if sport == "FIFA_WC_2026" else "wt20wSentiments"
    doc_ref = db.collection(collection_name).document(timestamp)
    doc = doc_ref.get()
    if doc.exists:
        return doc.to_dict()
    return None

if __name__ == "__main__":
    print("Testing Firebase connection...")
    db = init_firebase()
    print("✅ Firebase connected successfully!")