"""Download building footprints and heights from OpenStreetMap into backend/data.

Run once per demo area, from backend/, on a machine with internet access:

    uv run python tools/fetch_buildings.py hongkong 22.24 114.11 22.34 114.25

(south, west, north, east in degrees). Writes data/buildings_<name>.geojson: one polygon per
building with ``height_m`` and where it comes from (``source``): the ``height`` tag, else
``building:levels`` x 3 m, else ``DEFAULT_HEIGHT_M``. Uses the public Overpass API, standard
library only. Name the file in the scenario: "terrain": {"buildings": "buildings_<name>.geojson"}.
"""

from __future__ import annotations

import json
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter",
]
LEVEL_M = 3.0
DEFAULT_HEIGHT_M = 10.0
"""Buildings with neither tag: about three floors."""
OUT_DIR = Path(__file__).resolve().parent.parent / "data"


def tls_context() -> ssl.SSLContext:
    """Certificates from certifi (installed with the backend) when available: some Python
    builds check against an outdated list of their own ("certificate has expired")."""
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def query(south: float, west: float, north: float, east: float) -> str:
    box = f"{south},{west},{north},{east}"
    return f'[out:json][timeout:300];way["building"]({box});out tags geom;'


def fetch(overpass_query: str) -> dict:
    body = urllib.parse.urlencode({"data": overpass_query}).encode()
    last_error: Exception | None = None
    for endpoint in ENDPOINTS:
        for attempt in range(2):
            try:
                print(f"asking {endpoint} ...", flush=True)
                request = urllib.request.Request(
                    endpoint, data=body, headers={"User-Agent": "corridor-edth/1.0"}
                )
                with urllib.request.urlopen(
                    request, timeout=360, context=tls_context()
                ) as response:
                    return json.loads(response.read())
            except Exception as exc:  # network or server busy: try again, then elsewhere
                last_error = exc
                print(f"  failed ({exc}); retrying", flush=True)
                time.sleep(5 * (attempt + 1))
    raise SystemExit(f"every Overpass server failed: {last_error}")


def metres(value: str | None) -> float | None:
    """'25', '25 m', '25.5m' -> metres; feet and other units are skipped."""
    if not value:
        return None
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*m?", value.strip())
    return float(match.group(1)) if match else None


def height(tags: dict[str, str]) -> tuple[float, str]:
    tagged = metres(tags.get("height"))
    if tagged and tagged > 0:
        return tagged, "height"
    levels = metres(tags.get("building:levels"))
    if levels and levels > 0:
        return levels * LEVEL_M, "levels"
    return DEFAULT_HEIGHT_M, "default"


def features(data: dict) -> list[dict]:
    out = []
    for element in data.get("elements", []):
        geometry = element.get("geometry")
        if element.get("type") != "way" or not geometry or len(geometry) < 4:
            continue
        ring = [[round(p["lon"], 6), round(p["lat"], 6)] for p in geometry]
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        h, source = height(element.get("tags", {}))
        out.append(
            {
                "type": "Feature",
                "properties": {"height_m": round(h, 1), "source": source},
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
        )
    return out


def main() -> None:
    if len(sys.argv) != 6:
        raise SystemExit(__doc__)
    name = sys.argv[1]
    south, west, north, east = (float(v) for v in sys.argv[2:])
    buildings = features(fetch(query(south, west, north, east)))
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / f"buildings_{name}.geojson"
    collection = {"type": "FeatureCollection", "features": buildings}
    path.write_text(json.dumps(collection, separators=(",", ":")), encoding="utf-8")
    sources = {
        s: sum(f["properties"]["source"] == s for f in buildings)
        for s in ("height", "levels", "default")
    }
    print(f"wrote {len(buildings)} buildings to {path} ({path.stat().st_size / 1e6:.1f} MB)")
    print(f"height from: {sources}")


if __name__ == "__main__":
    main()
