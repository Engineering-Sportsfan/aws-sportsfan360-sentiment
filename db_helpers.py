import time
import uuid
from boto3.dynamodb.conditions import Key
from dynamodb_store import get_table
from firebase_store import init_firebase
from firebase_admin import firestore

# ─── Dual-Write Phase Locks ───────────────────────────────────────────────────

def db_check_phase_lock(sport: str, match_id: str, phase: str, room_id: str = None) -> bool:
    room_suffix = f"_{room_id}" if room_id else "_global"
    lock_key = f"dolly_phase_lock_{sport}_{match_id}_{phase}{room_suffix}"
    
    # 1. Try DynamoDB
    try:
        table = get_table("RealTimeChat")
        res = table.get_item(Key={"roomId": "SYSTEM#DOLLY_LOCKS", "sk": f"LOCK#{lock_key}"})
        if res.get("Item"):
            item = res["Item"]
            posted_at = item.get("postedAt", 0)
            post_count = item.get("count", 1)
            if phase == "POST-MATCH":
                return post_count >= 1
            return False
    except Exception as e:
        print(f"⚠️ DynamoDB check phase lock failed: {e}")

    # 2. Fallback to Firebase
    try:
        db = init_firebase()
        doc = db.collection("dollyPhaseLocks").document(lock_key).get()
        if doc.exists:
            data = doc.to_dict()
            post_count = data.get("count", 1)
            if phase == "POST-MATCH":
                return post_count >= 1
    except Exception as e:
        print(f"⚠️ Firebase fallback check phase lock failed: {e}")

    return False

def db_stamp_phase_lock(sport: str, match_id: str, phase: str, room_id: str = None):
    room_suffix = f"_{room_id}" if room_id else "_global"
    lock_key = f"dolly_phase_lock_{sport}_{match_id}_{phase}{room_suffix}"
    now_ms = int(time.time() * 1000)

    # Fetch existing count to increment
    existing_count = 0
    try:
        table = get_table("RealTimeChat")
        res = table.get_item(Key={"roomId": "SYSTEM#DOLLY_LOCKS", "sk": f"LOCK#{lock_key}"})
        if res.get("Item"):
            existing_count = res["Item"].get("count", 0)
    except Exception as e:
        print(f"⚠️ DynamoDB get existing lock count failed: {e}")

    # 1. Put to DynamoDB
    try:
        table = get_table("RealTimeChat")
        table.put_item(Item={
            "roomId": "SYSTEM#DOLLY_LOCKS",
            "sk": f"LOCK#{lock_key}",
            "sport": sport,
            "matchId": match_id,
            "phase": phase,
            "roomIdVal": room_id or "global",
            "postedAt": now_ms,
            "count": existing_count + 1
        })
        print(f"✅ Phase lock stamped in DynamoDB: {lock_key}")
    except Exception as e:
        print(f"⚠️ DynamoDB stamp lock failed: {e}")

    # 2. Put to Firebase fallback
    try:
        db = init_firebase()
        db.collection("dollyPhaseLocks").document(lock_key).set({
            "sport": sport,
            "matchId": match_id,
            "phase": phase,
            "roomId": room_id or "global",
            "postedAt": now_ms,
            "count": existing_count + 1
        })
        print(f"✅ Phase lock stamped in Firebase: {lock_key}")
    except Exception as e:
        print(f"⚠️ Firebase stamp lock failed: {e}")

# ─── Partisan Bot Lock Helpers ────────────────────────────────────────────────

def db_check_partisan_lock(sport: str, match_id: str, room_id: str, bot_uid: str) -> bool:
    room_suffix = f"_{room_id}" if room_id else "_global"
    lock_key = f"partisan_lock_{sport}_{match_id}{room_suffix}_{bot_uid}"
    
    # 1. Try DynamoDB
    try:
        table = get_table("RealTimeChat")
        res = table.get_item(Key={"roomId": "SYSTEM#PARTISAN_LOCKS", "sk": f"LOCK#{lock_key}"})
        if res.get("Item"):
            item = res["Item"]
            posted_at = item.get("postedAt", 0)
            elapsed_minutes = (time.time() * 1000 - posted_at) / (1000 * 60)
            return elapsed_minutes < 10 # 10 minutes cooldown
    except Exception as e:
        print(f"⚠️ DynamoDB check partisan lock failed: {e}")

    # 2. Fallback to Firebase
    try:
        db = init_firebase()
        doc = db.collection("partisanLocks").document(lock_key).get()
        if doc.exists:
            posted_at = doc.to_dict().get("postedAt", 0)
            elapsed_minutes = (time.time() * 1000 - posted_at) / (1000 * 60)
            return elapsed_minutes < 10
    except Exception as e:
        print(f"⚠️ Firebase fallback check partisan lock failed: {e}")

    return False

