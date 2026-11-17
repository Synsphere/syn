"""Offline timezone prior. IANA identifiers are regions, not longitude strips.

A timezone does not necessarily contain a capital. Selection is: most populous
national capital actually in the zone, otherwise its named reference city,
otherwise the most populous city in it. The fallback is recorded, not concealed.
"""
from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
import json
import math
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import PositionConfig
from .types import EARTH_RADIUS, geographic


def local_timezone() -> str:
    explicit = os.environ.get("TZ", "").lstrip(":")
    if explicit and not explicit.startswith("/"):
        try:
            ZoneInfo(explicit)
            return explicit
        except (ZoneInfoNotFoundError, ValueError):
            pass
    try:
        from tzlocal import get_localzone_name
        return get_localzone_name()
    except (ImportError, OSError, ValueError, ZoneInfoNotFoundError):
        pass
    try:
        path = str(Path("/etc/localtime").resolve())
        if "/zoneinfo/" in path:
            return path.split("/zoneinfo/", 1)[1]
        return Path("/etc/timezone").read_text().strip()
    except OSError:
        return "Etc/UTC"


@lru_cache(maxsize=2048)
def canonical_zone(zone: str) -> str:
    # The OS tzdb supplies links even when system timezone is an older alias.
    for root in (Path("/usr/share/zoneinfo"), Path("/usr/lib/zoneinfo")):
        try:
            resolved = (root / zone).resolve().relative_to(root.resolve())
            if (root / resolved).is_file():
                zone = str(resolved)
            links = {}
            for line in (root / "tzdata.zi").read_text().splitlines():
                parts = line.split()
                if len(parts) == 3 and parts[0] in {"L", "Link"}:
                    links[parts[2]] = parts[1]
            seen = set()
            while zone in links and zone not in seen:
                seen.add(zone)
                zone = links[zone]
            return zone
        except (OSError, ValueError):
            continue
    return {"US/Eastern": "America/New_York", "US/Central": "America/Chicago",
            "US/Mountain": "America/Denver", "US/Pacific": "America/Los_Angeles",
            "Asia/Calcutta": "Asia/Kolkata", "GB": "Europe/London",
            "Europe/Kiev": "Europe/Kyiv"}.get(zone, zone)


def longitude_span(longitudes) -> float:
    """Smallest covering arc, including disconnected regions across +/-180."""
    values = sorted(set(float(x) % 360 for x in longitudes))
    if len(values) < 2 or any(not math.isfinite(x) for x in values):
        raise ValueError("Timezone boundary needs at least two finite longitudes")
    gaps = [b-a for a, b in zip(values, values[1:])] + [values[0]+360-values[-1]]
    return 360 - max(gaps)


def half_width_meters(longitudes, latitude: float) -> float:
    if not -90 <= latitude <= 90:
        raise ValueError("Invalid reference latitude")
    # WGS84 parallel length; not a geodesic across the interior of the region.
    phi = math.radians(latitude)
    n = 6_378_137 / math.sqrt(1 - 6.69437999014e-3 * math.sin(phi)**2)
    return max(1.0, math.radians(longitude_span(longitudes))*n*abs(math.cos(phi))/2)


def catalogue_entry(zone: str, tf=None, cache=None) -> dict:
    if tf is None:
        from timezonefinder import TimezoneFinder
        tf = TimezoneFinder(in_memory=False)
    if cache is None:
        from geonamescache import GeonamesCache
        cache = GeonamesCache(min_city_population=500)
    geometry = tf.get_geometry(tz_name=zone, coords_as_pairs=True)
    if not geometry:
        raise ValueError("No timezone boundary")
    cities = [c for c in cache.get_cities().values() if canonical_zone(c["timezone"]) == zone]
    if not cities:
        raise ValueError("No reference city for timezone")
    countries = cache.get_countries()
    capitals = [c for c in cities if c["name"].casefold() ==
                countries.get(c["countrycode"], {}).get("capital", "").casefold()]
    named = [c for c in cities if c["name"].casefold().replace(" ", "_") == zone.rsplit("/", 1)[-1].casefold()]
    chosen = sorted(capitals or named or cities,
                    key=lambda c: (-c.get("population", 0), c["name"], str(c.get("geonameid", ""))))[0]
    longitudes = [point[0] for polygon in geometry for point in polygon[0]]
    versions = {}
    for package in ("timezonefinder", "timezonefinder-data", "geonamescache", "tzdata"):
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            pass
    return {"timezone": zone, "city": chosen["name"], "country": chosen["countrycode"],
            "latitude": chosen["latitude"], "longitude": chosen["longitude"],
            "selection": "national-capital" if capitals else "zone-reference-city" if named else "largest-zone-city",
            "errorMeters": half_width_meters(longitudes, chosen["latitude"]),
            "longitudeSpanDegrees": longitude_span(longitudes), "dataVersions": versions}


def bootstrap(config: PositionConfig, *, now: datetime | None = None) -> object:
    now = now or datetime.now(UTC)
    supplied = config.timezone or local_timezone()
    zone = canonical_zone(supplied)
    try:
        ZoneInfo(zone)
        if zone in {"UTC", "GMT", "Etc/UTC", "Etc/GMT", "UCT"} or zone.startswith("Etc/GMT"):
            raise ValueError("A fixed UTC offset has no uniquely determined geographic boundary or capital")
        if config.timezoneCatalogue:
            entries = json.loads(Path(config.timezoneCatalogue).read_text(encoding="utf-8"))
            entry = entries[zone]
            if entry["timezone"] != zone:
                raise ValueError("Mismatched timezone catalogue entry")
        else:
            entry = catalogue_entry(zone)
        return geographic(entry["latitude"], entry["longitude"], None, entry["errorMeters"],
                          now, "timezone", uncertaintyMethod="half-timezone-east-west-width",
                          provenance={**entry, "requestedTimezone": supplied,
                                      "heuristic": True, "coverageGuaranteed": False})
    except (ImportError, LookupError, ValueError, OSError, TypeError) as error:
        # Still supply a guess, but never represent missing geographic data as precision.
        return geographic(51.50853, -0.12574, None, math.pi*EARTH_RADIUS, now, "timezone",
                          uncertaintyMethod="global-fallback-no-geographic-timezone",
                          provenance={"timezone": zone, "requestedTimezone": supplied,
                                      "city": "London", "selection": "fallback-not-inferred-location",
                                      "reason": type(error).__name__ + ": " + str(error)[:256],
                                      "heuristic": True, "coverageGuaranteed": False})
