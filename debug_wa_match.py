import asyncio
from world_athletics_client import WorldAthleticsClient


async def main():
    athlete_id = 14549089  # Neeraj Chopra

    async with WorldAthleticsClient() as wa:
        basic = await wa.get_competitor_basic_info(id=athlete_id)
        basic_data = basic.get("basicData") or {}
        country_code = basic_data.get("countryCode")
        iaaf_id = basic_data.get("iaafId")

        print("country_code from basic info:", repr(country_code))
        print("iaafId from basic info:", repr(iaaf_id))

        if country_code:
            candidates = await wa.search_competitors(country_code=country_code)
            print(f"search_competitors(country_code={country_code!r}) returned {len(candidates)} candidates")
            for c in candidates:
                print("  ", {
                    "aaAthleteId": c.get("aaAthleteId"),
                    "iaafId": c.get("iaafId"),
                    "familyName": c.get("familyName"),
                    "givenName": c.get("givenName"),
                })
        else:
            print("country_code was falsy -- search_competitors was never called")


asyncio.run(main())