def db_stamp_partisan_lock(sport: str, match_id: str, room_id: str, bot_uid: str):
    room_suffix = f"_{room_id}" if room_id else "_global"
    lock_key = f"partisan_lock_{sport}_{match_id}{room_suffix}_{bot_uid}"
    now_ms = int(time.time() * 1000)

    # 1. Put to DynamoDB
    try:
        table = get_table("RealTimeChat")
        table.put_item(Item={
            "roomId": "SYSTEM#PARTISAN_LOCKS",
            "sk": f"LOCK#{lock_key}",
            "sport": sport,
            "matchId": match_id,
            "roomIdVal": room_id or "global",
            "botUid": bot_uid,
            "postedAt": now_ms
        })
        print(f"✅ Partisan lock stamped in DynamoDB: {lock_key}")
    except Exception as e:
        print(f"⚠️ DynamoDB stamp partisan lock failed: {e}")

    # 2. Put to Firebase fallback
    try:
        db = init_firebase()
        db.collection("partisanLocks").document(lock_key).set({
            "sport": sport,
            "matchId": match_id,
            "roomId": room_id or "global",
            "botUid": bot_uid,
            "postedAt": now_ms
        })
        print(f"✅ Partisan lock stamped in Firebase: {lock_key}")
    except Exception as e:
        print(f"⚠️ Firebase stamp partisan lock failed: {e}")


# ─── Dual-Write Bot Posts & Messages ──────────────────────────────────────────

def db_save_room_message(room_id: str, text: str, bot_uid: str, bot_username: str, sport: str, type_val: str, polls_data: list = None, extra_payload: dict = None):
    now_ms = int(time.time() * 1000)
    msg_id = f"msg_{now_ms}_{uuid.uuid4().hex[:6]}"

    message_payload = {
        "msgId": msg_id,
        "roomId": room_id,
        "authorUid": bot_uid,
        "authorUsername": bot_username,
        "authorBadge": "Bot",
        "text": text,
        "type": type_val,
        "createdAt": now_ms,
        "sport": sport,
        "fireCount": 0,
        "noChanceCount": 0,
        "agreeCount": 0,
        "disagreeCount": 0,
        "heartCount": 0,
        "replyCount": 0
    }
    if polls_data:
        message_payload["questions"] = polls_data
        # Also hoist sideA/sideB to top-level so frontend can read them directly
        # (frontend reads m.sideA and m.sideB at root, not inside questions array)
        first_poll = polls_data[0] if len(polls_data) > 0 else {}
        if first_poll.get("sideA"):
            message_payload["sideA"] = first_poll["sideA"]
        if first_poll.get("sideB"):
            message_payload["sideB"] = first_poll["sideB"]
    if extra_payload:
        message_payload.update(extra_payload)

    # 1. Write to DynamoDB RealTimeChat
    try:
        table = get_table("RealTimeChat")
        table.put_item(Item={
            "roomId": f"ROOM#{room_id}",
            "sk": f"MSG#{room_id}#{now_ms}#{msg_id}",
            **message_payload
        })
        print(f"✅ Bot message saved to DynamoDB: ROOM#{room_id}")
    except Exception as e:
        print(f"⚠️ DynamoDB save bot message failed: {e}")

    # 2. Write to Firebase roarRooms collection
    try:
        db = init_firebase()
        db.collection("roarRooms").document(room_id).collection("messages").document(msg_id).set({
            **message_payload,
            "createdAt": now_ms
        })
        print(f"✅ Bot message saved to Firebase: roarRooms/{room_id}/messages/{msg_id}")
    except Exception as e:
        print(f"⚠️ Firebase save bot message failed: {e}")

