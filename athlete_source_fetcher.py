# """
# Layer 1 -- Data Ingestion. Licensed federation/rights-holder feeds ONLY.

# No unlicensed scraping -- this is a hard constraint (legal exposure already
# flagged in the platform's Data Source & Coverage Audit). Every fetcher here
# must correspond to an actual licensed feed/API agreement. Until a source is
# contractually confirmed for a given sport, its fetcher stays a stub that
# raises SourceFetchError rather than silently falling back to scraping.

# Track & field is wired to World Athletics via world_athletics_client.py --
# a standalone GraphQL client vendored from worldathletics.org's own frontend
# requests, using plain httpx instead of the abandoned `worldathletics` PyPI
# package (whose pinned pydantic==2.8.2 conflicts with fastapi/google-genai
# already installed in this project).

# Confirmed working end-to-end as of this session: endpoint, API key,
# search_competitors, get_competitor_basic_info, and both the free-text-name
# and exact-ID profile lookups in world_athletics_client.py.

# DISAMBIGUATION: the admin "add athlete" form captures a numeric World
# Athletics aaAthleteId up front (from an earlier search step done by the
# editor), so run_athlete_pipeline's `athlete_source_id` argument is that
# numeric ID -- not a name. fetch_track_field looks the athlete up by exact
# ID (get_athlete_profile_by_id) rather than re-running a fuzzy name search
# and guessing at the first match.

# SOURCE_URL FALLBACK: get_athlete_profile_by_id's country-search exact-match
# step has an open, not-yet-root-caused bug where it sometimes misses a known
# athlete and falls back to basic-info-only data -- which frequently has a
# null urlSlug (confirmed unreliable field on that query). Since SourceRef
# requires a real HttpUrl and previously crashed the whole pipeline on None,
# fetch_raw_athlete_data below always builds *some* valid source_url -- a
# direct profile link when url_slug was recovered, otherwise a search-by-id
# link -- so a urlSlug miss degrades provenance instead of crashing the run.
# """

# from __future__ import annotations

# import asyncio
# from datetime import datetime, timezone
# from typing import Any, Callable, Dict

# from world_athletics_client import WorldAthleticsClient, WorldAthleticsError


# class SourceFetchError(Exception):
#     """Raised when a licensed source fails to return usable athlete data."""


# class NoLicensedSourceError(SourceFetchError):
#     """Raised when no licensed source is configured yet for a given sport."""


# def _now_iso() -> str:
#     return datetime.now(timezone.utc).isoformat()


# async def _fetch_track_field_async(athlete_source_id: str) -> Dict[str, Any]:
#     try:
#         athlete_id = int(athlete_source_id)
#     except (TypeError, ValueError) as exc:
#         raise SourceFetchError(
#             f"Expected a numeric World Athletics aaAthleteId, got '{athlete_source_id}'"
#         ) from exc

#     async with WorldAthleticsClient() as wa:
#         try:
#             profile = await wa.get_athlete_profile_by_id(athlete_id)
#         except WorldAthleticsError as exc:
#             raise SourceFetchError(
#                 f"World Athletics API error for athlete id {athlete_id}: {exc}"
#             ) from exc

#         if profile is None:
#             raise SourceFetchError(f"No World Athletics athlete found for id {athlete_id}")

#         return profile


# def fetch_track_field(athlete_source_id: str) -> Dict[str, Any]:
#     """
#     Sync entry point. athlete_source_id is the numeric World Athletics
#     aaAthleteId (captured by the admin form at add-athlete time), looked up
#     exactly -- see module docstring for why this isn't a name search.
#     """
#     return asyncio.run(_fetch_track_field_async(athlete_source_id))


# def fetch_cricket(athlete_source_id: str) -> Dict[str, Any]:
#     raise NoLicensedSourceError(
#         f"No licensed source configured for cricket yet (requested id: '{athlete_source_id}')"
#     )


# def fetch_badminton(athlete_source_id: str) -> Dict[str, Any]:
#     raise NoLicensedSourceError(
#         f"No licensed source configured for badminton yet (requested id: '{athlete_source_id}')"
#     )


# # sport -> fetcher function. Each fetcher takes the source-specific athlete
# # identifier and returns a flat raw profile dict.
# SOURCE_FETCHERS: Dict[str, Callable[[str], Dict[str, Any]]] = {
#     "track_field": fetch_track_field,
#     "cricket": fetch_cricket,
#     "badminton": fetch_badminton,
# }

