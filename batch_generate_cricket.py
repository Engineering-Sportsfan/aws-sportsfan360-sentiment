# """
# batch_generate_cricket.py

# Batch-runs the cricket profile generator across the Indian Cricket T20 Squad
# for the 20th Asian Games 2026 (Aichi-Nagoya, Japan), sourced from the
# Ministry of Youth Affairs & Sports sanction order (Annexure-I, 22.08.2026).

# Same pattern as batch_generate_athletes.py:
#   - drafts stay LOCAL-ONLY in llm_cricket_drafts/ (no DynamoDB review-queue
#     write here) so each one can be cross-checked by hand first
#   - once reviewed, push each live individually with push_live_athlete.py
#   - rate-limit-aware retry with exponential backoff, per-player failures
#     logged and skipped (not fatal to the batch)

# Format is generated TWICE per player -- T20 Internationals and IPL --
# saved as two separate draft files sharing the same underlying playerId
# (e.g. tilak_varma_t20i.json / tilak_varma_ipl.json). Not blended into one
# profile: T20I and IPL are different competitions with different stats,
# so a merged/averaged number wouldn't correspond to anything real.
# coreInfo (name/bio/DOB/etc.) is effectively duplicated across both drafts
# since it doesn't change by format -- push it once when reviewing;
# analytics/record_highlight from each become separate MS_Transactions
# entries (AFFIL#<sport>#<levelFormatId>#<format>#STATS) under the same
# playerId, matching the existing multi-format pattern (Test/ODI/T20
# pinning) used elsewhere in the pipeline.

# Name handling: the sanction order gives full legal names (Given + Family),
# which are often NOT the player's common cricket name (e.g. the PDF's
# "THAKUR TILAK VARMA NAMBOORI" is known in cricket as "Tilak Varma"). Per
# user decision, the script searches/generates using COMMON CRICKET NAMES,
# not the literal legal strings — mapped by hand below against the PDF.
# Legal name is preserved in the tuple as a comment/reference only.

# Each saved draft also gets a `searchTokens` field (name words + full name
# + country + sport, lowercased, derived locally -- no extra Gemini call)
# so a global search bar can match "rohit" -> Rohit Sharma, "varma" -> Tilak
# Varma, "india"/"cricket" etc. This is exact/prefix-word matching only, not
# true fuzzy/typo-tolerant search -- see _build_search_tokens().

# Usage:
#     python batch_generate_cricket.py
#     python batch_generate_cricket.py --delay 8
#     python batch_generate_cricket.py --only tilak_varma,jasprit_bumrah
#     python batch_generate_cricket.py --squad men
#     python batch_generate_cricket.py --squad women
# """

# import argparse
# import json
# import os
# import sys
# import time
# from datetime import datetime, timezone

# from pydantic import ValidationError

# # Adjust this import to match wherever the cricket generation function
# # actually lives in generate_athlete_content_llm.py (Sport.CRICKET path).
# from generate_athlete_content_llm import (
#     Sport,
#     GenerationError,
#     generate_athlete_profile,
#     _slugify,
#     OUTPUT_DIR as _DEFAULT_OUTPUT_DIR,
# )

# OUTPUT_DIR = "llm_cricket_drafts"

# # Two formats generated per player, NOT blended into one profile: T20
# # Internationals and IPL are different competitions with different stats
# # (a merged/averaged strike rate etc. wouldn't correspond to anything
# # real). coreInfo (name/bio/DOB/etc.) is effectively duplicated across
# # both drafts since it doesn't change by format -- only push it once when
# # reviewing; analytics/record_highlight from each draft become separate
# # MS_Transactions entries (AFFIL#<sport>#<levelFormatId>#<format>#STATS)
# # under the same playerId, matching the existing multi-format pattern
# # used elsewhere (Test/ODI/T20 pinning).
# FORMATS: list[tuple[str, str]] = [
#     ("T20", "t20i"),   # cricket_format value, file-suffix label
#     ("IPL", "ipl"),
# ]

