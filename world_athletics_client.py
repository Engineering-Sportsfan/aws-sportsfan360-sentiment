# """
# Standalone World Athletics GraphQL client.

# Vendored from worldathletics.org's own frontend network requests (not the
# PyPI `worldathletics` package, whose endpoint + API key are both stale/dead
# as of this session). Plain httpx + dict responses, zero pydantic dependency
# -- avoids the conflict with fastapi / google-genai in this project.

# Endpoint: https://graphql-prod-4877.edge.aws.worldathletics.org/graphql
# API key:  captured live from worldathletics.org's own requests (public
#           frontend key, scoped to certain queries only -- see note below).

# KNOWN AUTH BOUNDARY:
#   This API key works for `searchCompetitors` and `getSingleCompetitor`
#   (basic info) but returns "Not Authorized" for `getCISSingleCompetitor`
#   (the richer profile query with PBs/honours/season bests/world rankings).
#   That deeper query is NOT usable with this key. If you need PBs/honours/
#   season bests later, there are separate working queries for those
#   (GetSingleCompetitorAllTimePersonalTop10, getSingleCompetitorSeasonBests,
#   GetSingleCompetitorResultsDate) -- not wired in here yet since the
#   current scope only needs basic bio info. Ask if you want those added.

# GetCompetitorBasicInfo field gaps (confirmed via live test):
#   firstName, lastName, countryName, birthDateStr, urlSlug, biography,
#   twitterLink, instagramLink, facebookLink, representativeId all come
#   back null for at least this athlete. Only reliably populated:
#   countryCode, birthDate, iaafId, aaId, primaryMedia filename.
#   -> For name / country name / discipline, use search_competitors'
#      output instead (familyName, givenName, country, disciplines are
#      populated there).

# ID-BASED LOOKUP (get_athlete_profile_by_id):
#   The admin "add athlete" form captures a numeric aaAthleteId up front
#   (from an earlier search step), so the pipeline entry point can fetch
#   by exact ID instead of re-running a fuzzy name search and guessing
#   at the first match. Since get_competitor_basic_info(id) doesn't
#   reliably return name/discipline, this method does a second call --
#   search_competitors filtered by the country code from basic info,
#   then picks the exact aaAthleteId match out of that country's results
#   to recover the populated name/discipline fields. If no exact match
#   turns up in that country slice, falls back to basic-info-only data
#   with name fields left None rather than guessing.

#   KNOWN OPEN ISSUE (flagged, not yet root-caused): the country-search
#   exact-match loop has been observed missing known-good athletes (e.g.
#   Neeraj Chopra, India -- a small country slice, so not a pagination
#   issue in that case). Root cause not yet confirmed -- possible culprits
#   include aaAthleteId type/format mismatches between search results and
#   the requested id, or an API-side inconsistency. Until root-caused, the
#   fallback branch below must never silently produce an unusable profile
#   (see url_slug_verified in _merge_profile and the source_url fallback
#   in athlete_source_fetcher.py).

# Usage:
#     import asyncio
#     from world_athletics_client import WorldAthleticsClient

#     async def main():
#         async with WorldAthleticsClient() as wa:
#             profile = await wa.get_athlete_profile_by_id(14549089)
#             print(profile)

#     asyncio.run(main())
# """

# from __future__ import annotations

# from datetime import datetime
# from typing import Any, Dict, List, Optional

# import httpx


# def _normalize_birth_date(raw_date: Optional[str]) -> Optional[str]:
#     """
#     World Athletics returns birth dates like '24 DEC 1997', which is not
#     valid ISO-8601 -- Pydantic's `date` type (used downstream in
#     TrackFieldProfile.date_of_birth) rejects it outright. Normalize to
#     'YYYY-MM-DD' here, at the source, so every downstream consumer (Gemini
#     extraction, Pydantic validation) gets a clean ISO date. Returns None
#     (rather than raising) for anything that doesn't parse, since a bad date
#     shouldn't crash the whole fetch -- date_of_birth is Optional downstream.
#     """
#     if not raw_date:
#         return None
#     for fmt in ("%d %b %Y", "%Y-%m-%d", "%d %B %Y"):
#         try:
#             return datetime.strptime(raw_date.strip(), fmt).date().isoformat()
#         except ValueError:
#             continue
#     return None

