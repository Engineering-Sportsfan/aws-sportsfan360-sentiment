"""
team_image_lookup.py

Team-equivalent of generate_athlete_content_llm.py's get_profile_image_url
tiered-fallback approach.

v3 (reverting v2's logo change): v2 excluded board/association logos
(BCCI etc.) from get_team_logo_url on the assumption that the board logo
wasn't the wanted result -- but for most national cricket teams, the
board's logo genuinely IS the team's logo (India doesn't have a separate
team crest distinct from BCCI's), so v2's exclusion was actively wrong:
it rejected the correct BCCI result and fell through to an unrelated
Commons match ("India Legends.jpg", not a logo at all) instead.
get_team_logo_url is back to trusting the Wikipedia infobox image
directly, board logo included -- that was correct all along. The board
exclusion is kept ONLY for get_team_photo_url's Commons search, since a
board logo showing up as a "team photo" would still be wrong there.

  - PHOTO (v2 fix, kept as-is, confirmed working): the original photo
    lookup had no file-type or content filtering at all, so a Commons
    search for "India cricket team squad" could -- and did -- match a
    completely unrelated scanned PDF page that merely contained "India"
    in its metadata. Results are now restricted to real photo file types
    (jpg/jpeg/png/webp -- NOT pdf/svg/tif), and files must additionally
    contain a cricket-context token (cricket/squad/team/xi) so a bare
    name match isn't enough on its own.

  1. Wikipedia infobox image -- used directly for the logo, board logo
     included; this is the correct/expected result for most teams.
  2. Wikimedia Commons search for the photo, tightened to real photo
     files + a cricket-context token requirement, board logos excluded
     (a team photo should never be a logo file).

  get_team_logo_url(team_name)   -> team logo (board logo is a valid and
                                     expected result for most teams)
  get_team_photo_url(team_name)  -> a real photo of the team (squad/action
                                     photo), never a logo or a non-photo file

Both return None (never raise) if nothing is found at any tier or on any
request error -- this is nice-to-have enrichment, not a required field,
same contract as the athlete script's image helpers.

Requires `requests` (already a dependency of generate_athlete_content_llm.py
in this environment).
"""

import re
import sys
from typing import Optional

import requests

_UA = "SportsFan360-TeamPipeline/1.0 (contact: social@sportsfan360.com)"

# Common joined/no-space or alternate forms that show up in team_id/teamName
# generation (e.g. Gemini or a user typing "Srilanka" for the CLI prompt)
# but never match how Wikipedia/Commons actually title their articles and
# files ("Sri Lanka", two words). Without this, every downstream query
# silently fails to match anything -- name_tokens={"srilanka"} is never a
# subset of title_tokens={"sri","lanka",...}, so it's not a "no results"
# case, it's a token-mismatch case that looks identical to one. Add more
# entries here as other multi-word nations show the same problem (e.g. a
# "Newzealand"/"Southafrica"/"Westindies" input).
_TEAM_NAME_ALIASES = {
    "srilanka": "Sri Lanka",
    "newzealand": "New Zealand",
    "southafrica": "South Africa",
    "westindies": "West Indies",
    "sl": "Sri Lanka",
    "nz": "New Zealand",
    "sa": "South Africa",
    "wi": "West Indies",
}


def _normalize_team_name(team_name: str) -> str:
    """
    Resolves a joined/abbreviated team_name to the spaced form Wikipedia
    and Commons actually use, via _TEAM_NAME_ALIASES. Falls back to the
    input unchanged if it's not a known alias (e.g. "India", "Australia"
    -- already correct as-is).
    """
    key = re.sub(r"[^a-z0-9]", "", team_name.strip().lower())
    return _TEAM_NAME_ALIASES.get(key, team_name)

# Tokens that mark a result as the governing BOARD's logo rather than the
# team's own crest -- exclude these regardless of which tier found them.
_BOARD_TOKENS = {
    "bcci", "board", "association", "council", "federation", "authority",
    "confederation", "committee", "acb", "ecb", "pcb", "csa", "nzc",
    "sllc", "bcb", "cwi", "zc",
}

# File extensions that are real, displayable photos/logos -- excludes PDFs,
# TIFFs, and other scanned-document formats that a loose Commons keyword
# search can otherwise match.
_PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
_LOGO_EXTENSIONS = (".svg", ".png", ".jpg", ".jpeg", ".webp")

# At least one of these must appear in a photo candidate's title, so a
# result is actually about the cricket team, not just something that
# happens to share a name token with the country.
_CRICKET_CONTEXT_TOKENS = {"cricket", "squad", "team", "xi", "players", "match", "test"}


def _has_board_token(title_tokens: set) -> bool:
    return bool(title_tokens & _BOARD_TOKENS)


# ═══════════════════════════════════════════════════════════════════════
# TIER 1 — Wikipedia infobox image
# ═══════════════════════════════════════════════════════════════════════