# # ═══════════════════════════════════════════════════════════════════════
# # SQUAD — Indian Cricket T20 Squad, Asian Games 2026 (Aichi-Nagoya)
# # Source: Ministry of Youth Affairs and Sports sanction order, 22.08.2026,
# # Annexure-I. PDF gives legal Given+Family names; mapped here to each
# # player's common cricket name (used for generation/search), with the
# # original PDF legal name kept as a trailing comment for traceability.
# # gender is passed through explicitly per squad rather than inferred.
# # ═══════════════════════════════════════════════════════════════════════

# MEN: list[tuple[str, str]] = [
#     ("Ravi Bishnoi", "RAVI BISHNOI"),
#     ("Jasprit Bumrah", "JASPRIT JASBIRSINGH BUMRAH"),
#     ("Varun Chakaravarthy", "VARUN CHAKARAVARTHY VINOD"),
#     ("Shivam Dube", "SHIVAM RAJESH DUBE"),
#     ("Shreyas Iyer", "SHREYAS SANTOSH IYER"),
#     ("Nitish Kumar Reddy", "NITISH KUMAR REDDY KAKI"),
#     ("Ishan Kishan", "ISHAN KISHAN"),
#     ("Washington Sundar", "WASHINGTON SUNDAR MANI SUNDAR"),
#     ("Tilak Varma", "THAKUR TILAK VARMA NAMBOORI"),
#     ("Axar Patel", "AXAR RAJESHBHAI PATEL"),
#     ("Harshit Rana", "HARSHIT RANA"),
#     ("Abhishek Sharma", "ABHISHEK SHARMA"),
#     ("Arshdeep Singh", "ARSHDEEP SINGH"),
#     ("Vaibhav Suryavanshi", "VAIBHAV SOORYAVANSHI"),
#     ("Sanju Samson", "SANJU VISHWANADH SAMSON"),
# ]

# WOMEN: list[tuple[str, str]] = [
#     ("Renuka Singh Thakur", "RENUKA THAKUR"),
#     ("Arundhati Reddy", "ARUNDHATI REDDY"),
#     ("Shafali Verma", "SHAFALI VERMA"),
#     ("Harmanpreet Kaur", "HARMANPREET KAUR BHULLAR"),
#     ("Bharti Fulmali", "BHARTI SHRIKRUSHNA FULMALI"),
#     ("Kranti Goud", "KRANTI DEVI GAUD"),
#     ("Richa Ghosh", "RICHA GHOSH"),
#     ("Kamalini G", "KAMALINI GUNALAN"),
#     ("Smriti Mandhana", "SMRITI SHRINIWAS MANDHANA"),
#     ("Sree Charani", "SREE CHARANI NALLAPUREDDY"),
#     ("Shreyanka Patil", "SHREYANKA RAJESH PATIL"),
#     ("Pratika Rawal", "PRATIKA RAWAL"),
#     ("Deepti Sharma", "DEEPTI SHARMA"),
#     ("Nandani Sharma", "NANDANI SHARMA"),
#     ("Radha Yadav", "RADHA OMPRAKASH YADAV"),
# ]

# # (common_name, gender) — legal name dropped here, kept only in MEN/WOMEN above
# ATHLETES: list[tuple[str, str]] = (
#     [(name, "Male") for name, _legal in MEN]
#     + [(name, "Female") for name, _legal in WOMEN]
# )

# _RATE_LIMIT_MARKERS = ("429", "rate limit", "resource exhausted", "quota")


# def _looks_like_rate_limit(exc: Exception) -> bool:
#     msg = str(exc).lower()
#     return any(marker in msg for marker in _RATE_LIMIT_MARKERS)


# def _build_search_tokens(name: str, country: str | None, sport_id: str) -> list[str]:
#     """
#     Build a precomputed search-token array so a global search bar can match
#     on any individual name word ("varma"), the full name ("tilak varma"),
#     country ("india"), or sport ("cricket") without a separate search
#     service -- plain string matching against this array (client-side, a
#     filtered Scan, or a token GSI) covers all of those.