# _SOURCE_NAMES: Dict[str, str] = {
#     "track_field": "World Athletics",
#     "cricket": "N/A",
#     "badminton": "N/A",
# }


# def fetch_raw_athlete_data(sport: str, athlete_source_id: str) -> Dict[str, Any]:
#     """
#     Entry point used by athlete_pipeline.py. Raises SourceFetchError (not a
#     silent fallback) if no licensed fetcher exists yet for this sport.

#     Wraps the sport-specific fetcher's flat profile dict into the shape the
#     pipeline expects: {"source_name", "source_url", "fetched_at", "raw"}.
#     """
#     fetcher = SOURCE_FETCHERS.get(sport)
#     if not fetcher:
#         raise SourceFetchError(f"No licensed source fetcher registered for sport '{sport}'.")

#     raw = fetcher(athlete_source_id)

#     source_url = None
#     if sport == "track_field":
#         url_slug = raw.get("url_slug")
#         athlete_id = raw.get("athlete_id")
#         if url_slug:
#             source_url = f"https://worldathletics.org/athletes/{url_slug}"
#         elif athlete_id:
#             # url_slug wasn't recovered (see module docstring) -- fall back
#             # to a search-by-id link so provenance is never lost and the
#             # pipeline never crashes on a missing URL. Editors reviewing
#             # the draft can still click through to find the athlete.
#             source_url = f"https://worldathletics.org/search?query={athlete_id}"

#     if source_url is None:
#         # Should be unreachable for track_field given the athlete_id
#         # fallback above, but guard anyway rather than letting SourceRef
#         # crash three layers downstream with an opaque pydantic error.
#         raise SourceFetchError(
#             f"Could not construct a source_url for sport '{sport}', "
#             f"athlete_source_id '{athlete_source_id}' -- no url_slug or "
#             f"athlete_id available from the fetcher."
#         )

#     return {
#         "source_name": _SOURCE_NAMES.get(sport, sport),
#         "source_url": source_url,
#         "fetched_at": _now_iso(),
#         "raw": raw,
#     }




"""
Layer 1 -- Data Ingestion. Licensed federation/rights-holder feeds ONLY.

No unlicensed scraping -- this is a hard constraint (legal exposure already
flagged in the platform's Data Source & Coverage Audit). Every fetcher here
must correspond to an actual licensed feed/API agreement. Until a source is
contractually confirmed for a given sport, its fetcher stays a stub that
raises SourceFetchError rather than silently falling back to scraping.

Track & field is wired to World Athletics via world_athletics_client.py --
a standalone GraphQL client vendored from worldathletics.org's own frontend
requests, using plain httpx instead of the abandoned `worldathletics` PyPI
package (whose pinned pydantic==2.8.2 conflicts with fastapi/google-genai
already installed in this project).

Confirmed working end-to-end: endpoint, API key, search_competitors,
get_competitor_basic_info, both profile lookups, and (as of this session)
get_season_bests / get_personal_bests for stats data.

DISAMBIGUATION: the admin "add athlete" form captures a numeric World
Athletics aaAthleteId up front (from an earlier search step done by the
editor), so run_athlete_pipeline's `athlete_source_id` argument is that
numeric ID -- not a name. fetch_track_field looks the athlete up by exact
ID (get_athlete_profile_by_id) rather than re-running a fuzzy name search
and guessing at the first match.

SOURCE_URL FALLBACK: get_athlete_profile_by_id's country-search exact-match
step has an open, not-yet-root-caused bug where it sometimes misses a known
athlete and falls back to basic-info-only data -- which frequently has a
null urlSlug (confirmed unreliable field on that query). Since SourceRef
requires a real HttpUrl and previously crashed the whole pipeline on None,
fetch_raw_athlete_data below always builds *some* valid source_url -- a
direct profile link when url_slug was recovered, otherwise a search-by-id
link -- so a urlSlug miss degrades provenance instead of crashing the run.

STATS DATA: personal_bests and season_bests are fetched best-effort and
attached to the raw profile dict under those keys. If either call fails
(auth, network, no data for this athlete) the fetch does NOT fail the whole
run -- it logs nothing fatal and just leaves that key as an empty list, since
stats are an enrichment, not a required field for a valid draft. See
world_athletics_client.py's STATS FIELD IMPORTANT NOTE for the known
track-vs-field mark-direction caveat on personal_bests.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Dict

from world_athletics_client import WorldAthleticsClient, WorldAthleticsError


class SourceFetchError(Exception):
    """Raised when a licensed source fails to return usable athlete data."""


class NoLicensedSourceError(SourceFetchError):
    """Raised when no licensed source is configured yet for a given sport."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _fetch_track_field_async(athlete_source_id: str) -> Dict[str, Any]:
    try:
        athlete_id = int(athlete_source_id)
    except (TypeError, ValueError) as exc:
        raise SourceFetchError(
            f"Expected a numeric World Athletics aaAthleteId, got '{athlete_source_id}'"
        ) from exc

    async with WorldAthleticsClient() as wa:
        try:
            profile = await wa.get_athlete_profile_by_id(athlete_id)
        except WorldAthleticsError as exc:
            raise SourceFetchError(
                f"World Athletics API error for athlete id {athlete_id}: {exc}"
            ) from exc

        if profile is None:
            raise SourceFetchError(f"No World Athletics athlete found for id {athlete_id}")

        # Best-effort stats enrichment -- never fail the whole fetch over this.
        try:
            profile["personal_bests"] = await wa.get_personal_bests(athlete_id)
        except WorldAthleticsError:
            profile["personal_bests"] = []

        try:
            season_bests_raw = await wa.get_season_bests(athlete_id)
            profile["season_bests"] = season_bests_raw.get("results") or []
        except WorldAthleticsError:
            profile["season_bests"] = []

        return profile


