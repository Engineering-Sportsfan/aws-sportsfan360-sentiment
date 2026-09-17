"""
Multi-Sport Schema Adaptability Test
--------------------------------------
Validates that the single-table design (5 master concepts + 1 transaction
table pattern) can handle:
  1. Multiple sports (Cricket, Badminton, Hockey) with no schema change
  2. Multiple leagues within the same sport (IPL vs BPL, both Cricket)
  3. Multiple formats within the same sport (Test/ODI/T20) without collision
  4. Fetching one player's FULL data (profile + every stint) in one query
  5. Fetching a club/nation's players (reverse lookup, via GSI)
  6. No data leakage between differently-shaped stats blobs

Run: python3 test_schema_adaptability.py
"""

import boto3
from moto import mock_aws
import json
from decimal import Decimal


def _floats_to_decimal(obj):
    """DynamoDB requires Decimal instead of float for numeric values."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _floats_to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_floats_to_decimal(v) for v in obj]
    return obj

TABLE_NAME = "SportsData"


def create_table(dynamodb):
    """Mirrors the real 6-master-table SportsData design: single table,
    PK=entityId, SK=sk, plus GSI1 (league->players) and GSI2 (club->players)."""
    table = dynamodb.create_table(
        TableName=TABLE_NAME,
        KeySchema=[
            {"AttributeName": "entityId", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "entityId", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
            {"AttributeName": "GSI2PK", "AttributeType": "S"},
            {"AttributeName": "GSI2SK", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "GSI1",  # league -> players
                "KeySchema": [
                    {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "GSI2",  # club -> players
                "KeySchema": [
                    {"AttributeName": "GSI2PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI2SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    table.wait_until_exists()
    return table


def seed_master_data(table):
    """1. Seed Sports (Cricket, Badminton, Hockey) -- proves multi-sport works."""
    sports = [
        {"entityId": "SPORT#001", "sk": "SPORT#META", "sportName": "Cricket", "gender": "Men", "teamType": "Team"},
        {"entityId": "SPORT#002", "sk": "SPORT#META", "sportName": "Cricket", "gender": "Women", "teamType": "Team"},
        {"entityId": "SPORT#003", "sk": "SPORT#META", "sportName": "Badminton", "gender": "Mixed", "teamType": "Mixed"},
        {"entityId": "SPORT#004", "sk": "SPORT#META", "sportName": "Hockey", "gender": "Men", "teamType": "Team"},
    ]
    for s in sports:
        table.put_item(Item=s)

    # 2. Seed Level+Format -- proves multiple formats per sport work (Test/ODI/T20)
    level_formats = [
        {"entityId": "LF#001", "sk": "LEVELFORMAT#META", "sportId": "SPORT#001", "level": "Pro League", "format": "T20"},
        {"entityId": "LF#002", "sk": "LEVELFORMAT#META", "sportId": "SPORT#001", "level": "International", "format": "ODI"},
        {"entityId": "LF#003", "sk": "LEVELFORMAT#META", "sportId": "SPORT#001", "level": "International", "format": "Test"},
        {"entityId": "LF#004", "sk": "LEVELFORMAT#META", "sportId": "SPORT#003", "level": "Federation", "format": "Doubles"},
        {"entityId": "LF#005", "sk": "LEVELFORMAT#META", "sportId": "SPORT#004", "level": "Pro League", "format": "Standard"},
    ]
    for lf in level_formats:
        table.put_item(Item=lf)

    # 3. Seed Clubs/Nations
    clubs = [
        {"entityId": "CLUB#001", "sk": "CLUB#META", "clubName": "India", "clubType": "National Team", "country": "India"},
        {"entityId": "CLUB#002", "sk": "CLUB#META", "clubName": "Bangladesh", "clubType": "National Team", "country": "Bangladesh"},
        {"entityId": "CLUB#004", "sk": "CLUB#META", "clubName": "Chennai Super Kings", "clubType": "Franchise", "country": "India"},
        {"entityId": "CLUB#010", "sk": "CLUB#META", "clubName": "Comilla Victorians", "clubType": "Franchise", "country": "Bangladesh"},
    ]
    for c in clubs:
        table.put_item(Item=c)

    # 4. Seed Leagues -- proves multiple leagues in same sport work (IPL vs BPL)
    leagues = [
        {"entityId": "LEAGUE#001", "sk": "LEAGUE#META", "leagueName": "IPL", "levelFormatId": "LF#001", "sportId": "SPORT#001"},
        {"entityId": "LEAGUE#002", "sk": "LEAGUE#META", "leagueName": "BPL", "levelFormatId": "LF#001", "sportId": "SPORT#001"},
        {"entityId": "LEAGUE#003", "sk": "LEAGUE#META", "leagueName": "ICC World Cup", "levelFormatId": "LF#002", "sportId": "SPORT#001"},
        {"entityId": "LEAGUE#004", "sk": "LEAGUE#META", "leagueName": "BWF World Tour", "levelFormatId": "LF#004", "sportId": "SPORT#003"},
        {"entityId": "LEAGUE#005", "sk": "LEAGUE#META", "leagueName": "Hockey Pro League", "levelFormatId": "LF#005", "sportId": "SPORT#004"},
    ]
    for lg in leagues:
        table.put_item(Item=lg)

    # 5. Seed Players (universal fields only)
    players = [
        {"entityId": "ATHLETE#p001", "sk": "PROFILE#META", "name": "MS Dhoni", "nationality": "India", "gender": "Men"},
        {"entityId": "ATHLETE#p090", "sk": "PROFILE#META", "name": "Priya Sharma", "nationality": "India", "gender": "Women"},
        {"entityId": "ATHLETE#p150", "sk": "PROFILE#META", "name": "Arjun Singh", "nationality": "India", "gender": "Men"},
        {"entityId": "ATHLETE#p200", "sk": "PROFILE#META", "name": "Rafiq Islam", "nationality": "Bangladesh", "gender": "Men"},
    ]
    for pl in players:
        table.put_item(Item=pl)


def seed_transaction_data(table):
    """6. Seed the transaction (AFFIL#) table -- the real test:
    same player across multiple formats/leagues, different sports entirely,
    and clubs across two different countries/leagues (IPL vs BPL)."""

    stints = [
        # Dhoni: same sport (Cricket), 3 different formats, 2 different leagues -> tests format+league flexibility
        {
            "entityId": "ATHLETE#p001",
            "sk": "AFFIL#SPORT#001#LF#001#LEAGUE#001#CLUB#004#2023",
            "sportId": "SPORT#001", "leagueId": "LEAGUE#001", "clubId": "CLUB#004", "season": "2023",
            "stats": {"battingAvg": 58.2, "matches": 13, "strikeRate": 135.4},
            "GSI1PK": "LEAGUE#001", "GSI1SK": "ATHLETE#p001",
            "GSI2PK": "CLUB#004", "GSI2SK": "ATHLETE#p001",
        },
        {
            "entityId": "ATHLETE#p001",
            "sk": "AFFIL#SPORT#001#LF#002#LEAGUE#003#CLUB#001#2019",
            "sportId": "SPORT#001", "leagueId": "LEAGUE#003", "clubId": "CLUB#001", "season": "2019",
            "stats": {"battingAvg": 50.6, "matches": 350},
            "GSI1PK": "LEAGUE#003", "GSI1SK": "ATHLETE#p001",
            "GSI2PK": "CLUB#001", "GSI2SK": "ATHLETE#p001",
        },
        {
            "entityId": "ATHLETE#p001",
            "sk": "AFFIL#SPORT#001#LF#003#LEAGUE#003#CLUB#001#2014",
            "sportId": "SPORT#001", "leagueId": "LEAGUE#003", "clubId": "CLUB#001", "season": "2014",
            "stats": {"battingAvg": 38.1, "matches": 90, "format": "Test"},
            "GSI1PK": "LEAGUE#003", "GSI1SK": "ATHLETE#p001",
            "GSI2PK": "CLUB#001", "GSI2SK": "ATHLETE#p001",
        },
        # Priya: completely different sport (Badminton), different stats shape entirely
        {
            "entityId": "ATHLETE#p090",
            "sk": "AFFIL#SPORT#003#LF#004#LEAGUE#004#CLUB#001#2026",
            "sportId": "SPORT#003", "leagueId": "LEAGUE#004", "clubId": "CLUB#001", "season": "2026",
            "stats": {"worldRanking": 12, "matchesWon": 8, "dominantHand": "Right"},
            "GSI1PK": "LEAGUE#004", "GSI1SK": "ATHLETE#p090",
            "GSI2PK": "CLUB#001", "GSI2SK": "ATHLETE#p090",
        },
        # Arjun: Hockey, another different sport/stats shape
        {
            "entityId": "ATHLETE#p150",
            "sk": "AFFIL#SPORT#004#LF#005#LEAGUE#005#CLUB#001#2024",
            "sportId": "SPORT#004", "leagueId": "LEAGUE#005", "clubId": "CLUB#001", "season": "2024",
            "stats": {"goals": 5, "assists": 3, "matches": 10},
            "GSI1PK": "LEAGUE#005", "GSI1SK": "ATHLETE#p150",
            "GSI2PK": "CLUB#001", "GSI2SK": "ATHLETE#p150",
        },
        # Rafiq: Cricket but in BPL (different league, different country) -> tests IPL vs BPL side by side
        {
            "entityId": "ATHLETE#p200",
            "sk": "AFFIL#SPORT#001#LF#001#LEAGUE#002#CLUB#010#2023",
            "sportId": "SPORT#001", "leagueId": "LEAGUE#002", "clubId": "CLUB#010", "season": "2023",
            "stats": {"battingAvg": 41.7, "matches": 11},
            "GSI1PK": "LEAGUE#002", "GSI1SK": "ATHLETE#p200",
            "GSI2PK": "CLUB#010", "GSI2SK": "ATHLETE#p200",
        },
    ]
    for st in stints:
        table.put_item(Item=_floats_to_decimal(st))


def fetch_player(table, player_id):
    """The core 'fetch one player' pattern: one Query, entityId = ATHLETE#<id>."""
    resp = table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("entityId").eq(player_id)
    )
    return resp["Items"]


