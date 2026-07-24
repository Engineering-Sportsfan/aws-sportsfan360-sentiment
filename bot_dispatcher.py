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

def fetch_active_bots(db):
    """Fetches global Kill Switch status for all bots from users collection."""
    bots = {}
    try:
        users_ref = db.collection("users").where("isBot", "==", True).stream()
        for doc in users_ref:
            data = doc.to_dict()
            bots[doc.id] = {
                "name": data.get("username", data.get("name", doc.id)),
                "role": data.get("botRole", "neutral"),
                # Defaults to true unless explicitly disabled in Admin Panel
                "active": data.get("isBotActive", True)
            }
    except Exception as e:
        print(f"⚠️ Error fetching active bots: {e}")
        
    # Seed Dolly if database is completely empty (Backward Compatibility)
    if "dolly-dolphin-bot" not in bots:
        print("🌱 Seeding default Dolly bot profile in active_bots dict.")
        bots["dolly-dolphin-bot"] = {"name": "Dolly", "role": "neutral", "active": True}
        
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
                # Legacy room compatibility: Run Dolly by default
                futures.append(executor.submit(dispatch_bot, "dolly-dolphin-bot", sport, room_id))
                time.sleep(1)
                continue

            # Fetch the match to enforce kickoff time
            match_id = room_data.get("matchId")
            if match_id:
                try:
                    match_doc = db.collection("matches").document(match_id).get()
                    if match_doc.exists:
                        match_data = match_doc.to_dict()
                        kickoff_time = match_data.get("kickoff_time", 0)
                        now_ms = int(time.time() * 1000)
                        if kickoff_time and now_ms < kickoff_time:
                            print(f"⏸️ Match [{match_id}] hasn't kicked off yet (Starts in {(kickoff_time - now_ms)/60000:.1f} mins). Skipping bots for room [{room_id}].")
                            continue
                except Exception as e:
                    print(f"⚠️ Error fetching match data for room {room_id}: {e}")
                    
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
