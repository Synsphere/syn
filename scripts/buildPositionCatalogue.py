"""Build an optional reviewable offline timezone-prior catalogue.

Example: uv run python scripts/buildPositionCatalogue.py Europe/London Europe/Paris --output timezone-catalogue.json
The standard runtime can calculate entries directly from installed datasets;
this helper allows deployments to inspect and pin the exact selected entries.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from zoneinfo import available_timezones


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zones", nargs="*")
    parser.add_argument("--all", action="store_true", help="All installed geographic zones; report missing ones")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.zones and not args.all:
        parser.error("Provide zones or --all")
    from geonamescache import GeonamesCache
    from timezonefinder import TimezoneFinder
    from syn.positions.bootstrap import canonical_zone, catalogue_entry
    finder, cache = TimezoneFinder(in_memory=False), GeonamesCache(min_city_population=500)
    zones = sorted({canonical_zone(z) for z in (available_timezones() if args.all else args.zones)})
    entries, missing = {}, {}
    for zone in zones:
        try:
            entries[zone] = catalogue_entry(zone, finder, cache)
        except (KeyError, ValueError, TypeError) as error:
            missing[zone] = str(error)
    # Exclusive creation avoids overwriting a previously reviewed catalogue.
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(entries, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({"written": len(entries), "missing": missing}, sort_keys=True, indent=2))
    return 1 if missing and not args.all else 0


if __name__ == "__main__":
    raise SystemExit(main())
