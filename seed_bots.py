import os
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/Users/prishadureja/Desktop/aws-sportsfan360-sentiment/google_creds.json"
cred = credentials.ApplicationDefault()
firebase_admin.initialize_app(cred, {'projectId': 'fleet-gift-498306-p7'})
db = firestore.client()

bots = [
    {"id": "dolly-dolphin-bot", "username": "Dolly", "botRole": "Neutral Analyst", "isBot": True, "isBotActive": True, "avatarUrl": "/images/Dolly 4.png"},
    {"id": "krishna-india-bot", "username": "Krishna", "botRole": "Partisan India Fan", "isBot": True, "isBotActive": True, "avatarUrl": "/images/krishna.png"},
    {"id": "radha-england-bot", "username": "Radha", "botRole": "Partisan England Fan", "isBot": True, "isBotActive": True, "avatarUrl": "/images/radha.png"}
]

for bot in bots:
    bot_id = bot.pop("id")
    db.collection("users").document(bot_id).set(bot, merge=True)
    print(f"✅ Injected bot: {bot_id}")

print("Database seeded perfectly.")