def _get_wikipedia_infobox_image(query: str) -> Optional[str]:
    """
    Fetches the pageimages API's curated "original" image for the article
    matching `query`, with redirects=1. A board/association logo (e.g.
    BCCI) IS an acceptable, expected result here -- most national cricket
    teams don't have a logo distinct from their board's, so no filtering
    is applied on that basis. Returns None if no matching article, no
    page image, or on any request error.
    """
    try:
        resp = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "titles": query,
                "prop": "pageimages",
                "piprop": "original",
                "redirects": 1,
                "format": "json",
            },
            headers={"User-Agent": _UA},
            timeout=10,
        )
        resp.raise_for_status()
        pages = resp.json().get("query", {}).get("pages", {})
        for page in pages.values():
            if "missing" in page:
                continue
            original = page.get("original", {})
            url = original.get("source")
            if url:
                return url
        print(f"No Wikipedia infobox image found for '{query}'", file=sys.stderr)
        return None
    except requests.exceptions.RequestException as e:
        print(f"Wikipedia pageimages fetch failed for '{query}': {e}", file=sys.stderr)
        return None


# ═══════════════════════════════════════════════════════════════════════
# TIER 2 — Wikimedia Commons search
# ═══════════════════════════════════════════════════════════════════════

def _search_commons(
    query: str,
    name_tokens: set,
    allowed_extensions: tuple,
    require_context_token: bool = False,
    exclude_board: bool = True,
) -> Optional[str]:
    """
    generator=search over the File: namespace, filtered so the matched
    file's title contains every token of the team name, ends in an
    allowed image extension, optionally contains at least one
    cricket-context token (for photo searches, so a bare country-name
    match isn't enough), and -- unless disabled -- does NOT contain a
    board/association token. Also skips filenames with "," or " and "
    (usually multi-subject group photos).
    """
    try:
        resp = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query",
                "generator": "search",
                "gsrnamespace": 6,  # File: namespace
                "gsrsearch": query,
                "gsrlimit": 15,
                "prop": "imageinfo",
                "iiprop": "url",
                "iiurlwidth": 500,
                "format": "json",
            },
            headers={"User-Agent": _UA},
            timeout=10,
        )
        resp.raise_for_status()
        pages = resp.json().get("query", {}).get("pages", {})
        for page in pages.values():
            title = (page.get("title") or "")
            title_lower = title.lower()
            title_tokens = set(re.split(r"[^a-z0-9]+", title_lower))

            if not name_tokens.issubset(title_tokens):
                continue
            if "," in title_lower or " and " in title_lower:
                continue
            if exclude_board and _has_board_token(title_tokens):
                continue
            if not title_lower.endswith(allowed_extensions):
                continue
            if require_context_token and not (title_tokens & _CRICKET_CONTEXT_TOKENS):
                continue

            imageinfo = page.get("imageinfo", [])
            if imageinfo:
                url = imageinfo[0].get("thumburl") or imageinfo[0].get("url")
                if url:
                    return url
        return None
    except requests.exceptions.RequestException as e:
        print(f"Wikimedia Commons search failed for query '{query}': {e}", file=sys.stderr)
        return None


# ═══════════════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINTS
# ═══════════════════════════════════════════════════════════════════════

def get_team_logo_url(team_name: str) -> Optional[str]:
    """
    Team logo -- the governing board's own logo (BCCI, Cricket Australia,
    etc.) is a valid, expected result here, since most national cricket
    teams don't have a crest distinct from their board's. Tries:
      1. Wikipedia infobox image on "<team> national cricket team" --
         primary source, reliably correct (confirmed against real runs).
      2. Wikimedia Commons, "<team> cricket team logo".
      3. Wikimedia Commons, "<team> cricket board logo".
    """
    team_name = _normalize_team_name(team_name)

    result = _get_wikipedia_infobox_image(f"{team_name} national cricket team")
    if result:
        return result

    name_tokens = set(team_name.lower().split())

    for query in (
        f"{team_name} cricket team logo",
        f"{team_name} cricket board logo",
    ):
        result = _search_commons(query, name_tokens, _LOGO_EXTENSIONS, require_context_token=False, exclude_board=False)
        if result:
            return result

    print(f"No team logo found for {team_name} (Wikipedia + Commons)", file=sys.stderr)
    return None


def get_team_photo_url(team_name: str) -> Optional[str]:
    """
    A real photo of the team (squad lineup, celebration, on-field action)
    -- never a logo/crest, and never a non-photo file type (the earlier
    version of this function returned a scanned PDF book page for India,
    since it had no file-type filtering at all). Tries, each restricted
    to real photo extensions (_PHOTO_EXTENSIONS) and requiring a
    cricket-context token in the filename:
      1. Wikimedia Commons, "<team> cricket team squad".
      2. Wikimedia Commons, "<team> national cricket team players".
      3. Wikimedia Commons, "<team> cricket team match".
    """
    team_name = _normalize_team_name(team_name)
    name_tokens = set(team_name.lower().split())

    for query in (
        f"{team_name} cricket team squad",
        f"{team_name} national cricket team players",
        f"{team_name} cricket team match",
    ):
        result = _search_commons(
            query, name_tokens, _PHOTO_EXTENSIONS, require_context_token=True, exclude_board=True
        )
        if result:
            return result

    print(f"No team photo found for {team_name} (Commons, filtered to real photos)", file=sys.stderr)
    return None