def db_save_bot_post(text: str, bot_uid: str, bot_username: str, sport: str, type_val: str, polls_data: list = None, extra_payload: dict = None):
    now_ms = int(time.time() * 1000)
    post_id = f"post_{now_ms}_{uuid.uuid4().hex[:6]}"

    post_payload = {
        "id": post_id,
        "authorUid": bot_uid,
        "authorUsername": bot_username,
        "text": text,
        "type": type_val,
        "createdAt": now_ms,
        "sport": sport,
        "likes": 0,
        "likedBy": [],
        "repostCount": 0
    }
    if polls_data:
        post_payload["poll"] = polls_data[0] if len(polls_data) > 0 else None
    if extra_payload:
        post_payload.update(extra_payload)

    # 1. Write to DynamoDB SocialAndContent
    try:
        table = get_table("SocialAndContent")
        table.put_item(Item={
            "contentId": f"POST#{post_id}",
            "sk": f"POST#{now_ms}",
            **post_payload
        })
        print(f"✅ Bot post saved to DynamoDB: POST#{post_id}")
    except Exception as e:
        print(f"⚠️ DynamoDB save bot post failed: {e}")

    # 2. Write to Firebase roarPosts
    try:
        db = init_firebase()
        db.collection("roarPosts").document(post_id).set(post_payload)
        print(f"✅ Bot post saved to Firebase: roarPosts/{post_id}")
    except Exception as e:
        print(f"⚠️ Firebase save bot post failed: {e}")

# ─── Cooldown and Question History checks ──────────────────────────────────────

def db_was_recently_posted(room_id: str = None, sport: str = "cricket", cooldown_minutes: int = 15, bot_uid: str = "dolly-dolphin-bot") -> bool:
    cutoff_ms = int((time.time() - cooldown_minutes * 60) * 1000)
    
    # 1. Try DynamoDB
    try:
        if room_id:
            table = get_table("RealTimeChat")
            res = table.query(
                KeyConditionExpression=Key("roomId").eq(f"ROOM#{room_id}"),
                FilterExpression="authorUid = :bot AND sport = :sport",
                ExpressionAttributeValues={":bot": bot_uid, ":sport": sport},
                ScanIndexForward=False,
                Limit=10
            )
            for item in res.get("Items", []):
                if item.get("createdAt", 0) > cutoff_ms:
                    return True
        else:
            table = get_table("SocialAndContent")
            # Scan matches or check via scan
            pass
    except Exception as e:
        print(f"⚠️ DynamoDB cooldown check failed: {e}")

    # 2. Fallback / Main check on Firebase
    try:
        db = init_firebase()
        if room_id:
            msgs = db.collection("roarRooms").document(room_id).collection("messages") \
                .where("authorUid", "==", bot_uid) \
                .where("sport", "==", sport).stream()
            for msg in msgs:
                if msg.to_dict().get("createdAt", 0) > cutoff_ms:
                    return True
        else:
            posts = db.collection("roarPosts") \
                .where("authorUid", "==", bot_uid) \
                .where("sport", "==", sport).stream()
            for post in posts:
                if post.to_dict().get("createdAt", 0) > cutoff_ms:
                    return True
    except Exception as e:
        print(f"⚠️ Firebase cooldown check failed: {e}")

    return False

def db_get_existing_questions(room_id: str = None, sport: str = "cricket", bot_uid: str = "dolly-dolphin-bot") -> list:
    questions = []
    
    # Try DynamoDB
    try:
        if room_id:
            table = get_table("RealTimeChat")
            res = table.query(
                KeyConditionExpression=Key("roomId").eq(f"ROOM#{room_id}"),
                FilterExpression="authorUid = :bot",
                ExpressionAttributeValues={":bot": bot_uid},
                ScanIndexForward=False,
                Limit=30
            )
            for item in res.get("Items", []):
                text = item.get("text")
                if text:
                    questions.append(text)
    except Exception as e:
        print(f"⚠️ DynamoDB get existing questions failed: {e}")

    # Fallback / Merge with Firebase
    try:
        db = init_firebase()
        if room_id:
            room_ref = db.collection("roarRooms").document(room_id).collection("messages") \
                .where("authorUid", "==", bot_uid).stream()
            for doc in sorted(room_ref, key=lambda x: x.to_dict().get("createdAt", 0), reverse=True)[:30]:
                text = doc.to_dict().get("text")
                if text:
                    questions.append(text)
        else:
            global_ref = db.collection("roarPosts").where("authorUid", "==", bot_uid) \
                .where("sport", "==", sport).stream()
            for doc in sorted(global_ref, key=lambda x: x.to_dict().get("createdAt", 0), reverse=True)[:30]:
                text = doc.to_dict().get("text")
                if text:
                    questions.append(text)
    except Exception as e:
        print(f"⚠️ Firebase get existing questions failed: {e}")

    return list(set(questions))

