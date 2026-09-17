"""
diagnose_profile_image.py

Standalone test: calls get_profile_image_url() and get_welcome_video_url()
directly, outside the pipeline's ThreadPoolExecutor + timeout wrapper, so
every stderr diagnostic print inside those functions (and their internal
tiers) actually shows up in your terminal instead of being silently lost.

Run this in the same venv, same folder as generate_athlete_content_llm.py:
    python diagnose_profile_image.py
"""

import sys
import time

from generate_athlete_content_llm import (
    Sport,
    get_profile_image_url,
    get_welcome_video_url,
)

ATHLETE_NAME = "Sawan Barwal"
SPORT = Sport.LONG_DISTANCE
COUNTRY = "India"

print("=" * 70)
print("1. Checking Playwright is importable...")
try:
    from playwright.sync_api import sync_playwright
    print("   OK -- playwright package is installed.")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        print("   OK -- Chromium launched and closed successfully.")
    except Exception as e:
        print(f"   FAILED to launch Chromium: {e}")
        print("   -> run: playwright install chromium")
except ImportError:
    print("   MISSING -- playwright is not installed.")
    print("   -> run: pip install playwright && playwright install chromium")

print("\n" + "=" * 70)
print(f"2. Calling get_profile_image_url({ATHLETE_NAME!r}, sport={SPORT}, country={COUNTRY!r})")
print("   (no timeout here -- will run to completion, watch for stderr lines above the result)")
start = time.time()
image_url = get_profile_image_url(ATHLETE_NAME, sport=SPORT, country=COUNTRY)
elapsed = time.time() - start
print(f"\n   RESULT after {elapsed:.1f}s: {image_url!r}")

print("\n" + "=" * 70)
print(f"3. Calling get_welcome_video_url({ATHLETE_NAME!r}, sport_label='Long Distance')")
start = time.time()
video_url = get_welcome_video_url(ATHLETE_NAME, sport_label="Long Distance")
elapsed = time.time() - start
print(f"\n   RESULT after {elapsed:.1f}s: {video_url!r}")

print("\n" + "=" * 70)
print("Summary:")
print(f"  profileImage:    {image_url}")
print(f"  welcomeVideoUrl: {video_url}")