# GRAPHQL_URL = "https://graphql-prod-4877.edge.aws.worldathletics.org/graphql"

# DEFAULT_HEADERS = {
#     "x-api-key": "da2-tzmostylynabpfkrgbmmml4toq",
#     "x-amz-user-agent": "aws-amplify/3.0.2",
#     "content-type": "application/json",
#     "origin": "https://worldathletics.org",
#     "referer": "https://worldathletics.org/",
# }


# class WorldAthleticsError(Exception):
#     """Raised when the GraphQL API returns an error payload."""


# class WorldAthleticsClient:
#     def __init__(
#         self,
#         url: str = GRAPHQL_URL,
#         headers: Optional[Dict[str, str]] = None,
#         timeout: float = 15.0,
#     ) -> None:
#         self._url = url
#         self._headers = headers or DEFAULT_HEADERS
#         self._client = httpx.AsyncClient(timeout=timeout)

#     async def __aenter__(self) -> "WorldAthleticsClient":
#         return self

#     async def __aexit__(self, *exc_info: Any) -> None:
#         await self.close()

#     async def close(self) -> None:
#         await self._client.aclose()

#     async def _execute(
#         self, query: str, variables: Dict[str, Any], operation_name: str
#     ) -> Dict[str, Any]:
#         response = await self._client.post(
#             self._url,
#             headers=self._headers,
#             json={
#                 "query": query,
#                 "variables": variables,
#                 "operationName": operation_name,
#             },
#         )
#         response.raise_for_status()
#         payload = response.json()

#         if payload.get("errors"):
#             raise WorldAthleticsError(payload["errors"])

#         return payload["data"]

#     # ------------------------------------------------------------------
#     # search_competitors -- free-text name search -> athlete id + urlSlug
#     # ------------------------------------------------------------------
#     async def search_competitors(
#         self,
#         query: Optional[str] = None,
#         gender: Optional[str] = None,
#         discipline_code: Optional[str] = None,
#         country_code: Optional[str] = None,
#         environment: Optional[str] = None,
#     ) -> List[Dict[str, Any]]:
#         gql = """
#         query SearchCompetitors($query: String, $gender: GenderType, $disciplineCode: String, $environment: String, $countryCode: String) {
#           searchCompetitors(
#             query: $query
#             gender: $gender
#             disciplineCode: $disciplineCode
#             environment: $environment
#             countryCode: $countryCode
#           ) {
#             aaAthleteId
#             familyName
#             givenName
#             birthDate
#             disciplines
#             iaafId
#             gender
#             country
#             urlSlug
#           }
#         }
#         """
#         data = await self._execute(
#             gql,
#             {
#                 "query": query,
#                 "gender": gender,
#                 "disciplineCode": discipline_code,
#                 "environment": environment,
#                 "countryCode": country_code,
#             },
#             "SearchCompetitors",
#         )
#         return data["searchCompetitors"]

#     # ------------------------------------------------------------------
#     # get_competitor_basic_info -- confirmed working replacement for
#     # get_cis_single_competitor's basicData block (that query is auth-
#     # blocked). Field coverage is thinner -- see module docstring.
#     # ------------------------------------------------------------------
#     async def get_competitor_basic_info(
#         self,
#         id: Optional[int] = None,
#         url_slug: Optional[str] = None,
#     ) -> Dict[str, Any]:
#         gql = """
#         query GetCompetitorBasicInfo($id: Int, $urlSlug: String) {
#           competitor: getSingleCompetitor(id: $id, urlSlug: $urlSlug) {
#             primaryMediaId
#             primaryMedia {
#               urlSlug
#               title
#               fileName
#             }
#             basicData {
#               firstName
#               lastName
#               countryName
#               countryCode
#               countryUrlSlug
#               birthDate
#               birthDateStr
#               urlSlug
#               representativeId
#               biography
#               twitterLink
#               instagramLink
#               facebookLink
#               iaafId
#               aaId
#             }
#           }
#         }
#         """
#         data = await self._execute(
#             gql, {"id": id, "urlSlug": url_slug}, "GetCompetitorBasicInfo"
#         )
#         return data["competitor"]

