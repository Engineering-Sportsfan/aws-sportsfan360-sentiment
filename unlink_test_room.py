from firebase_store import init_firebase

db = init_firebase()

def unlink_room():
    # Find the Spain vs France room in Firestore and clear the matchId link
    rooms = db.collection("roarRooms").stream()
    for room in rooms:
        data = room.to_dict()
        if "Spain vs France" in data.get("name", ""):
            print(f"🔓 Unlinking match from room: {room.id} ('{data.get('name')}')")
            db.collection("roarRooms").document(room.id).update({
                "matchId": None
            })
            print("✅ Success! Room is now unlinked. Dolly will stay silent in this room.")
            return
    print("⚠️ Room not found.")

if __name__ == "__main__":
    unlink_room()
