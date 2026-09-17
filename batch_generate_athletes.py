"""
batch_generate_athletes.py

Batch-runs generate_athlete_content_llm.py's generate_athlete_profile()
across a fixed list of athletes (from Missing_athletes.pdf), instead of
running the script's interactive main() loop one athlete at a time.

Each athlete still goes through the SAME generation pipeline as a single
interactive run -- same schema validation, same retry-fill passes, same
local JSON save to llm_athlete_drafts/. UNLIKE the interactive script,
this does NOT write to the DynamoDB review queue -- drafts stay local-only
in llm_athlete_drafts/ so you can cross-check each one by hand first, same
workflow as before batching existed. Once reviewed, push each one live
individually with push_live_athlete.py.

Rate-limit handling:
  - a fixed delay between athletes (--delay, default 5s)
  - automatic retry with exponential backoff if a call fails with what
    looks like a 429/rate-limit error (checks the exception message for
    "429" / "rate limit" / "resource exhausted", not a hard API
    guarantee since the underlying error type isn't inspected here)
  - per-athlete failures are logged and skipped, not fatal to the batch

Usage:
    python batch_generate_athletes.py
    python batch_generate_athletes.py --delay 8
    python batch_generate_athletes.py --only mohammed_ashfaq,sawan_barwal
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone

from pydantic import ValidationError

from generate_athlete_content_llm import (
    Sport,
    GenerationError,
    generate_athlete_profile,
    _slugify,
    OUTPUT_DIR,
)
import os

# ═══════════════════════════════════════════════════════════════════════
# ATHLETE LIST — from Missing_athletes.pdf, mapped to discipline.
# Decathlon/Heptathlon (no combined-events category in the schema) are
# bucketed under JUMPS per team decision -- revisit those two manually if
# the draft doesn't read right.
# ═══════════════════════════════════════════════════════════════════════

ATHLETES: list[tuple[str, Sport]] = [
    # Sprints
    ("Unnathi Aiyappa Bolland", Sport.SPRINTS),
    ("Nandhini Kongan", Sport.SPRINTS),
    ("Anu Raghavan", Sport.SPRINTS),
    ("Vithya Ramraj", Sport.SPRINTS),
    ("Jyothi Yarraji", Sport.SPRINTS),
    ("Prachi", Sport.SPRINTS),
    # Middle Distance
    ("Krishan Kumar", Sport.MIDDLE_DISTANCE),
    ("Mohammed Afsal Pulikkalakath", Sport.MIDDLE_DISTANCE),
    ("Avinash Mukund Sable", Sport.MIDDLE_DISTANCE),
    ("Yoonus Shah", Sport.MIDDLE_DISTANCE),
    ("Gowthami Jayaraman", Sport.MIDDLE_DISTANCE),
    ("Ankita", Sport.MIDDLE_DISTANCE),
    # Long Distance
    ("Sawan Barwal", Sport.LONG_DISTANCE),
    ("Kartik Jayraj Karkera", Sport.LONG_DISTANCE),
    ("Abhishek Pal", Sport.LONG_DISTANCE),
    ("Manju Rani", Sport.LONG_DISTANCE),
    # Throws
    ("Annu Rani", Sport.THROWS),
    ("Tanya Chaudhary", Sport.THROWS),
    ("Krishna Jayasankar Menon", Sport.THROWS),
    ("Anushka Yadav", Sport.THROWS),
    # Jumps
    ("Shahnavaz Khan", Sport.JUMPS),
    ("Karthik Unnikrishnan", Sport.JUMPS),
    ("Supriya Sindigere Basavaraju", Sport.JUMPS),
    ("Baranica Elangovan", Sport.JUMPS),
    ("Shaili Singh", Sport.JUMPS),
    ("Ancy Sojan Edappilly", Sport.JUMPS),
    ("Niharika Vashisht", Sport.JUMPS),
    # Combined events -> Jumps (no dedicated category; revisit manually)
    ("Thowfeeq Noushad", Sport.JUMPS),
    ("Annamika Kozhinjapara Mbil Aneesh", Sport.JUMPS),
    # Relays
    ("Mohammed Ashfaq", Sport.RELAYS),
    ("Dharmveer Choudhary", Sport.RELAYS),
    ("Jay Kumar", Sport.RELAYS),
    ("Manu Thekkinalil Saji", Sport.RELAYS),
    ("Pranav Pramod Gurav", Sport.RELAYS),
    ("Tamanna", Sport.RELAYS),
    ("Sneha Sathyanarayana", Sport.RELAYS),
    ("Sudeshna Hanmant Shivankar", Sport.RELAYS),
    ("Nithya Gandhe", Sport.RELAYS),
    ("Poovamma Raju Machettira", Sport.RELAYS),
    ("Kumari Saloni Nagar", Sport.RELAYS),
    ("Srabani Nanda", Sport.RELAYS),
    ("Abinaya Rajarajan", Sport.RELAYS),
]

_RATE_LIMIT_MARKERS = ("429", "rate limit", "resource exhausted", "quota")


def _looks_like_rate_limit(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _RATE_LIMIT_MARKERS)


def run_batch(athletes: list[tuple[str, Sport]], delay_seconds: float, max_retries: int = 3) -> dict:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    results = {"succeeded": [], "failed": []}

    for i, (name, sport) in enumerate(athletes, 1):
        print(f"\n[{i}/{len(athletes)}] {name} ({sport.value})")
        attempt = 0
        backoff = 10.0

        while True:
            attempt += 1
            try:
                start = time.time()
                profile, profile_dict = generate_athlete_profile(name, sport)
                elapsed = time.time() - start

                out_path = os.path.join(OUTPUT_DIR, f"{profile.athlete_id}.json")
                with open(out_path, "w") as f:
                    json.dump(profile_dict, f, indent=2, default=str)

                print(f"  OK in {elapsed:.1f}s -> {out_path}")
                results["succeeded"].append({
                    "name": name, "sport": sport.value, "athlete_id": profile.athlete_id,
                    "path": out_path,
                })
                break

            except (GenerationError, ValidationError) as e:
                if _looks_like_rate_limit(e) and attempt <= max_retries:
                    print(f"  Rate-limit-like error (attempt {attempt}/{max_retries}): {e}"
                          f" -- backing off {backoff:.0f}s")
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                print(f"  FAILED: {e}", file=sys.stderr)
                results["failed"].append({"name": name, "sport": sport.value, "error": str(e)})
                break

            except Exception as e:
                print(f"  FAILED (unexpected): {e}", file=sys.stderr)
                results["failed"].append({"name": name, "sport": sport.value, "error": str(e)})
                break

        if i < len(athletes):
            time.sleep(delay_seconds)

    return results


def main():
    parser = argparse.ArgumentParser(description="Batch-generate athlete drafts into the review queue")
    parser.add_argument("--delay", type=float, default=5.0, help="Seconds to wait between athletes (default 5)")
    parser.add_argument(
        "--only", type=str, default=None,
        help="Comma-separated athlete_id slugs to run (subset of the full 42), e.g. mohammed_ashfaq,sawan_barwal",
    )
    args = parser.parse_args()

    athletes = ATHLETES
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        athletes = [(name, sport) for name, sport in ATHLETES if _slugify(name) in wanted]
        missing = wanted - {_slugify(name) for name, _ in athletes}
        if missing:
            print(f"Warning: no match in ATHLETES for: {', '.join(sorted(missing))}", file=sys.stderr)
        if not athletes:
            print("Nothing to run.", file=sys.stderr)
            sys.exit(1)

    print(f"Running batch generation for {len(athletes)} athlete(s), delay={args.delay}s between calls")
    start = time.time()
    results = run_batch(athletes, delay_seconds=args.delay)
    elapsed = time.time() - start

    print(f"\n{'=' * 60}")
    print(f"Batch complete in {elapsed / 60:.1f} min: "
          f"{len(results['succeeded'])} succeeded, {len(results['failed'])} failed")

    if results["failed"]:
        print("\nFailed athletes (rerun these individually or with --only):")
        for f in results["failed"]:
            print(f"  - {f['name']} ({f['sport']}): {f['error']}")

    log_path = os.path.join(OUTPUT_DIR, f"_batch_log_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json")
    with open(log_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nFull log written to: {log_path}")


if __name__ == "__main__":
    main()