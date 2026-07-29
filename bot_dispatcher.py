import time
import concurrent.futures
from firebase_store import init_firebase
from dolly_bot import run_dolly_for_sport

# Phase 6 Dependency Protection (Try/Except)
try:
    from partisan_bot import run_partisan_bot
except ImportError:
    run_partisan_bot = None

def acquire_dispatcher_lock(db):
    """Prevents AWS EventBridge overlaps from spawning duplicate bots."""
    lock_ref = db.collection("system").document("dispatcherLock")
    now_ms = int(time.time() * 1000)
    
    try:
        doc = lock_ref.get()
        if doc.exists:
            last_run = doc.to_dict().get("lockedAt", 0)
            # 4 minute threshold (240000 ms) to prevent overlap of 5-min cron
            if now_ms - last_run < 240000:
                print(f"🔒 Dispatcher overlap detected! Last run was {(now_ms - last_run)/1000}s ago. Exiting.")
                return False
                
        lock_ref.set({"lockedAt": now_ms})
        return True
    except Exception as e:
        print(f"⚠️ Error acquiring lock: {e}. Falling back to run anyway.")
        return True


# ── Default Bot Profiles (Single Source of Truth) ─────────────────────────────
# These are the 3 permanent bots. If their Firestore user documents are missing,
# they are auto-created so the Admin Panel Kill Switch works AND bots never go silent.
DEFAULT_BOTS = {
    "dolly-dolphin-bot": {
        "username": "Dolly",
        "botRole": "neutral",
        "isBot": True,
        "isBotActive": True,
        "displayPicture": "",
        "createdAt": 0,
    },
    "krishna-india-bot": {
        "username": "Krishna",
        "botRole": "partisan",
        "isBot": True,
        "isBotActive": True,
        "displayPicture": "",
        "createdAt": 0,
    },
    "radha-england-bot": {
        "username": "Radha",
        "botRole": "partisan",
        "isBot": True,
        "isBotActive": True,
        "displayPicture": "",
        "createdAt": 0,
    },
}

def fetch_active_bots(db):
    """
    Fetches global Kill Switch status for all bots from the users collection.
    PERMANENT FIX: If any of the 3 core bot documents are missing from Firestore,
    they are auto-created so the Admin Panel can control them AND so bots never
    go silently dark due to a missing document.
    """
    bots = {}
    try:
        users_ref = db.collection("users").where("isBot", "==", True).stream()
        for doc in users_ref:
            data = doc.to_dict()
            bots[doc.id] = {
                "name": data.get("username", data.get("name", doc.id)),
                "role": data.get("botRole", "neutral"),
                "active": data.get("isBotActive", True)
            }
    except Exception as e:
        print(f"⚠️ Error fetching active bots: {e}")

    # ── Self-Healing: Auto-create missing bot documents in Firestore ──────────
    for bot_uid, profile in DEFAULT_BOTS.items():
        if bot_uid not in bots:
            print(f"🌱 Bot [{bot_uid}] missing from Firestore. Auto-creating user document...")
            try:
                db.collection("users").document(bot_uid).set(profile, merge=True)
            except Exception as e:
                print(f"⚠️ Could not auto-create bot [{bot_uid}] in Firestore: {e}")
            # Always seed in memory even if the DB write fails
            bots[bot_uid] = {
                "name": profile["username"],
                "role": profile["botRole"],
                "active": profile["isBotActive"],
            }
        
    return bots