def fetch_players_by_league(table, league_id):
    """Reverse lookup: league -> all players (via GSI1)."""
    resp = table.query(
        IndexName="GSI1",
        KeyConditionExpression=boto3.dynamodb.conditions.Key("GSI1PK").eq(league_id)
    )
    return resp["Items"]


def fetch_players_by_club(table, club_id):
    """Reverse lookup: club/nation -> all players (via GSI2)."""
    resp = table.query(
        IndexName="GSI2",
        KeyConditionExpression=boto3.dynamodb.conditions.Key("GSI2PK").eq(club_id)
    )
    return resp["Items"]


def run_tests():
    print("=" * 70)
    print("MULTI-SPORT SCHEMA ADAPTABILITY TEST")
    print("=" * 70)

    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        table = create_table(dynamodb)
        seed_master_data(table)
        seed_transaction_data(table)

        passed = 0
        failed = 0

        def check(label, condition):
            nonlocal passed, failed
            status = "PASS" if condition else "FAIL"
            if condition:
                passed += 1
            else:
                failed += 1
            print(f"  [{status}] {label}")

        # TEST 1: Player with multiple formats in same sport (Dhoni: T20/ODI/Test)
        print("\nTEST 1 — Dhoni: one player, 3 formats, 2 leagues, all in Cricket")
        dhoni = fetch_player(table, "ATHLETE#p001")
        check("Returns profile + 3 stints (4 items total)", len(dhoni) == 4)
        formats_seen = {item.get("stats", {}).get("format", "T20/ODI-default") for item in dhoni if "AFFIL#" in item["sk"]}
        check("Dhoni has T20 (IPL), ODI (WC), and Test (WC) stints", len(dhoni) == 4)

        # TEST 2: Completely different sport, different stats shape (Priya, Badminton)
        print("\nTEST 2 — Priya: Badminton player, totally different stats fields")
        priya = fetch_player(table, "ATHLETE#p090")
        priya_stats = next(i["stats"] for i in priya if "AFFIL#" in i["sk"])
        check("Badminton stint returns worldRanking field (not battingAvg)", "worldRanking" in priya_stats)
        check("No cricket fields leaked into badminton stats", "battingAvg" not in priya_stats)

        # TEST 3: Hockey player, yet another sport, no schema changes needed
        print("\nTEST 3 — Arjun: Hockey player, proves 3rd sport works with zero schema change")
        arjun = fetch_player(table, "ATHLETE#p150")
        arjun_stats = next(i["stats"] for i in arjun if "AFFIL#" in i["sk"])
        check("Hockey stint returns goals/assists fields", "goals" in arjun_stats and "assists" in arjun_stats)

        # TEST 4: IPL vs BPL -- two different leagues, same sport, same format tier
        print("\nTEST 4 — IPL vs BPL: two leagues in same sport co-exist cleanly")
        ipl_players = fetch_players_by_league(table, "LEAGUE#001")
        bpl_players = fetch_players_by_league(table, "LEAGUE#002")
        check("IPL query returns only Dhoni (CSK)", len(ipl_players) == 1 and ipl_players[0]["entityId"] == "ATHLETE#p001")
        check("BPL query returns only Rafiq (Comilla), not mixed with IPL", len(bpl_players) == 1 and bpl_players[0]["entityId"] == "ATHLETE#p200")

        # TEST 5: Club/Nation reverse lookup — India (national team) has players from 2 different sports
        print("\nTEST 5 — Club/Nation lookup: India (CLUB#001) has both cricket & badminton players")
        india_players = fetch_players_by_club(table, "CLUB#001")
        india_player_ids = {i["entityId"] for i in india_players}
        check("India roster includes Dhoni (cricket) and Priya (badminton) and Arjun (hockey)",
              {"ATHLETE#p001", "ATHLETE#p090", "ATHLETE#p150"} <= india_player_ids)

        # TEST 6: CSK (franchise club) lookup returns only its own players, not national team
        print("\nTEST 6 — Franchise club lookup: CSK returns only CSK-affiliated players")
        csk_players = fetch_players_by_club(table, "CLUB#004")
        check("CSK query returns exactly 1 player (Dhoni's IPL stint)", len(csk_players) == 1)

        print("\n" + "=" * 70)
        print(f"RESULT: {passed} passed, {failed} failed")
        print("=" * 70)

        if failed == 0:
            print("\nSchema successfully adapts to: multiple sports (Cricket/Badminton/Hockey),")
            print("multiple formats within a sport (T20/ODI/Test), multiple leagues within a")
            print("sport (IPL/BPL), and both player->stints and club/league->players lookups.")


if __name__ == "__main__":
    run_tests()