#     NOT true fuzzy/typo-tolerant search -- only exact-word and, if the
#     caller does prefix matching against these tokens, prefix search.
#     """
#     tokens: set[str] = set()

#     name_lower = name.strip().lower()
#     if name_lower:
#         tokens.add(name_lower)
#         for word in name_lower.split():
#             tokens.add(word)

#     if country:
#         tokens.add(country.strip().lower())

#     if sport_id:
#         tokens.add(sport_id.strip().lower())

#     return sorted(tokens)


# def run_batch(players: list[tuple[str, str]], delay_seconds: float, max_retries: int = 3) -> dict:
#     os.makedirs(OUTPUT_DIR, exist_ok=True)
#     results = {"succeeded": [], "failed": []}

#     # Flatten to (name, gender, cricket_format, suffix) so each player is
#     # generated once per format -- two passes per player, saved as two
#     # separate draft files sharing the same underlying playerId.
#     jobs = [
#         (name, gender, cricket_format, suffix)
#         for name, gender in players
#         for cricket_format, suffix in FORMATS
#     ]

#     for i, (name, gender, cricket_format, suffix) in enumerate(jobs, 1):
#         print(f"\n[{i}/{len(jobs)}] {name} ({gender}, {cricket_format})")
#         attempt = 0
#         backoff = 10.0

#         while True:
#             attempt += 1
#             try:
#                 start = time.time()
#                 profile, profile_dict = generate_athlete_profile(
#                     name, Sport.CRICKET, cricket_format=cricket_format
#                 )
#                 elapsed = time.time() - start

#                 # gender isn't a generate_athlete_profile() input -- force
#                 # it post-generation the same way flag/country overrides
#                 # are applied elsewhere in the pipeline, since we already
#                 # know it authoritatively from the squad list (men's/
#                 # women's), no need to leave it to Gemini.
#                 core_info = profile_dict.setdefault("coreInfo", {})
#                 core_info["gender"] = gender

#                 # Precomputed search tokens for the global search bar (name
#                 # words + full name + country + sport, lowercased) -- added
#                 # here rather than inside generate_athlete_profile itself
#                 # since it's derived, not Gemini-generated, and doesn't need
#                 # a model call. profile_dict is the dict that gets saved/
#                 # pushed, so this travels with the profile everywhere.
#                 profile_dict["searchTokens"] = _build_search_tokens(
#                     name=core_info.get("name", name),
#                     country=core_info.get("country"),
#                     sport_id=profile_dict.get("sportId", "cricket"),
#                 )

#                 out_path = os.path.join(OUTPUT_DIR, f"{profile.athlete_id}_{suffix}.json")
#                 with open(out_path, "w") as f:
#                     json.dump(profile_dict, f, indent=2, default=str)

#                 print(f"  OK in {elapsed:.1f}s -> {out_path}")
#                 results["succeeded"].append({
#                     "name": name, "gender": gender, "format": cricket_format,
#                     "athlete_id": profile.athlete_id, "path": out_path,
#                 })
#                 break

#             except (GenerationError, ValidationError) as e:
#                 if _looks_like_rate_limit(e) and attempt <= max_retries:
#                     print(f"  Rate-limit-like error (attempt {attempt}/{max_retries}): {e}"
#                           f" -- backing off {backoff:.0f}s")
#                     time.sleep(backoff)
#                     backoff *= 2
#                     continue
#                 print(f"  FAILED: {e}", file=sys.stderr)
#                 results["failed"].append({"name": name, "gender": gender, "format": cricket_format, "error": str(e)})
#                 break

#             except Exception as e:
#                 print(f"  FAILED (unexpected): {e}", file=sys.stderr)
#                 results["failed"].append({"name": name, "gender": gender, "format": cricket_format, "error": str(e)})
#                 break

#         if i < len(jobs):
#             time.sleep(delay_seconds)

#     return results