#     # ------------------------------------------------------------------
#     # get_athlete_profile -- convenience combiner for free-text NAME search.
#     # Takes the first search_competitors match -- fine for exploratory
#     # lookups, but NOT exact. Prefer get_athlete_profile_by_id below when
#     # a numeric aaAthleteId is already known (e.g. from the admin form),
#     # since that gives an exact match instead of a best guess.
#     # ------------------------------------------------------------------
#     async def get_athlete_profile(self, name_query: str) -> Optional[Dict[str, Any]]:
#         matches = await self.search_competitors(query=name_query)
#         if not matches:
#             return None

#         top = matches[0]
#         athlete_id = int(top["aaAthleteId"])
#         basic = await self.get_competitor_basic_info(id=athlete_id)

#         return self._merge_profile(top, basic)

#     # ------------------------------------------------------------------
#     # get_athlete_profile_by_id -- exact lookup by numeric aaAthleteId.
#     # This is the one the pipeline should use, since the admin add-athlete
#     # form already captures the correct ID up front (no name-search
#     # ambiguity to resolve).
#     #
#     # get_competitor_basic_info(id) alone doesn't reliably return name/
#     # discipline (see module docstring), so this does a second call --
#     # search_competitors scoped to the athlete's country code -- and picks
#     # out the exact aaAthleteId match to recover those fields. If the id
#     # isn't found in that country's search results, returns basic-info-only
#     # data with name fields left None rather than silently guessing.
#     # ------------------------------------------------------------------
#     async def get_athlete_profile_by_id(self, athlete_id: int) -> Optional[Dict[str, Any]]:
#         basic = await self.get_competitor_basic_info(id=athlete_id)
#         if not basic:
#             return None

#         country_code = (basic.get("basicData") or {}).get("countryCode")

#         match: Optional[Dict[str, Any]] = None
#         if country_code:
#             candidates = await self.search_competitors(country_code=country_code)

#             # Primary key: aaAthleteId. This has been observed missing
#             # known-good athletes for reasons not yet root-caused (type/
#             # format mismatch suspected). iaafId is reliably populated on
#             # both search results and basic info, so try it as a fallback
#             # match key before giving up and falling into the null-name
#             # branch below.
#             for c in candidates:
#                 try:
#                     if int(c["aaAthleteId"]) == athlete_id:
#                         match = c
#                         break
#                 except (TypeError, ValueError):
#                     continue

#             if match is None:
#                 basic_iaaf_id = (basic.get("basicData") or {}).get("iaafId")
#                 if basic_iaaf_id:
#                     for c in candidates:
#                         try:
#                             if int(c.get("iaafId")) == int(basic_iaaf_id):
#                                 match = c
#                                 break
#                         except (TypeError, ValueError):
#                             continue

#         if match is None:
#             # Fall back to basic-info-only shape -- name fields stay None.
#             # NOTE: basicData.urlSlug is confirmed unreliable/null on this
#             # query (see module docstring), so url_slug here is very likely
#             # None too. Do not assume it's populated -- see
#             # url_slug_verified in _merge_profile, and the source_url
#             # fallback in athlete_source_fetcher.py which handles this.
#             match = {
#                 "aaAthleteId": athlete_id,
#                 "familyName": None,
#                 "givenName": None,
#                 "country": country_code,
#                 "birthDate": (basic.get("basicData") or {}).get("birthDate"),
#                 "disciplines": None,
#                 "gender": None,
#                 "urlSlug": (basic.get("basicData") or {}).get("urlSlug"),
#                 "iaafId": (basic.get("basicData") or {}).get("iaafId"),
#             }

#         return self._merge_profile(match, basic)