# ─── Match and Room Metadata Queries ──────────────────────────────────────────

def db_get_rooms() -> list:
    # 1. Try DynamoDB scan for META
    try:
        table = get_table("RealTimeChat")
        res = table.scan(
            FilterExpression="begins_with(roomId, :r) AND begins_with(sk, :m)",
            ExpressionAttributeValues={":r": "ROOM#", ":m": "META#"}
        )
        if res.get("Items"):
            return res["Items"]
    except Exception as e:
        print(f"⚠️ DynamoDB rooms scan failed: {e}")

    # 2. Fallback to Firebase
    try:
        db = init_firebase()
        rooms = db.collection("roarRooms").stream()
        return [{**doc.to_dict(), "id": doc.id} for doc in rooms]
    except Exception as e:
        print(f"⚠️ Firebase fallback get rooms failed: {e}")
        return []

def db_get_match(match_id: str) -> dict:
    # 1. Try DynamoDB
    try:
        table = get_table("SportsData")
        res = table.get_item(Key={"entityId": f"MATCH#{match_id}", "sk": "MATCH#META"})
        if res.get("Item"):
            return res["Item"]
    except Exception as e:
        print(f"⚠️ DynamoDB get match failed: {e}")

    # 2. Fallback to Firebase
    try:
        db = init_firebase()
        doc = db.collection("matches").document(match_id).get()
        if doc.exists:
            return doc.to_dict()
    except Exception as e:
        print(f"⚠️ Firebase fallback get match failed: {e}")
    
    return None

def db_update_match_status(match_id: str, status: str):
    now_ms = int(time.time() * 1000)
    
    # 1. Update in DynamoDB
    try:
        table = get_table("SportsData")
        table.update_item(
            Key={"entityId": f"MATCH#{match_id}", "sk": "MATCH#META"},
            UpdateExpression="SET #st = :status, updatedAt = :u",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":status": status, ":u": now_ms}
        )
        print(f"✅ Match {match_id} status updated to {status} in DynamoDB")
    except Exception as e:
        print(f"⚠️ DynamoDB update match status failed: {e}")

    # 2. Update in Firebase
    try:
        db = init_firebase()
        db.collection("matches").document(match_id).update({"status": status, "updated_at": now_ms})
        print(f"✅ Match {match_id} status updated to {status} in Firebase")
    except Exception as e:
        print(f"⚠️ Firebase update match status failed: {e}")

# ─── Pre-Match Grounding Homework (Research) ───────────────────────────────

def db_save_match_research(match_id: str, data: dict):
    now_ms = int(time.time() * 1000)
    table = get_table("SportsData")

    # 1. Save to DynamoDB
    try:
        for r in data.get("rivalries", []):
            rid = f"riv_{uuid.uuid4().hex[:6]}"
            table.put_item(Item={
                "entityId": f"MATCH#{match_id}",
                "sk": f"RIVALRY#{rid}",
                **r,
                "createdAt": now_ms
            })
            
        for s in data.get("stats", []):
            sid = f"stat_{uuid.uuid4().hex[:6]}"
            table.put_item(Item={
                "entityId": f"MATCH#{match_id}",
                "sk": f"STAT#{sid}",
                **s,
                "createdAt": now_ms
            })

        for h in data.get("matchup_history", []):
            hid = f"hist_{uuid.uuid4().hex[:6]}"
            table.put_item(Item={
                "entityId": f"MATCH#{match_id}",
                "sk": f"MATCHUP_HIST#{hid}",
                **h,
                "createdAt": now_ms
            })

        table.put_item(Item={
            "entityId": f"MATCH#{match_id}",
            "sk": "FORM#LATEST",
            **data.get("tournament_form", {}),
            "updatedAt": now_ms
        })
        print(f"✅ Pre-match research saved to DynamoDB for match {match_id}")
    except Exception as e:
        print(f"⚠️ DynamoDB save pre-match research failed: {e}")

    # 2. Save to Firebase fallback
    try:
        db = init_firebase()
        match_doc_ref = db.collection("matches").document(match_id)
        
        for r in data.get("rivalries", []):
            match_doc_ref.collection("rivalries").add({**r, "createdAt": time.time()})
            
        for s in data.get("stats", []):
            match_doc_ref.collection("stats").add({**s, "createdAt": time.time()})
            
        for h in data.get("matchup_history", []):
            match_doc_ref.collection("matchup_history").add({**h, "createdAt": time.time()})
            
        match_doc_ref.collection("tournament_form").document("latest").set({
            **data.get("tournament_form", {}),
            "updatedAt": time.time()
        })
        print(f"✅ Pre-match research saved to Firebase for match {match_id}")
    except Exception as e:
        print(f"⚠️ Firebase save pre-match research failed: {e}")

