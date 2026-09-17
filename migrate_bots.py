from firebase_store import init_firebase

def migrate():
    db = init_firebase()
    print("🚀 Starting Bot UID database migration...")

    # 1. Migrate Users collection documents
    mappings = {
        "krishna-bot": "krishna-india-bot",
        "radha-bot": "radha-england-bot"
    }

    for old_id, new_id in mappings.items():
        old_ref = db.collection("users").document(old_id)
        old_doc = old_ref.get()
        if old_doc.exists:
            data = old_doc.to_dict()
            # Update UID field inside the data
            data["uid"] = new_id
            
            # Create new doc
            new_ref = db.collection("users").document(new_id)
            new_ref.set(data)
            print(f"✅ Copied {old_id} data to {new_id}")
            
            # Delete old doc
            old_ref.delete()
            print(f"🗑️ Deleted old document {old_id}")
        else:
            print(f"⚠️ Document {old_id} not found in users collection (might be already migrated).")

    # 2. Migrate botConfig inside active roarRooms
    rooms = db.collection("roarRooms").where("isActive", "==", True).stream()
    for room in rooms:
        room_data = room.to_dict()
        bot_config = room_data.get("botConfig")
        if bot_config:
            updated = False
            new_config = {}
            for bot_id, config in bot_config.items():
                if bot_id in mappings:
                    new_id = mappings[bot_id]
                    new_config[new_id] = config
                    updated = True
                    print(f"🔄 Room [{room.id}]: Mapping {bot_id} -> {new_id}")
                else:
                    new_config[bot_id] = config
            
            if updated:
                db.collection("roarRooms").document(room.id).update({
                    "botConfig": new_config
                })
                print(f"✅ Updated botConfig in room {room.id}")

    print("🎉 Database migration completed successfully!")

if __name__ == "__main__":
    migrate()