# def main():
#     parser = argparse.ArgumentParser(
#         description="Batch-generate Asian Games 2026 T20 cricket squad drafts (local-only, human review before push)"
#     )
#     parser.add_argument("--delay", type=float, default=5.0, help="Seconds to wait between players (default 5)")
#     parser.add_argument(
#         "--squad", choices=["men", "women", "all"], default="all",
#         help="Which squad to run (default: all 30)",
#     )
#     parser.add_argument(
#         "--only", type=str, default=None,
#         help="Comma-separated athlete_id slugs to run, e.g. tilak_varma,jasprit_bumrah",
#     )
#     args = parser.parse_args()

#     if args.squad == "men":
#         players = [(n, "Male") for n, _ in MEN]
#     elif args.squad == "women":
#         players = [(n, "Female") for n, _ in WOMEN]
#     else:
#         players = ATHLETES

#     if args.only:
#         wanted = {s.strip() for s in args.only.split(",") if s.strip()}
#         players = [(name, gender) for name, gender in players if _slugify(name) in wanted]
#         missing = wanted - {_slugify(name) for name, _ in players}
#         if missing:
#             print(f"Warning: no match for: {', '.join(sorted(missing))}", file=sys.stderr)
#         if not players:
#             print("Nothing to run.", file=sys.stderr)
#             sys.exit(1)

#     print(f"Running batch generation for {len(players)} player(s) x {len(FORMATS)} format(s) "
#           f"({', '.join(fmt for fmt, _ in FORMATS)}), delay={args.delay}s between calls")
#     start = time.time()
#     results = run_batch(players, delay_seconds=args.delay)
#     elapsed = time.time() - start

#     print(f"\n{'=' * 60}")
#     print(f"Batch complete in {elapsed:.1f}s ({elapsed / 60:.1f} min): "
#           f"{len(results['succeeded'])} succeeded, {len(results['failed'])} failed")

#     if results["failed"]:
#         print("\nFailed player/format combos (rerun individually or with --only):")
#         for f in results["failed"]:
#             print(f"  - {f['name']} ({f['gender']}, {f.get('format', '?')}): {f['error']}")

#     log_path = os.path.join(OUTPUT_DIR, f"_batch_log_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json")
#     with open(log_path, "w") as f:
#         json.dump(results, f, indent=2)
#     print(f"\nFull log written to: {log_path}")


# if __name__ == "__main__":
#     main()





