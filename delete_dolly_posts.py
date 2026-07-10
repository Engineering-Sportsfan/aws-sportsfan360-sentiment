from firebase_store import init_firebase

def delete_old_dolly_posts():
    print("🧹 Cleaning up old Dolly posts from Firestore...")
    db = init_firebase()
    
    # 1. Clean up global roarPosts (keep only the 3 most recent)
    global_ref = db.collection("roarPosts").where("authorUid", "==", "dolly-dolphin-bot").stream()
    global_posts = [d for d in global_ref]
    # Sort in memory by createdAt descending
    global_posts.sort(key=lambda x: x.to_dict().get("createdAt", 0), reverse=True)
    
    if len(global_posts) > 3:
        for doc in global_posts[3:]:
            print(f"🗑️ Deleting old global post: {doc.id}")
            doc.reference.delete()
            
    # 2. Clean up room messages in Infinity Room (keep only the 3 most recent)
    room_id = "kw4jNVBIBwNeAdWEvg8v"
    room_ref = db.collection("roarRooms").document(room_id).collection("messages").where("authorUid", "==", "dolly-dolphin-bot").stream()
    room_posts = [d for d in room_ref]
    # Sort in memory by createdAt descending
    room_posts.sort(key=lambda x: x.to_dict().get("createdAt", 0), reverse=True)
    
    if len(room_posts) > 3:
        for doc in room_posts[3:]:
            print(f"🗑️ Deleting old room message: {doc.id}")
            doc.reference.delete()
            
    print("✅ Finished cleaning up old Dolly posts!")

if __name__ == "__main__":
    delete_old_dolly_posts()