def db_get_match_research(match_id: str) -> dict:
    rivalries = []
    stats = []
    matchup_history = []
    tournament_form = {}

    # 1. Try DynamoDB
    try:
        table = get_table("SportsData")
        res = table.query(
            KeyConditionExpression=Key("entityId").eq(f"MATCH#{match_id}")
        )
        if res.get("Items"):
            for item in res["Items"]:
                sk = item.get("sk", "")
                if sk.startswith("RIVALRY#"):
                    rivalries.append(item)
                elif sk.startswith("STAT#"):
                    stats.append(item)
                elif sk.startswith("MATCHUP_HIST#"):
                    matchup_history.append(item)
                elif sk == "FORM#LATEST":
                    tournament_form = item
            if rivalries or stats or matchup_history:
                return {
                    "rivalries": rivalries,
                    "stats": stats,
                    "matchup_history": matchup_history,
                    "tournament_form": tournament_form
                }
    except Exception as e:
        print(f"⚠️ DynamoDB get match research failed: {e}")

    # 2. Fallback to Firebase
    try:
        db = init_firebase()
        match_ref = db.collection("matches").document(match_id)
        
        rival_docs = match_ref.collection("rivalries").stream()
        stat_docs = match_ref.collection("stats").stream()
        hist_docs = match_ref.collection("matchup_history").stream()
        form_doc = match_ref.collection("tournament_form").document("latest").get()

        return {
            "rivalries": [d.to_dict() for d in rival_docs],
            "stats": [d.to_dict() for d in stat_docs],
            "matchup_history": [d.to_dict() for d in hist_docs],
            "tournament_form": form_doc.to_dict() if form_doc.exists else {}
        }
    except Exception as e:
        print(f"⚠️ Firebase get match research failed: {e}")

    return {
        "rivalries": [],
        "stats": [],
        "matchup_history": [],
        "tournament_form": {}
    }

def db_get_room(room_id: str) -> dict:
    # 1. Try DynamoDB candidates ROOM#id and raw id
    try:
        table = get_table("RealTimeChat")
        candidates = [f"ROOM#{room_id}", room_id]
        for cand in candidates:
            res = table.get_item(Key={"roomId": cand, "sk": f"META#{room_id}"})
            if res.get("Item"):
                return res["Item"]
    except Exception as e:
        print(f"⚠️ DynamoDB get room failed: {e}")

    # 2. Fallback to Firebase
    try:
        db = init_firebase()
        doc = db.collection("roarRooms").document(room_id).get()
        if doc.exists:
            return doc.to_dict()
    except Exception as e:
        print(f"⚠️ Firebase fallback get room failed: {e}")
    
    return None

def db_get_matches_by_status(sport: str, status: str) -> list:
    # 1. Try DynamoDB
    try:
        table = get_table("SportsData")
        res = table.scan(
            FilterExpression="begins_with(entityId, :m) AND sk = :mt AND sport = :s AND #st = :status",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":m": "MATCH#", ":mt": "MATCH#META", ":s": sport, ":status": status}
        )
        if res.get("Items"):
            results = []
            for item in res["Items"]:
                match_id = item["entityId"].replace("MATCH#", "")
                results.append({**item, "id": match_id})
            return results
    except Exception as e:
        print(f"⚠️ DynamoDB get matches by status failed: {e}")

    # 2. Fallback to Firebase
    try:
        db = init_firebase()
        docs = db.collection("matches")\
                 .where("sport", "==", sport)\
                 .where("status", "==", status)\
                 .stream()
        return [{**doc.to_dict(), "id": doc.id} for doc in docs]
    except Exception as e:
        print(f"⚠️ Firebase get matches by status failed: {e}")
        return []