"""
batch_generate_cricket.py

Batch-runs the cricket profile generator across the Indian Cricket T20 Squad
for the 20th Asian Games 2026 (Aichi-Nagoya, Japan), sourced from the
Ministry of Youth Affairs & Sports sanction order (Annexure-I, 22.08.2026).

Same pattern as batch_generate_athletes.py:
  - drafts stay LOCAL-ONLY in llm_cricket_drafts/ (no DynamoDB review-queue
    write here) so each one can be cross-checked by hand first
  - once reviewed, push each live individually with push_live_athlete.py
  - rate-limit-aware retry with exponential backoff, per-player failures
    logged and skipped (not fatal to the batch)

Format is generated TWICE per player -- T20 Internationals and IPL --
saved as two separate draft files sharing the same underlying playerId
(e.g. tilak_varma_t20i.json / tilak_varma_ipl.json). Not blended into one
profile: T20I and IPL are different competitions with different stats,
so a merged/averaged number wouldn't correspond to anything real.
coreInfo (name/bio/DOB/etc.) is effectively duplicated across both drafts
since it doesn't change by format -- push it once when reviewing;
analytics/record_highlight from each become separate MS_Transactions
entries (AFFIL#<sport>#<levelFormatId>#<format>#STATS) under the same
playerId, matching the existing multi-format pattern (Test/ODI/T20
pinning) used elsewhere in the pipeline.

Name handling: the sanction order gives full legal names (Given + Family),
which are often NOT the player's common cricket name (e.g. the PDF's
"THAKUR TILAK VARMA NAMBOORI" is known in cricket as "Tilak Varma"). Per
user decision, the script searches/generates using COMMON CRICKET NAMES,
not the literal legal strings — mapped by hand below against the PDF.
Legal name is preserved in the tuple as a comment/reference only.

Each saved draft also gets a `searchTokens` field (name words + full name
+ country + sport, lowercased, derived locally -- no extra Gemini call)
so a global search bar can match "rohit" -> Rohit Sharma, "varma" -> Tilak
Varma, "india"/"cricket" etc. This is exact/prefix-word matching only, not
true fuzzy/typo-tolerant search -- see _build_search_tokens().

Usage:
    python batch_generate_cricket.py
    python batch_generate_cricket.py --delay 8
    python batch_generate_cricket.py --only tilak_varma,jasprit_bumrah
    python batch_generate_cricket.py --squad men
    python batch_generate_cricket.py --squad women
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

from pydantic import ValidationError

# Adjust this import to match wherever the cricket generation function
# actually lives in generate_athlete_content_llm.py (Sport.CRICKET path).
from generate_athlete_content_llm import (
    Sport,
    GenerationError,
    generate_athlete_profile,
    _slugify,
    client as _gemini_client,
    GEMINI_MODEL as _GEMINI_MODEL,
)
from google.genai import types as _genai_types

OUTPUT_DIR = "llm_cricket_drafts"

# Two formats generated per player, NOT blended into one profile: T20
# Internationals and IPL are different competitions with different stats
# (a merged/averaged strike rate etc. wouldn't correspond to anything
# real). coreInfo (name/bio/DOB/etc.) is effectively duplicated across
# both drafts since it doesn't change by format -- only push it once when
# reviewing; analytics/record_highlight from each draft become separate
# MS_Transactions entries (AFFIL#<sport>#<levelFormatId>#<format>#STATS)
# under the same playerId, matching the existing multi-format pattern
# used elsewhere (Test/ODI/T20 pinning).
FORMATS: list[tuple[str, str]] = [
    ("T20", "t20i"),   # cricket_format value, file-suffix label
    ("IPL", "ipl"),
]

# ═══════════════════════════════════════════════════════════════════════
# SQUAD — Indian Cricket T20 Squad, Asian Games 2026 (Aichi-Nagoya)
# Source: Ministry of Youth Affairs and Sports sanction order, 22.08.2026,
# Annexure-I. PDF gives legal Given+Family names; mapped here to each
# player's common cricket name (used for generation/search), with the
# original PDF legal name kept as a trailing comment for traceability.
# gender is passed through explicitly per squad rather than inferred.
# ═══════════════════════════════════════════════════════════════════════

MEN: list[tuple[str, str]] = [
    ("Ravi Bishnoi", "RAVI BISHNOI"),
    ("Jasprit Bumrah", "JASPRIT JASBIRSINGH BUMRAH"),
    ("Varun Chakaravarthy", "VARUN CHAKARAVARTHY VINOD"),
    ("Shivam Dube", "SHIVAM RAJESH DUBE"),
    ("Shreyas Iyer", "SHREYAS SANTOSH IYER"),
    ("Nitish Kumar Reddy", "NITISH KUMAR REDDY KAKI"),
    ("Ishan Kishan", "ISHAN KISHAN"),
    ("Washington Sundar", "WASHINGTON SUNDAR MANI SUNDAR"),
    ("Tilak Varma", "THAKUR TILAK VARMA NAMBOORI"),
    ("Axar Patel", "AXAR RAJESHBHAI PATEL"),
    ("Harshit Rana", "HARSHIT RANA"),
    ("Abhishek Sharma", "ABHISHEK SHARMA"),
    ("Arshdeep Singh", "ARSHDEEP SINGH"),
    ("Vaibhav Suryavanshi", "VAIBHAV SOORYAVANSHI"),
    ("Sanju Samson", "SANJU VISHWANADH SAMSON"),
]

WOMEN: list[tuple[str, str]] = [
    ("Renuka Singh Thakur", "RENUKA THAKUR"),
    ("Arundhati Reddy", "ARUNDHATI REDDY"),
    ("Shafali Verma", "SHAFALI VERMA"),
    ("Harmanpreet Kaur", "HARMANPREET KAUR BHULLAR"),
    ("Bharti Fulmali", "BHARTI SHRIKRUSHNA FULMALI"),
    ("Kranti Goud", "KRANTI DEVI GAUD"),
    ("Richa Ghosh", "RICHA GHOSH"),
    ("Kamalini G", "KAMALINI GUNALAN"),
    ("Smriti Mandhana", "SMRITI SHRINIWAS MANDHANA"),
    ("Sree Charani", "SREE CHARANI NALLAPUREDDY"),
    ("Shreyanka Patil", "SHREYANKA RAJESH PATIL"),
    ("Pratika Rawal", "PRATIKA RAWAL"),
    ("Deepti Sharma", "DEEPTI SHARMA"),
    ("Nandani Sharma", "NANDANI SHARMA"),
    ("Radha Yadav", "RADHA OMPRAKASH YADAV"),
]

# (common_name, gender) — legal name dropped here, kept only in MEN/WOMEN above
ATHLETES: list[tuple[str, str]] = (
    [(name, "Male") for name, _legal in MEN]
    + [(name, "Female") for name, _legal in WOMEN]
)

def _fetch_extra_cricket_stats(name: str, cricket_format: str) -> dict:
    """
    Cricket-only enrichment call, lives entirely in this file (per user
    decision: generate_athlete_content_llm.py is athletes-only and stays
    untouched for cricket-specific additions). Fetches the extra batting/
    bowling fields the base generator doesn't ask for -- highestScore,
    hundreds, fifties, ducks, fourWicketHauls, fiveWicketHauls, maidens --
    via a second, narrow Gemini search-grounded call, reusing the same
    client/model the base generator uses.

    Returns a dict like {"battingStats": {...}, "bowlingStats": {...}}
    with only the new keys (existing keys from the base generation are
    left alone and merged over, not replaced, by the caller). Any key
    Gemini couldn't find comes back null -- never guessed or invented.
    Returns {} on any failure (bad JSON, request error) -- this is an
    enrichment step, not a required field, so a failure here should not
    fail the whole player's batch entry.
    """
    prompt = f"""
