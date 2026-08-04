import asyncio
from world_athletics_client import WorldAthleticsClient

async def main():
    async with WorldAthleticsClient() as wa:
        profile = await wa.get_cis_single_competitor("india/neeraj-chopra-14549089")
        print(profile)

asyncio.run(main())