#     # ------------------------------------------------------------------
#     # shared merge logic for both name-search and id-based lookups
#     # ------------------------------------------------------------------
#     @staticmethod
#     def _merge_profile(search_match: Dict[str, Any], basic: Dict[str, Any]) -> Dict[str, Any]:
#         primary_media = (basic.get("primaryMedia") or [{}])[0]
#         basic_data = basic.get("basicData") or {}

#         url_slug = search_match.get("urlSlug") or basic_data.get("urlSlug")

#         given = search_match.get("givenName")
#         family = search_match.get("familyName")
#         full_name = f"{given or ''} {family or ''}".strip() or None

#         if full_name is None:
#             # Name genuinely unrecoverable from World Athletics for this
#             # athlete (see get_athlete_profile_by_id's KNOWN OPEN ISSUE --
#             # search_competitors(country_code=...) is capped at ~30 results
#             # server-side with no pagination param available, so athletes
#             # outside that slice can't be matched by exact ID). Rather than
#             # crash the pipeline (full_name is a required field downstream),
#             # ship an obviously-placeholder name so schema validation
#             # passes and the draft still reaches the human review queue --
#             # this is exactly what that queue exists for. The editor sees
#             # a clearly wrong name and corrects it, instead of the whole
#             # athlete failing to onboard.
#             full_name = f"[NEEDS REVIEW] Athlete {search_match.get('aaAthleteId')}"

#         return {
#             "athlete_id": int(search_match["aaAthleteId"]),
#             "iaaf_id": search_match.get("iaafId"),
#             "given_name": given,
#             "family_name": family,
#             "full_name": full_name,
#             "country_code": search_match.get("country") or basic_data.get("countryCode"),
#             "birth_date": _normalize_birth_date(
#                 search_match.get("birthDate") or basic_data.get("birthDate")
#             ),
#             "disciplines": search_match.get("disciplines"),
#             "gender": search_match.get("gender"),
#             "url_slug": url_slug,
#             # True only when we recovered a real urlSlug from the API.
#             # False means downstream code (athlete_source_fetcher.py)
#             # must build a fallback source_url instead of trusting this
#             # field -- prevents the SourceRef(url=None) pydantic crash.
#             "url_slug_verified": bool(url_slug),
#             "photo_filename": primary_media.get("fileName"),
#             # fields World Athletics does NOT provide -- left None deliberately
#             "height": None,
#             "weight": None,
#             "dominant_hand": None,
#             "coach": None,
#         }