def fetch_track_field(athlete_source_id: str) -> Dict[str, Any]:
    """
    Sync entry point. athlete_source_id is the numeric World Athletics
    aaAthleteId (captured by the admin form at add-athlete time), looked up
    exactly -- see module docstring for why this isn't a name search.
    """
    return asyncio.run(_fetch_track_field_async(athlete_source_id))


def fetch_cricket(athlete_source_id: str) -> Dict[str, Any]:
    raise NoLicensedSourceError(
        f"No licensed source configured for cricket yet (requested id: '{athlete_source_id}')"
    )


def fetch_badminton(athlete_source_id: str) -> Dict[str, Any]:
    raise NoLicensedSourceError(
        f"No licensed source configured for badminton yet (requested id: '{athlete_source_id}')"
    )


# sport -> fetcher function. Each fetcher takes the source-specific athlete
# identifier and returns a flat raw profile dict.
SOURCE_FETCHERS: Dict[str, Callable[[str], Dict[str, Any]]] = {
    "track_field": fetch_track_field,
    "cricket": fetch_cricket,
    "badminton": fetch_badminton,
}

_SOURCE_NAMES: Dict[str, str] = {
    "track_field": "World Athletics",
    "cricket": "N/A",
    "badminton": "N/A",
}


def fetch_raw_athlete_data(sport: str, athlete_source_id: str) -> Dict[str, Any]:
    """
    Entry point used by athlete_pipeline.py. Raises SourceFetchError (not a
    silent fallback) if no licensed fetcher exists yet for this sport.

    Wraps the sport-specific fetcher's flat profile dict into the shape the
    pipeline expects: {"source_name", "source_url", "fetched_at", "raw"}.
    """
    fetcher = SOURCE_FETCHERS.get(sport)
    if not fetcher:
        raise SourceFetchError(f"No licensed source fetcher registered for sport '{sport}'.")

    raw = fetcher(athlete_source_id)

    source_url = None
    if sport == "track_field":
        url_slug = raw.get("url_slug")
        athlete_id = raw.get("athlete_id")
        if url_slug:
            source_url = f"https://worldathletics.org/athletes/{url_slug}"
        elif athlete_id:
            # url_slug wasn't recovered (see module docstring) -- fall back
            # to a search-by-id link so provenance is never lost and the
            # pipeline never crashes on a missing URL. Editors reviewing
            # the draft can still click through to find the athlete.
            source_url = f"https://worldathletics.org/search?query={athlete_id}"

    if source_url is None:
        # Should be unreachable for track_field given the athlete_id
        # fallback above, but guard anyway rather than letting SourceRef
        # crash three layers downstream with an opaque pydantic error.
        raise SourceFetchError(
            f"Could not construct a source_url for sport '{sport}', "
            f"athlete_source_id '{athlete_source_id}' -- no url_slug or "
            f"athlete_id available from the fetcher."
        )

    return {
        "source_name": _SOURCE_NAMES.get(sport, sport),
        "source_url": source_url,
        "fetched_at": _now_iso(),
        "raw": raw,
    }