def run_bot_dispatcher():
    print("🚀 Central Bot Dispatcher started.")
    db = init_firebase()
    
    if not acquire_dispatcher_lock(db):
        return
        
    active_bots = fetch_active_bots(db)
    
    futures = []
    # Throttled execution to prevent Gemini 429 Rate Limit Error
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=3)
    
    def dispatch_bot(bot_uid, sport, room_id, team=None):
        bot_info = active_bots.get(bot_uid)
        if not bot_info or not bot_info["active"]:
            print(f"⏸️ Bot [{bot_uid}] is disabled by Global Kill Switch. Skipping.")
            return
            
        role = bot_info["role"]
        username = bot_info["name"]
        
        try:
            if role == "neutral" or bot_uid == "dolly-dolphin-bot":
                print(f"🤖 Dispatching {username} (Neutral) to sport={sport}, room={room_id}")
                run_dolly_for_sport(sport, room_id=room_id, bot_uid=bot_uid, bot_username=username)
            elif role == "partisan" and team:
                if run_partisan_bot:
                    print(f"🔥 Dispatching {username} (Partisan: {team}) to sport={sport}, room={room_id}")
                    run_partisan_bot(bot_uid, team, sport, room_id)
                else:
                    print(f"⚠️ Phase 6 partisan_bot.py not found. Skipping Partisan Bot [{bot_uid}].")
        except Exception as e:
            print(f"❌ Error executing bot [{bot_uid}] in room [{room_id}]: {e}")
                
    # ── 1. GLOBAL FEED (Headless runs) ──
    # Ensure main app feed receives AI updates
    print("🌐 Dispatching Global Feeds...")
    futures.append(executor.submit(dispatch_bot, "dolly-dolphin-bot", "cricket", None))
    time.sleep(1) # Stagger to protect rate limits
    futures.append(executor.submit(dispatch_bot, "dolly-dolphin-bot", "football", None))
    time.sleep(1)
    
    # ── 2. ROAR ROOMS ──
    print("🏟️ Scanning Active RoAR Rooms...")
    try:
        roar_rooms = db.collection("roarRooms").where("isActive", "==", True).stream()
        
        for room in roar_rooms:
            room_id = room.id
            room_data = room.to_dict()
            sport = room_data.get("sport", "cricket")
            
            # The "Infinity Room" Pause (Backward Compatibility Rule)
            if "infinity" in room_id.lower() or "infinity" in room_data.get("name", "").lower():
                print(f"⏸️ Skipping Infinity Room [{room_id}] per Phase 5 instruction.")
                continue
                
            bot_config = room_data.get("botConfig")
            
            if not bot_config:
                print(f"⚠️ Room [{room_id}] has no botConfig. Defaulting to Dolly, Krishna, and Radha dynamically.")
                match_id = room_data.get("matchId")
                if match_id:
                    try:
                        match_doc = db.collection("matches").document(match_id).get()
                        if match_doc.exists:
                            match_data_tmp = match_doc.to_dict()
                            team_a = match_data_tmp.get("team_a", "India")
                            team_b = match_data_tmp.get("team_b", "England")
                        else:
                            team_a, team_b = ("Real Madrid", "Barcelona") if sport == "football" else ("India", "England")
                    except Exception as e:
                        print(f"⚠️ Error fetching match for default bots: {e}")
                        team_a, team_b = ("Real Madrid", "Barcelona") if sport == "football" else ("India", "England")
                else:
                    # No match linked — use sport-appropriate default teams
                    team_a, team_b = ("Real Madrid", "Barcelona") if sport == "football" else ("India", "England")
                
                bot_config = {
                    "dolly-dolphin-bot": {"role": "neutral"},
                    "krishna-india-bot": {"role": "partisan", "team": team_a},
                    "radha-england-bot": {"role": "partisan", "team": team_b}
                }

            # Fetch the match to enforce kickoff time & completed status
            match_id = room_data.get("matchId")
            now_ms = int(time.time() * 1000)
            
            if match_id:
                try:
                    match_doc = db.collection("matches").document(match_id).get()
                    if match_doc.exists:
                        match_data = match_doc.to_dict()
                        kickoff_time = match_data.get("kickoff_time", 0)
                        status = match_data.get("status")
                        
                        # 1. Kickoff gating
                        if status == "upcoming" and kickoff_time and now_ms < kickoff_time:
                            print(f"⏸️ Match [{match_id}] hasn't kicked off yet. Skipping bots for room [{room_id}].")
                            continue
                            
                        # 2. Concluded / Completed gating (45 minutes post-match cutoff)
                        if status == "completed":
                            updated_at_val = match_data.get("updated_at")
                            if hasattr(updated_at_val, "timestamp"):
                                updated_at_ms = int(updated_at_val.timestamp() * 1000)
                            else:
                                updated_at_ms = int(updated_at_val or 0)
                                
                            if now_ms - updated_at_ms > 2700000:
                                print(f"⏸️ Match [{match_id}] concluded > 45 mins ago. Stopping bots for room [{room_id}].")
                                continue
                except Exception as e:
                    print(f"⚠️ Error fetching match data for room {room_id}: {e}")
            else:
                # No match linked — check if it is a designated testing room
                is_testing = room_data.get("isTestingRoom", False)
                if not is_testing:
                    print(f"🔒 Room [{room_id}] has no match and is not marked for testing. Blocking bots.")
                    continue
                
                # Testing room cutoff (1 hour limit)
                created_at = room_data.get("createdAt", 0)
                if now_ms - created_at > 3600000:
                    print(f"⏸️ Testing Room [{room_id}] exceeded 1 hour limit. Stopping bots.")
                    continue
                    
            # Process dynamically assigned bots
            for bot_uid, config in bot_config.items():
                if not config: # If config is False (unchecked in UI)
                    continue
                    
                team = config.get("team") if isinstance(config, dict) else None
                role = config.get("role") if isinstance(config, dict) else "neutral"
                
                # Catch Stateless Partisan Crash
                if role == "partisan" and not team:
                    print(f"⚠️ Partisan Bot [{bot_uid}] in room [{room_id}] has no team selected! Skipping.")
                    continue
                    
                futures.append(executor.submit(dispatch_bot, bot_uid, sport, room_id, team))
                time.sleep(1)
    except Exception as e:
        print(f"⚠️ Error scanning RoAR Rooms: {e}")
            
    # ── 3. WATCHALONG ROOMS (Integrated Linked Rooms) ──
    print("📺 Scanning Active Watchalong Rooms...")
    try:
        watch_rooms = db.collection("watchAlongRooms").where("isLive", "==", True).stream()
        
        for room in watch_rooms:
            room_id = room.id
            room_data = room.to_dict()
            sport = room_data.get("sport", "cricket")
            
            bot_config = room_data.get("botConfig")
            if not bot_config:
                # Pure standalone watchalongs have no bots (verified)
                continue 
                
            for bot_uid, config in bot_config.items():
                if not config: 
                    continue
                team = config.get("team") if isinstance(config, dict) else None
                role = config.get("role") if isinstance(config, dict) else "neutral"
                
                if role == "partisan" and not team:
                    print(f"⚠️ Partisan Bot [{bot_uid}] in watchalong [{room_id}] has no team selected! Skipping.")
                    continue
                    
                futures.append(executor.submit(dispatch_bot, bot_uid, sport, room_id, team))
                time.sleep(1)
    except Exception as e:
        print(f"⚠️ Error scanning Watchalong Rooms: {e}")

    print(f"⏳ Waiting for {len(futures)} AWS threads to complete...")
    # CRITICAL: Forces AWS Lambda to stay awake until all Firebase writes succeed
    concurrent.futures.wait(futures)
    executor.shutdown()
    
    print("✅ Central Bot Dispatcher finished successfully.")

if __name__ == "__main__":
    run_bot_dispatcher()