You are looking up specific career cricket statistics for {name} in {cricket_format}
cricket. Use Google Search to find current, accurate figures.

Search for and report ONLY these fields, as a single JSON object:
{{
  "battingStats": {{
    "highestScore": <plain string, e.g. "85" or "104*" (asterisk if not out), or null>,
    "hundreds": <integer career count of 100+ scores in {cricket_format}, or null>,
    "fifties": <integer career count of 50-99 scores in {cricket_format}, or null>,
    "ducks": <integer career count of scores of 0 in {cricket_format}, or null>
  }},
  "bowlingStats": {{
    "fourWicketHauls": <integer career count of exactly-4-wicket innings in {cricket_format}, or null>,
    "fiveWicketHauls": <integer career count of 5+-wicket innings in {cricket_format}, or null>,
    "maidens": <integer career maiden-over count in {cricket_format}, or null -- this is
      genuinely rare/near-zero in T20 specifically; null or 0 is expected and correct there,
      not a miss>
  }}
}}

Only use null after a genuine search attempt comes up empty -- do not guess or invent a
number. If {name} has no bowling record in {cricket_format} (a pure batter), leave all of
bowlingStats null. If no batting record (a pure bowler, rare), leave all of battingStats null.

Respond with ONLY the JSON object above. No prose, no markdown code fences, no commentary.
"""
    def _one_call() -> dict:
        response = _gemini_client.models.generate_content(
            model=_GEMINI_MODEL,
            contents=prompt,
            config=_genai_types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=2048,
                tools=[_genai_types.Tool(google_search=_genai_types.GoogleSearch())],
            ),
        )
        raw = (response.text or "").strip()
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end == 0:
            diag = ""
            try:
                candidates = getattr(response, "candidates", None) or []
                if candidates:
                    finish_reason = getattr(candidates[0], "finish_reason", None)
                    diag = f" [finish_reason={finish_reason}]"
                else:
                    diag = " [no candidates returned]"
            except Exception:
                pass
            raise ValueError(f"empty/no-JSON response{diag} (raw len={len(raw)})")
        return json.loads(raw[start:end])

    try:
        try:
            data = _one_call()
        except (ValueError, json.JSONDecodeError) as e:
            # One retry on a malformed/empty response -- same forgiving
            # pattern the main generator uses (_generate_single_pass),
            # since a single bad JSON response from a search-grounded
            # call isn't unusual and a second attempt often succeeds.
            print(f"  extra-stats: retrying after parse issue for {name}: {e}", file=sys.stderr)
            data = _one_call()

        if not isinstance(data, dict):
            return {}
        return {
            "battingStats": data.get("battingStats") if isinstance(data.get("battingStats"), dict) else {},
            "bowlingStats": data.get("bowlingStats") if isinstance(data.get("bowlingStats"), dict) else {},
        }
    except Exception as e:
        print(f"  extra-stats fetch failed for {name}: {e}", file=sys.stderr)
        return {}


def _merge_extra_cricket_stats(profile_dict: dict, extra: dict) -> None:
    """Merges _fetch_extra_cricket_stats' result into profile_dict's
    performance.battingStats/bowlingStats IN PLACE.

    Every key from the extra-stats response is written, including null
    values -- a field Gemini genuinely searched for and couldn't find
    (e.g. maidens in T20, which is expected to be null/near-zero) should
    still show up as an explicit null in the output, not be silently
    absent as if it were never asked for. Only an EXISTING non-null value
    already filled by the base generator is left alone (never overwritten
    with a null from this second call)."""
    if not extra:
        return
    performance = profile_dict.setdefault("performance", {})

    batting = performance.get("battingStats") or {}
    for k, v in (extra.get("battingStats") or {}).items():
        if v is not None or k not in batting or batting.get(k) is None:
            batting[k] = v
    if batting:
        performance["battingStats"] = batting

    bowling = performance.get("bowlingStats") or {}
    for k, v in (extra.get("bowlingStats") or {}).items():
        if v is not None or k not in bowling or bowling.get(k) is None:
            bowling[k] = v
    if bowling:
        performance["bowlingStats"] = bowling


_RATE_LIMIT_MARKERS = ("429", "rate limit", "resource exhausted", "quota")


def _looks_like_rate_limit(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _RATE_LIMIT_MARKERS)


def _build_search_tokens(name: str, country: str | None, sport_id: str) -> list[str]:
    """
    Build a precomputed search-token array so a global search bar can match
    on any individual name word ("varma"), the full name ("tilak varma"),
    country ("india"), or sport ("cricket") without a separate search
    service -- plain string matching against this array (client-side, a
    filtered Scan, or a token GSI) covers all of those.

    NOT true fuzzy/typo-tolerant search -- only exact-word and, if the
    caller does prefix matching against these tokens, prefix search.
    """
    tokens: set[str] = set()

    name_lower = name.strip().lower()
    if name_lower:
        tokens.add(name_lower)
        for word in name_lower.split():
            tokens.add(word)

    if country:
        tokens.add(country.strip().lower())

    if sport_id:
        tokens.add(sport_id.strip().lower())

    return sorted(tokens)


def run_batch(players: list[tuple[str, str]], delay_seconds: float, max_retries: int = 3) -> dict:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    results = {"succeeded": [], "failed": []}

    # Flatten to (name, gender, cricket_format, suffix) so each player is
    # generated once per format -- two passes per player, saved as two
    # separate draft files sharing the same underlying playerId.
    jobs = [
        (name, gender, cricket_format, suffix)
        for name, gender in players
        for cricket_format, suffix in FORMATS
    ]

    for i, (name, gender, cricket_format, suffix) in enumerate(jobs, 1):
        print(f"\n[{i}/{len(jobs)}] {name} ({gender}, {cricket_format})")
        attempt = 0
        backoff = 10.0

        while True:
            attempt += 1
            try:
                start = time.time()
                profile, profile_dict = generate_athlete_profile(
                    name, Sport.CRICKET, cricket_format=cricket_format
                )
                elapsed = time.time() - start

                # gender isn't a generate_athlete_profile() input -- force
                # it post-generation the same way flag/country overrides
                # are applied elsewhere in the pipeline, since we already
                # know it authoritatively from the squad list (men's/
                # women's), no need to leave it to Gemini.
                core_info = profile_dict.setdefault("coreInfo", {})
                core_info["gender"] = gender

                # Cricket-only enrichment (highestScore/hundreds/fifties/
                # ducks/fourWicketHauls/fiveWicketHauls/maidens) -- lives
                # entirely in this file, does not touch
                # generate_athlete_content_llm.py at all (see chat).
                extra_stats = _fetch_extra_cricket_stats(name, cricket_format)
                _merge_extra_cricket_stats(profile_dict, extra_stats)

                # Precomputed search tokens for the global search bar (name
                # words + full name + country + sport, lowercased) -- added
                # here rather than inside generate_athlete_profile itself
                # since it's derived, not Gemini-generated, and doesn't need
                # a model call. profile_dict is the dict that gets saved/
                # pushed, so this travels with the profile everywhere.
                profile_dict["searchTokens"] = _build_search_tokens(
                    name=core_info.get("name", name),
                    country=core_info.get("country"),
                    sport_id=profile_dict.get("sportId", "cricket"),
                )

                out_path = os.path.join(OUTPUT_DIR, f"{profile.athlete_id}_{suffix}.json")
                with open(out_path, "w") as f:
                    json.dump(profile_dict, f, indent=2, default=str)

                print(f"  OK in {elapsed:.1f}s -> {out_path}")
                results["succeeded"].append({
                    "name": name, "gender": gender, "format": cricket_format,
                    "athlete_id": profile.athlete_id, "path": out_path,
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
                results["failed"].append({"name": name, "gender": gender, "format": cricket_format, "error": str(e)})
                break

            except Exception as e:
                print(f"  FAILED (unexpected): {e}", file=sys.stderr)
                results["failed"].append({"name": name, "gender": gender, "format": cricket_format, "error": str(e)})
                break

        if i < len(jobs):
            time.sleep(delay_seconds)

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Batch-generate Asian Games 2026 T20 cricket squad drafts (local-only, human review before push)"
    )
    parser.add_argument("--delay", type=float, default=5.0, help="Seconds to wait between players (default 5)")
    parser.add_argument(
        "--squad", choices=["men", "women", "all"], default="all",
        help="Which squad to run (default: all 30)",
    )
    parser.add_argument(
        "--only", type=str, default=None,
        help="Comma-separated athlete_id slugs to run, e.g. tilak_varma,jasprit_bumrah",
    )
    args = parser.parse_args()

    if args.squad == "men":
        players = [(n, "Male") for n, _ in MEN]
    elif args.squad == "women":
        players = [(n, "Female") for n, _ in WOMEN]
    else:
        players = ATHLETES

    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        players = [(name, gender) for name, gender in players if _slugify(name) in wanted]
        missing = wanted - {_slugify(name) for name, _ in players}
        if missing:
            print(f"Warning: no match for: {', '.join(sorted(missing))}", file=sys.stderr)
        if not players:
            print("Nothing to run.", file=sys.stderr)
            sys.exit(1)

    print(f"Running batch generation for {len(players)} player(s) x {len(FORMATS)} format(s) "
          f"({', '.join(fmt for fmt, _ in FORMATS)}), delay={args.delay}s between calls")
    start = time.time()
    results = run_batch(players, delay_seconds=args.delay)
    elapsed = time.time() - start

    print(f"\n{'=' * 60}")
    print(f"Batch complete in {elapsed:.1f}s ({elapsed / 60:.1f} min): "
          f"{len(results['succeeded'])} succeeded, {len(results['failed'])} failed")

    if results["failed"]:
        print("\nFailed player/format combos (rerun individually or with --only):")
        for f in results["failed"]:
            print(f"  - {f['name']} ({f['gender']}, {f.get('format', '?')}): {f['error']}")

    log_path = os.path.join(OUTPUT_DIR, f"_batch_log_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json")
    with open(log_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nFull log written to: {log_path}")


if __name__ == "__main__":
    main()