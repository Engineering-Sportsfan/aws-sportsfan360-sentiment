# test_stats_only.py
import asyncio
from world_athletics_client import WorldAthleticsClient

async def main():
    async with WorldAthleticsClient() as wa:
        pbs = await wa.get_personal_bests(15023839)
        print("PERSONAL BESTS:", pbs)
        sb = await wa.get_season_bests(15023839)
        print("SEASON BESTS:", sb)

asyncio.run(main())