import time
import concurrent.futures
from dynamodb_store import get_table
from firebase_store import init_firebase
from dolly_bot import run_dolly_for_sport
from db_helpers import db_get_room

try:
    from partisan_bot import run_partisan_bot
except Exception as e:
    print(f"⚠️ Error importing partisan_bot: {e}")
    run_partisan_bot = None

def acquire_dispatcher_lock():
    """Prevents AWS EventBridge overlaps from spawning duplicate bots."""
    now_ms = int(time.time() * 1000)
    
    try:
        table = get_table("RealTimeChat")
        res = table.get_item(Key={"roomId": "SYSTEM#DISPATCHER_LOCK", "sk": "META"})
        if res.get("Item"):
            last_run = res["Item"].get("lockedAt", 0)
            # 4 minute threshold (240000 ms) to prevent overlap of 5-min cron
            if now_ms - last_run < 240000:
                print(f"🔒 Dispatcher overlap detected! Last run was {(now_ms - last_run)/1000}s ago. Exiting.")
                return False
                
        table.put_item(Item={"roomId": "SYSTEM#DISPATCHER_LOCK", "sk": "META", "lockedAt": now_ms})
        return True
    except Exception as e:
        print(f"⚠️ Error acquiring lock in DynamoDB: {e}. Falling back to run anyway.")
        return True
    
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

def fetch_active_bots():
    """
    Fetches global Kill Switch status for all bots from DynamoDB users collection.
    """
    bots = {}
    try:
        table = get_table("SportsData")
        for bot_uid, profile in DEFAULT_BOTS.items():
            res = table.get_item(Key={"entityId": f"USER#{bot_uid}", "sk": "USER#META"})
            if res.get("Item"):
                data = res["Item"]
                bots[bot_uid] = {
                    "name": data.get("username", data.get("name", bot_uid)),
                    "role": data.get("botRole", "neutral"),
                    "active": data.get("isBotActive", True)
                }
            else:
                print(f"🌱 Bot [{bot_uid}] missing from DynamoDB. Auto-creating user document...")
                try:
                    table.put_item(Item={
                        "entityId": f"USER#{bot_uid}",
                        "sk": "USER#META",
                        **profile
                    })
                except Exception as e:
                    print(f"⚠️ Could not auto-create bot [{bot_uid}] in DynamoDB: {e}")
                bots[bot_uid] = {
                    "name": profile["username"],
                    "role": profile["botRole"],
                    "active": profile["isBotActive"],
                }
    except Exception as e:
        print(f"⚠️ Error fetching active bots from DynamoDB: {e}")
        # Fallback to defaults
        for bot_uid, profile in DEFAULT_BOTS.items():
            if bot_uid not in bots:
                bots[bot_uid] = {"name": profile["username"], "role": profile["botRole"], "active": profile["isBotActive"]}
        
    return bots


def run_bot_dispatcher(specific_room_id: str = None):
    print("🚀 Central Bot Dispatcher started.")
    
    if not specific_room_id and not acquire_dispatcher_lock():
        return
        
    active_bots = fetch_active_bots()
    
    futures = []
    # Throttled execution to prevent Gemini 429 Rate Limit Error
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)
    
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

    # ── 0. DIRECT TARGET ROOM (If specified via CLI) ──
    if specific_room_id:
        raw_id = specific_room_id.replace("ROOM#", "").strip()
        print(f"🎯 Direct Target Room specified: [{raw_id}]")
        room_data = db_get_room(raw_id) or {}
        sport = room_data.get("sport", "cricket")
        bot_cfg = room_data.get("botConfig") or {}
        
        team_a = "India"
        team_b = "West Indies"
        
        if isinstance(bot_cfg.get("krishna-india-bot"), dict) and bot_cfg["krishna-india-bot"].get("team"):
            team_a = bot_cfg["krishna-india-bot"]["team"]
        if isinstance(bot_cfg.get("radha-england-bot"), dict) and bot_cfg["radha-england-bot"].get("team"):
            team_b = bot_cfg["radha-england-bot"]["team"]
            
        match_id = room_data.get("matchId")
        if match_id:
            try:
                sports_table = get_table("SportsData")
                res_match = sports_table.get_item(Key={"entityId": f"MATCH#{match_id}", "sk": "MATCH#META"})
                if res_match.get("Item"):
                    team_a = res_match["Item"].get("team_a", team_a)
                    team_b = res_match["Item"].get("team_b", team_b)
            except Exception as e:
                pass
                
        futures.append(executor.submit(dispatch_bot, "dolly-dolphin-bot", sport, raw_id))
        time.sleep(1)
        futures.append(executor.submit(dispatch_bot, "krishna-india-bot", sport, raw_id, team_a))
        time.sleep(1)
        futures.append(executor.submit(dispatch_bot, "radha-england-bot", sport, raw_id, team_b))
        time.sleep(1)
        
        print(f"⏳ Waiting for {len(futures)} AWS threads to complete...")
        concurrent.futures.wait(futures)
        executor.shutdown()
        print(f"✅ Dispatched all bots directly into room [{raw_id}]!")
        return
                
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
        table = get_table("RealTimeChat")
        # Scan for active rooms in DynamoDB
        items = []
        scan_kwargs = {}
        while True:
            res = table.scan(**scan_kwargs)
            items.extend(res.get("Items", []))
            if "LastEvaluatedKey" not in res:
                break
            scan_kwargs["ExclusiveStartKey"] = res["LastEvaluatedKey"]

        # Filter for room metadata documents (exclude SYSTEM locks and message rows)
        roar_rooms = []
        for item in items:
            rid = str(item.get("roomId", ""))
            sk = str(item.get("sk", ""))
            if rid.startswith("SYSTEM#") or sk.startswith("MSG#") or sk.startswith("LOCK#"):
                continue
            if not (sk.startswith("META") or "name" in item or "sport" in item or "botConfig" in item):
                continue
            is_act = item.get("isActive") is True or str(item.get("isActive")).lower() == "true" or str(item.get("status", "")).upper() == "ACTIVE"
            if is_act:
                roar_rooms.append(item)
        
        for room_data in roar_rooms:
            # Extract actual room ID from DynamoDB key
            raw_room_id = room_data.get("roomId", "")
            room_id = raw_room_id.replace("ROOM#", "") if raw_room_id.startswith("ROOM#") else raw_room_id

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
                        sports_table = get_table("SportsData")
                        res_match = sports_table.get_item(Key={"entityId": f"MATCH#{match_id}", "sk": "MATCH#META"})
                        if res_match.get("Item"):
                            match_data_tmp = res_match["Item"]
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
            is_testing = room_data.get("isTestingRoom", False)
            now_ms = int(time.time() * 1000)
            
            if match_id:
                try:
                    sports_table = get_table("SportsData")
                    res_match = sports_table.get_item(Key={"entityId": f"MATCH#{match_id}", "sk": "MATCH#META"})
                    if res_match.get("Item"):
                        match_data = res_match["Item"]
                        kickoff_time = match_data.get("kickoff_time", 0)
                        status = match_data.get("status")
                        
                        # Testing Rooms bypass match gating restrictions
                        if not is_testing:
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
                if not is_testing:
                    print(f"🔒 Room [{room_id}] has no match and is not marked for testing. Blocking bots.")
                    continue
                
            # Global Testing room cutoff (1 hour limit)
            if is_testing:
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
        db = init_firebase()
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

