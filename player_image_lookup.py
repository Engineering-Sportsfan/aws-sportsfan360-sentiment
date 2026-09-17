"""
player_image_lookup.py

Fetches a real profileImage URL for a cricket player — same reasoning as
team_image_lookup.py / the athlete script's profileImage: never let the LLM
invent or guess at an image URL, always resolve one from a real source.

Tier order (simplest first — extend with SeekLogo-style scraping later if
Wikipedia/Commons coverage turns out too thin, same as team_image_lookup.py's
history):
  1. Wikipedia page main infobox image
  2. Wikimedia Commons search fallback

Requires a real User-Agent header — Wikimedia blocks default/generic UAs
(same fix already applied in team_image_lookup.py).
"""

import re
from typing import Optional

import requests

_HEADERS = {"User-Agent": "SportsFan360PlayerImageLookup/1.0 (contact: dev@sportsfan360.internal)"}


def _wikipedia_infobox_image(name: str) -> Optional[str]:
    try:
        resp = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "format": "json",
                "prop": "pageimages",
                "piprop": "original",
                "titles": name,
                "redirects": 1,
            },
            headers=_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        pages = resp.json().get("query", {}).get("pages", {})
        for page in pages.values():
            original = page.get("original", {}).get("source")
            if original:
                return original
    except requests.RequestException:
        pass
    return None


def _commons_search_image(query: str) -> Optional[str]:
    try:
        resp = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query",
                "format": "json",
                "generator": "search",
                "gsrsearch": f"{query} cricketer",
                "gsrnamespace": 6,  # File namespace
                "gsrlimit": 5,
                "prop": "imageinfo",
                "iiprop": "url",
            },
            headers=_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        pages = resp.json().get("query", {}).get("pages", {})
        for page in pages.values():
            title = page.get("title", "")
            # Skip obvious non-photo files (logos, scans, documents)
            if re.search(r"\.(pdf|svg)$", title, re.IGNORECASE):
                continue
            info = page.get("imageinfo", [])
            if info and info[0].get("url"):
                return info[0]["url"]
    except requests.RequestException:
        pass
    return None


def get_player_photo_url(name: str) -> Optional[str]:
    """Best-effort real photo URL for a player. Returns None if nothing found
    (never fabricates a plausible-looking link)."""
    url = _wikipedia_infobox_image(name)
    if url:
        return url
    url = _commons_search_image(name)
    if url:
        return url
    return None