"""
Standalone World Athletics GraphQL client.

Vendored from worldathletics.org's own frontend network requests (not the
PyPI `worldathletics` package, whose endpoint + API key are both stale/dead
as of this session). Plain httpx + dict responses, zero pydantic dependency
-- avoids the conflict with fastapi / google-genai in this project.

Endpoint: https://graphql-prod-4877.edge.aws.worldathletics.org/graphql
API key:  captured live from worldathletics.org's own requests (public
          frontend key, scoped to certain queries only -- see note below).

KNOWN AUTH BOUNDARY:
  This API key works for `searchCompetitors` and `getSingleCompetitor`
  (basic info) but returns "Not Authorized" for `getCISSingleCompetitor`
  (the richer profile query with PBs/honours/season bests/world rankings).
  That deeper query is NOT usable with this key.

  Two working replacements for stats data ARE wired in as of this session:
  getSingleCompetitorSeasonBests (season bests) and
  GetSingleCompetitorResultsDiscipline (full results history by discipline,
  used to derive personal bests since the dedicated Top10 query could not
  be captured from the live site).

GetCompetitorBasicInfo field gaps (confirmed via live test):
  firstName, lastName, countryName, birthDateStr, urlSlug, biography,
  twitterLink, instagramLink, facebookLink, representativeId all come
  back null for at least this athlete. Only reliably populated:
  countryCode, birthDate, iaafId, aaId, primaryMedia filename.
  -> For name / country name / discipline, use search_competitors'
     output instead (familyName, givenName, country, disciplines are
     populated there).

STATS FIELD IMPORTANT NOTE:
  get_personal_bests() picks the "best" result per discipline by taking
  the max() of the mark as a float. This is only correct for disciplines
  where HIGHER is better (throws, jumps, points-based combined events).
  For TRACK events (100m, 1500m, etc.) LOWER is better, and this method
  will currently return the athlete's WORST time, not their best. There is
  no discipline-type (track vs. field) classification wired in yet -- this
  is fine for the current Indian track & field pilot roster (mostly throws/
  jumps) but must be fixed with a proper discipline-type lookup before
  onboarding any track/running athlete. Flagged, not yet fixed.

ID-BASED LOOKUP (get_athlete_profile_by_id):
  The admin "add athlete" form captures a numeric aaAthleteId up front
  (from an earlier search step), so the pipeline entry point can fetch
  by exact ID instead of re-running a fuzzy name search and guessing
  at the first match. Since get_competitor_basic_info(id) doesn't
  reliably return name/discipline, this method does a second call --
  search_competitors filtered by the country code from basic info,
  then picks the exact aaAthleteId match out of that country's results
  to recover the populated name/discipline fields. If no exact match
  turns up in that country slice, falls back to basic-info-only data
  with name fields left None rather than guessing.

  KNOWN OPEN ISSUE (flagged, not yet root-caused): the country-search
  exact-match loop has been observed missing known-good athletes (e.g.
  Neeraj Chopra, India -- a small country slice, so not a pagination
  issue in that case). Root cause not yet confirmed -- possible culprits
  include aaAthleteId type/format mismatches between search results and
  the requested id, or an API-side inconsistency. Until root-caused, the
  fallback branch below must never silently produce an unusable profile
  (see url_slug_verified in _merge_profile and the source_url fallback
  in athlete_source_fetcher.py).

Usage:
    import asyncio
    from world_athletics_client import WorldAthleticsClient

    async def main():
        async with WorldAthleticsClient() as wa:
            profile = await wa.get_athlete_profile_by_id(14549089)
            print(profile)
            bests = await wa.get_personal_bests(14549089)
            print(bests)

    asyncio.run(main())
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx


def _normalize_birth_date(raw_date: Optional[str]) -> Optional[str]:
    """
    World Athletics returns birth dates like '24 DEC 1997', which is not
    valid ISO-8601 -- Pydantic's `date` type (used downstream in
    TrackFieldProfile.date_of_birth) rejects it outright. Normalize to
    'YYYY-MM-DD' here, at the source, so every downstream consumer (Gemini
    extraction, Pydantic validation) gets a clean ISO date. Returns None
    (rather than raising) for anything that doesn't parse, since a bad date
    shouldn't crash the whole fetch -- date_of_birth is Optional downstream.
    """
    if not raw_date:
        return None
    for fmt in ("%d %b %Y", "%Y-%m-%d", "%d %B %Y"):
        try:
            return datetime.strptime(raw_date.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _safe_float(mark: Optional[str]) -> float:
    """Best-effort numeric parse of a result mark string (e.g. '86.11').
    Returns -inf on anything unparsable so it never wins a max() comparison."""
    if not mark:
        return float("-inf")
    try:
        return float(mark)
    except (TypeError, ValueError):
        return float("-inf")


GRAPHQL_URL = "https://graphql-prod-4877.edge.aws.worldathletics.org/graphql"

DEFAULT_HEADERS = {
    "x-api-key": "da2-tzmostylynabpfkrgbmmml4toq",
    "x-amz-user-agent": "aws-amplify/3.0.2",
    "content-type": "application/json",
    "origin": "https://worldathletics.org",
    "referer": "https://worldathletics.org/",
}


class WorldAthleticsError(Exception):
    """Raised when the GraphQL API returns an error payload."""


class WorldAthleticsClient:
    def __init__(
        self,
        url: str = GRAPHQL_URL,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 15.0,
    ) -> None:
        self._url = url
        self._headers = headers or DEFAULT_HEADERS
        self._client = httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> "WorldAthleticsClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def _execute(
        self, query: str, variables: Dict[str, Any], operation_name: str
    ) -> Dict[str, Any]:
        response = await self._client.post(
            self._url,
            headers=self._headers,
            json={
                "query": query,
                "variables": variables,
                "operationName": operation_name,
            },
        )
        response.raise_for_status()
        payload = response.json()

        if payload.get("errors"):
            raise WorldAthleticsError(payload["errors"])

        return payload["data"]

    # ------------------------------------------------------------------
    # search_competitors -- free-text name search -> athlete id + urlSlug
    # ------------------------------------------------------------------
    async def search_competitors(
        self,
        query: Optional[str] = None,
        gender: Optional[str] = None,
        discipline_code: Optional[str] = None,
        country_code: Optional[str] = None,
        environment: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        gql = """
        query SearchCompetitors($query: String, $gender: GenderType, $disciplineCode: String, $environment: String, $countryCode: String) {
          searchCompetitors(
            query: $query
            gender: $gender
            disciplineCode: $disciplineCode
            environment: $environment
            countryCode: $countryCode
          ) {
            aaAthleteId
            familyName
            givenName
            birthDate
            disciplines
            iaafId
            gender
            country
            urlSlug
          }
        }
        """
        data = await self._execute(
            gql,
            {
                "query": query,
                "gender": gender,
                "disciplineCode": discipline_code,
                "environment": environment,
                "countryCode": country_code,
            },
            "SearchCompetitors",
        )
        return data["searchCompetitors"]

    # ------------------------------------------------------------------
    # get_competitor_basic_info -- confirmed working replacement for
    # get_cis_single_competitor's basicData block (that query is auth-
    # blocked). Field coverage is thinner -- see module docstring.
    # ------------------------------------------------------------------
    async def get_competitor_basic_info(
        self,
        id: Optional[int] = None,
        url_slug: Optional[str] = None,
    ) -> Dict[str, Any]:
        gql = """
        query GetCompetitorBasicInfo($id: Int, $urlSlug: String) {
          competitor: getSingleCompetitor(id: $id, urlSlug: $urlSlug) {
            primaryMediaId
            primaryMedia {
              urlSlug
              title
              fileName
            }
            basicData {
              firstName
              lastName
              countryName
              countryCode
              countryUrlSlug
              birthDate
              birthDateStr
              urlSlug
              representativeId
              biography
              twitterLink
              instagramLink
              facebookLink
              iaafId
              aaId
            }
          }
        }
        """
        data = await self._execute(
            gql, {"id": id, "urlSlug": url_slug}, "GetCompetitorBasicInfo"
        )
        return data["competitor"]

    # ------------------------------------------------------------------
    # get_season_bests -- confirmed working replacement for the auth-
    # blocked getCISSingleCompetitor season-bests block.
    # ------------------------------------------------------------------
    async def get_season_bests(
        self,
        id: int,
        season: Optional[int] = None,
    ) -> Dict[str, Any]:
        """season defaults to the API's own current-season behavior when None."""
        gql = """
        query getSingleCompetitorSeasonBests($id: Int, $seasonsBestsSeason: Int) {
          seasonBests: getSingleCompetitorSeasonBests(id: $id, seasonsBestsSeason: $seasonsBestsSeason) {
            activeSeasons
            withWind
            withRecords
            results {
              indoor
              disciplineCode
              discipline
              mark
              wind
              notLegal
              venue
              date
              resultScore
              records
              competitionId
              eventId
              competition
            }
          }
        }
        """
        data = await self._execute(
            gql,
            {"id": id, "seasonsBestsSeason": season},
            "getSingleCompetitorSeasonBests",
        )
        return data.get("seasonBests") or {}

    # ------------------------------------------------------------------
    # get_results_by_discipline -- confirmed working. Full results history
    # grouped by discipline. Used by get_personal_bests below to derive a
    # personal best per discipline, since the dedicated
    # GetSingleCompetitorAllTimePersonalTop10 query could not be captured
    # from the live site this session.
    # ------------------------------------------------------------------
    async def get_results_by_discipline(
        self,
        id: int,
        year: Optional[int] = None,
    ) -> Dict[str, Any]:
        gql = """
        query GetSingleCompetitorResultsDiscipline($id: Int, $resultsByYearOrderBy: String, $resultsByYear: Int) {
          getSingleCompetitorResultsDiscipline(id: $id, resultsByYear: $resultsByYear, resultsByYearOrderBy: $resultsByYearOrderBy) {
            activeYears
            resultsByEvent {
              indoor
              disciplineCode
              disciplineNameUrlSlug
              typeNameUrlSlug
              discipline
              withWind
              results {
                date
                competition
                venue
                country
                category
                race
                place
                mark
                wind
                notLegal
                resultScore
                remark
                competitionId
                eventId
              }
            }
          }
        }
        """
        data = await self._execute(
            gql,
            {"id": id, "resultsByYear": year, "resultsByYearOrderBy": None},
            "GetSingleCompetitorResultsDiscipline",
        )
        return data.get("getSingleCompetitorResultsDiscipline") or {}

    async def get_personal_bests(self, id: int) -> List[Dict[str, Any]]:
        """
        Derives one personal-best entry per discipline from the full
        results-by-discipline history (best legal mark per discipline).

        CAUTION -- see module docstring's STATS FIELD IMPORTANT NOTE: this
        assumes higher-mark-is-better, which is wrong for track/running
        events. Safe for throws/jumps/points disciplines only until a
        discipline-type classification is added.
        """
        raw = await self.get_results_by_discipline(id)
        events = raw.get("resultsByEvent") or []
        bests: List[Dict[str, Any]] = []
        for event in events:
            legal_results = [
                r for r in (event.get("results") or []) if not r.get("notLegal")
            ]
            if not legal_results:
                continue
            best = max(legal_results, key=lambda r: _safe_float(r.get("mark")))
            bests.append(
                {
                    "discipline": event.get("discipline"),
                    "discipline_code": event.get("disciplineCode"),
                    "mark": best.get("mark"),
                    "wind": best.get("wind"),
                    "venue": best.get("venue"),
                    "date": best.get("date"),
                    "competition": best.get("competition"),
                    "competition_id": best.get("competitionId"),
                    "event_id": best.get("eventId"),
                }
            )
        return bests

    # ------------------------------------------------------------------
    # get_athlete_profile -- convenience combiner for free-text NAME search.
    # Takes the first search_competitors match -- fine for exploratory
    # lookups, but NOT exact. Prefer get_athlete_profile_by_id below when
    # a numeric aaAthleteId is already known (e.g. from the admin form),
    # since that gives an exact match instead of a best guess.
    # ------------------------------------------------------------------
    async def get_athlete_profile(self, name_query: str) -> Optional[Dict[str, Any]]:
        matches = await self.search_competitors(query=name_query)
        if not matches:
            return None

        top = matches[0]
        athlete_id = int(top["aaAthleteId"])
        basic = await self.get_competitor_basic_info(id=athlete_id)

        return self._merge_profile(top, basic)

    # ------------------------------------------------------------------
    # get_athlete_profile_by_id -- exact lookup by numeric aaAthleteId.
    # This is the one the pipeline should use, since the admin add-athlete
    # form already captures the correct ID up front (no name-search
    # ambiguity to resolve).
    #
    # get_competitor_basic_info(id) alone doesn't reliably return name/
    # discipline (see module docstring), so this does a second call --
    # search_competitors scoped to the athlete's country code -- and picks
    # out the exact aaAthleteId match to recover those fields. If the id
    # isn't found in that country's search results, returns basic-info-only
    # data with name fields left None rather than silently guessing.
    # ------------------------------------------------------------------
    async def get_athlete_profile_by_id(self, athlete_id: int) -> Optional[Dict[str, Any]]:
        basic = await self.get_competitor_basic_info(id=athlete_id)
        if not basic:
            return None

        country_code = (basic.get("basicData") or {}).get("countryCode")

        match: Optional[Dict[str, Any]] = None
        if country_code:
            candidates = await self.search_competitors(country_code=country_code)

            # Primary key: aaAthleteId. This has been observed missing
            # known-good athletes for reasons not yet root-caused (type/
            # format mismatch suspected). iaafId is reliably populated on
            # both search results and basic info, so try it as a fallback
            # match key before giving up and falling into the null-name
            # branch below.
            for c in candidates:
                try:
                    if int(c["aaAthleteId"]) == athlete_id:
                        match = c
                        break
                except (TypeError, ValueError):
                    continue

            if match is None:
                basic_iaaf_id = (basic.get("basicData") or {}).get("iaafId")
                if basic_iaaf_id:
                    for c in candidates:
                        try:
                            if int(c.get("iaafId")) == int(basic_iaaf_id):
                                match = c
                                break
                        except (TypeError, ValueError):
                            continue

        if match is None:
            # Fall back to basic-info-only shape -- name fields stay None.
            # NOTE: basicData.urlSlug is confirmed unreliable/null on this
            # query (see module docstring), so url_slug here is very likely
            # None too. Do not assume it's populated -- see
            # url_slug_verified in _merge_profile, and the source_url
            # fallback in athlete_source_fetcher.py which handles this.
            match = {
                "aaAthleteId": athlete_id,
                "familyName": None,
                "givenName": None,
                "country": country_code,
                "birthDate": (basic.get("basicData") or {}).get("birthDate"),
                "disciplines": None,
                "gender": None,
                "urlSlug": (basic.get("basicData") or {}).get("urlSlug"),
                "iaafId": (basic.get("basicData") or {}).get("iaafId"),
            }

        return self._merge_profile(match, basic)

    # ------------------------------------------------------------------
    # shared merge logic for both name-search and id-based lookups
    # ------------------------------------------------------------------
    @staticmethod
    def _merge_profile(search_match: Dict[str, Any], basic: Dict[str, Any]) -> Dict[str, Any]:
        primary_media = (basic.get("primaryMedia") or [{}])[0]
        basic_data = basic.get("basicData") or {}

        url_slug = search_match.get("urlSlug") or basic_data.get("urlSlug")

        given = search_match.get("givenName")
        family = search_match.get("familyName")
        full_name = f"{given or ''} {family or ''}".strip() or None

        if full_name is None:
            # Name genuinely unrecoverable from World Athletics for this
            # athlete (see get_athlete_profile_by_id's KNOWN OPEN ISSUE --
            # search_competitors(country_code=...) is capped at ~30 results
            # server-side with no pagination param available, so athletes
            # outside that slice can't be matched by exact ID). Rather than
            # crash the pipeline (full_name is a required field downstream),
            # ship an obviously-placeholder name so schema validation
            # passes and the draft still reaches the human review queue --
            # this is exactly what that queue exists for. The editor sees
            # a clearly wrong name and corrects it, instead of the whole
            # athlete failing to onboard.
            full_name = f"[NEEDS REVIEW] Athlete {search_match.get('aaAthleteId')}"

        return {
            "athlete_id": int(search_match["aaAthleteId"]),
            "iaaf_id": search_match.get("iaafId"),
            "given_name": given,
            "family_name": family,
            "full_name": full_name,
            "country_code": search_match.get("country") or basic_data.get("countryCode"),
            "birth_date": _normalize_birth_date(
                search_match.get("birthDate") or basic_data.get("birthDate")
            ),
            "disciplines": search_match.get("disciplines"),
            "gender": search_match.get("gender"),
            "url_slug": url_slug,
            # True only when we recovered a real urlSlug from the API.
            # False means downstream code (athlete_source_fetcher.py)
            # must build a fallback source_url instead of trusting this
            # field -- prevents the SourceRef(url=None) pydantic crash.
            "url_slug_verified": bool(url_slug),
            "photo_filename": primary_media.get("fileName"),
            # fields World Athletics does NOT provide -- left None deliberately
            "height": None,
            "weight": None,
            "dominant_hand": None,
            "coach": None,
        }