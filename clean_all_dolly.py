from firebase_store import init_firebase

def delete_all_dolly_posts():
    print("🧹 Wiping all Dolly posts (global and room) to start fresh...")
    db = init_firebase()
    
    # 1. Delete all global posts by Dolly
    global_ref = db.collection("roarPosts").where("authorUid", "==", "dolly-dolphin-bot").stream()
    for doc in global_ref:
        print(f"🗑️ Deleting global post: {doc.id}")
        doc.reference.delete()
        
    # 2. Delete all room messages by Dolly
    room_id = "kw4jNVBIBwNeAdWEvg8v"
    room_ref = db.collection("roarRooms").document(room_id).collection("messages").where("authorUid", "==", "dolly-dolphin-bot").stream()
    for doc in room_ref:
        print(f"🗑️ Deleting room message: {doc.id}")
        doc.reference.delete()
        
    print("✅ All Dolly posts deleted!")

if __name__ == "__main__":
    delete_all_dolly_posts()
