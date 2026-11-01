"""
Pull the latest Monterey Bay airport METARs (and, optionally, your Ecowitt
station) and save them as JSON files in this repository's data/ folder.

Run by the GitHub Action in .github/workflows/weather.yml. Standard library
only, so the Action needs no pip installs.

Outputs
  data/monterey_bay_metars.json  - one row per airport (dashboard format)
  data/home_station.json         - your Ecowitt station (only if the
                                   ECOWITT_* secrets are set)
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"

AIRPORTS = {
    "KMRY": "Monterey Regional",
    "KSNS": "Salinas Municipal",
    "KWVI": "Watsonville Municipal",
}

METAR_URL = "https://aviationweather.gov/api/data/metar"
ECOWITT_URL = "https://api.ecowitt.net/api/v3/device/real_time"
HEADERS = {"User-Agent": "monterey-bay-weather-dashboard (GitHub Action)"}


def get_json(url, params):
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def iso_utc(epoch_seconds):
    return datetime.fromtimestamp(int(epoch_seconds), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json(name, rows):
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / name
    path.write_text(json.dumps(rows, indent=1) + "\n", encoding="utf-8")
    print(f"Wrote {path.name}: {len(rows)} row(s)")


def fetch_airports():
    obs = get_json(METAR_URL, {"ids": ",".join(AIRPORTS), "format": "json"})
    by_id = {o.get("icaoId"): o for o in obs}
    rows = []
    for icao, name in AIRPORTS.items():
        o = by_id.get(icao)
        if not o or o.get("temp") is None:
            print(f"  {icao}: no temperature in latest report, skipped")
            continue
        rows.append({
            "icao": icao,
            "name": name,
            "temp_c": o["temp"],
            "dewp_c": o.get("dewp"),
            "wdir": o.get("wdir"),
            "wspd_kt": o.get("wspd"),
            "obs_time": iso_utc(o["obsTime"]),
            "raw": o.get("rawOb"),
            "lat": o.get("lat"),
            "lon": o.get("lon"),
        })
    if not rows:
        raise RuntimeError("aviationweather.gov returned no usable observations")
    write_json("monterey_bay_metars.json", rows)


def fetch_ecowitt():
    app_key = os.environ.get("ECOWITT_APPLICATION_KEY")
    api_key = os.environ.get("ECOWITT_API_KEY")
    mac = os.environ.get("ECOWITT_MAC")
    if not (app_key and api_key and mac):
        print("Ecowitt secrets not set, skipping home station")
        return
    resp = get_json(ECOWITT_URL, {
        "application_key": app_key,
        "api_key": api_key,
        "mac": mac,
        "call_back": "outdoor,wind",
        "temp_unitid": 1,        # 1 = degrees C
        "wind_speed_unitid": 9,  # 9 = knots
    })
    if resp.get("code") != 0:
        raise RuntimeError(f"Ecowitt API error: {resp.get('msg')}")
    d = resp.get("data", {})
    out, wind = d.get("outdoor", {}), d.get("wind", {})

    def val(block, key):
        v = block.get(key, {}).get("value")
        return float(v) if v not in (None, "") else None

    t = out.get("temperature", {})
    row = {
        "icao": "HOME",
        "name": "My weather station",
        "temp_c": val(out, "temperature"),
        "dewp_c": val(out, "dew_point"),
        "humidity_pct": val(out, "humidity"),
        "wdir": val(wind, "wind_direction"),
        "wspd_kt": val(wind, "wind_speed"),
        "obs_time": iso_utc(t["time"]) if t.get("time") else None,
        # No lat/lon on purpose: this file is public if the repo is public.
    }
    write_json("home_station.json", [row])


def main():
    failed = False
    for step in (fetch_airports, fetch_ecowitt):
        try:
            step()
        except Exception as e:  # keep going so one source can't block the other
            print(f"{step.__name__} failed: {e}", file=sys.stderr)
            failed = True
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
