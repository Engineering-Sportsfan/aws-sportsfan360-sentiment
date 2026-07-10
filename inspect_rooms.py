from firebase_store import init_firebase

def inspect_roar_rooms():
    db = init_firebase()
    
    print("--- GLOBAL POSTS IN roarPosts ---")
    posts = db.collection("roarPosts").order_by("createdAt", direction="DESCENDING").limit(5).stream()
    for p in posts:
        data = p.to_dict()
        print(f"ID: {p.id} | Author: {data.get('authorUsername')} ({data.get('authorUid')}) | Text: {data.get('text')}")
        
    print("\n--- MESSAGES IN TEAM HUDDLE (nnklznEBbh1Ki9XqlTID) ---")
    msgs = db.collection("roarRooms").document("nnklznEBbh1Ki9XqlTID").collection("messages").order_by("createdAt", direction="DESCENDING").limit(5).stream()
    for m in msgs:
        data = m.to_dict()
        print(f"ID: {m.id} | Author: {data.get('authorUsername')} ({data.get('authorUid')}) | Text: {data.get('text')}")

if __name__ == "__main__":
    inspect_roar_rooms()