def run_continuous_session(room_id: str = None, interval_minutes: int = 10, total_duration_minutes: int = 90):
    """
    Runs automated bot batches every X minutes for the specified duration (e.g. 1 - 1.5 hours).
    Each round dispatches:
      - 1 Krishna post (Partisan)
      - 1 Radha post (Partisan)
      - 2 Dolly posts (Neutral debate & prediction)
    """
    start_time = time.time()
    total_seconds = total_duration_minutes * 60
    round_num = 1
    
    target_desc = f"Room [{room_id}]" if room_id else "All Active Rooms"
    print(f"\n🔄 Starting Automated Bot Session for {target_desc}")
    print(f"⏱️ Interval: Every {interval_minutes} mins | Total Duration: {total_duration_minutes} mins (~{total_duration_minutes/60:.1f} hrs)")
    print(f"🤖 Batch per cycle: 1 Krishna + 1 Radha + 2 Dolly posts\n")
    
    while time.time() - start_time < total_seconds:
        elapsed_mins = int((time.time() - start_time) / 60)
        remaining_mins = max(0, total_duration_minutes - elapsed_mins)
        print(f"\n{'='*60}")
        print(f"🚀 Round #{round_num} | Elapsed: {elapsed_mins}m / {total_duration_minutes}m | Remaining: {remaining_mins}m")
        print(f"{'='*60}")
        
        # Clear locks between rounds if any
        try:
            table = get_table("RealTimeChat")
            table.delete_item(Key={"roomId": "SYSTEM#DISPATCHER_LOCK", "sk": "META"})
        except Exception:
            pass
            
        run_bot_dispatcher(specific_room_id=room_id)
        
        round_num += 1
        
        if time.time() - start_time + (interval_minutes * 60) >= total_seconds:
            break
            
        sleep_sec = interval_minutes * 60
        print(f"\n⏳ Sleeping for {interval_minutes} minutes until next round... (Press Ctrl+C to stop)")
        time.sleep(sleep_sec)
        
    print(f"\n🏁 Automated session complete! Completed {round_num - 1} rounds over {total_duration_minutes} minutes.")

if __name__ == "__main__":
    import sys
    import argparse
    
    parser = argparse.ArgumentParser(description="RoAR Central Bot Dispatcher & Session Runner")
    parser.add_argument("room", nargs="?", default=None, help="Target Room ID (optional)")
    parser.add_argument("--loop", action="store_true", help="Run in continuous loop mode")
    parser.add_argument("--interval", type=int, default=10, help="Interval in minutes between bot batches (default: 10)")
    parser.add_argument("--duration", type=int, default=90, help="Total session duration in minutes (default: 90 = 1.5 hrs)")
    
    args, unknown = parser.parse_known_args()
    
    # Handle room passed via unknown args or positional
    target_room = args.room
    if not target_room and unknown:
        for u in unknown:
            if not u.startswith("-"):
                target_room = u
                break
                
    if args.loop:
        run_continuous_session(
            room_id=target_room,
            interval_minutes=args.interval,
            total_duration_minutes=args.duration
        )
    else:
        run_bot_dispatcher(specific_room_id=target_room)
