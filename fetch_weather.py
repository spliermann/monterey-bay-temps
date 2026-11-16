"""
Pull the latest Monterey Bay airport METARs (and, optionally, your Ecowitt
station) and save them as JSON files in this repository's data/ folder.

Run by the GitHub Action in .github/workflows/weather.yml. Standard library
only, so the Action needs no pip installs.

Outputs
  data/monterey_bay_metars.json  - one row per airport (dashboard format),
                                   with its temperature 24 hours earlier and
                                   today's high and low (since local midnight)
  data/home_station.json         - your Ecowitt station, latest reading,
                                   with its temperature 24 hours earlier and
                                   today's high and low (5-minute readings)
  data/home_history.json         - your Ecowitt station, last 7 days + today
                                   in 5-minute steps (°F, mph)
  (the two home files are written only if the ECOWITT_* secrets are set)
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


LOCAL_TZ = "America/Los_Angeles"


def local_midnight_ts():
    """Unix time of today's midnight in Pacific time."""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo(LOCAL_TZ))
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def hilo(points):
    """points: [(unix_ts, temp_c)] -> the four high/low fields, or Nones."""
    if not points:
        return {"today_high_c": None, "today_high_time": None, "today_low_c": None, "today_low_time": None}
    hi = max(points, key=lambda p: (p[1], p[0]))
    lo = min(points, key=lambda p: (p[1], -p[0]))
    return {"today_high_c": hi[1], "today_high_time": iso_utc(hi[0]),
            "today_low_c": lo[1], "today_low_time": iso_utc(lo[0])}


DAY = 24 * 3600
NEAR = 45 * 60   # a "24 hours ago" reading must be within 45 minutes of it


def fetch_airports():
    # 26 hours of reports, so each airport's report from 24 hours ago is included.
    obs = get_json(METAR_URL, {"ids": ",".join(AIRPORTS), "format": "json", "hours": 26})
    by_id = {}
    for o in obs:
        if o.get("temp") is not None and o.get("obsTime"):
            by_id.setdefault(o.get("icaoId"), []).append(o)
    rows = []
    for icao, name in AIRPORTS.items():
        reports = by_id.get(icao)
        if not reports:
            print(f"  {icao}: no temperature in recent reports, skipped")
            continue
        o = max(reports, key=lambda r: int(r["obsTime"]))
        target = int(o["obsTime"]) - DAY
        then = min(reports, key=lambda r: abs(int(r["obsTime"]) - target))
        if abs(int(then["obsTime"]) - target) > NEAR:
            then = None
        midnight = local_midnight_ts()
        today = [(int(r["obsTime"]), float(r["temp"])) for r in reports if int(r["obsTime"]) >= midnight]
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
            "temp_c_24h": then["temp"] if then else None,
            "obs_time_24h": iso_utc(then["obsTime"]) if then else None,
            **hilo(today),
        })
    if not rows:
        raise RuntimeError("aviationweather.gov returned no usable observations")
    write_json("monterey_bay_metars.json", rows)


# Ecowitt reports each value's unit; convert from whatever it sent.
_TO_MPH = {"mph": 1.0, "knots": 1.150779, "knot": 1.150779, "kt": 1.150779,
           "m/s": 2.236936, "km/h": 0.621371, "kmh": 0.621371, "fpm": 0.0113636}


def wind_to(value, unit, target):
    mph = value * _TO_MPH[str(unit).strip().lower()]
    return round(mph if target == "mph" else mph / 1.150779, 1)


def temp_to(value, unit, target):
    u = str(unit).upper()
    is_f = "F" in u or "\u2109" in u  # "F", "°F" or the single symbol "℉"
    c = (value - 32) * 5 / 9 if is_f else value
    return round(c if target == "C" else c * 9 / 5 + 32, 1)


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
        "wind_speed_unitid": 8,  # converted below from the unit the API reports
    })
    if resp.get("code") != 0:
        raise RuntimeError(f"Ecowitt API error: {resp.get('msg')}")
    d = resp.get("data", {})
    out, wind = d.get("outdoor", {}), d.get("wind", {})

    def val(block, key, kind=None, target=None):
        item = block.get(key, {})
        v = item.get("value")
        if v in (None, ""):
            return None
        v = float(v)
        if kind == "temp":
            return temp_to(v, item.get("unit", "C"), target)
        if kind == "wind":
            return wind_to(v, item.get("unit", "mph"), target)
        return v

    t = out.get("temperature", {})
    then = None
    if t.get("time"):
        try:
            then = ecowitt_temp_near(int(t["time"]) - DAY, app_key, api_key, mac)
        except Exception as e:  # the current reading still saves without it
            print(f"  24-hour-ago reading unavailable: {e}", file=sys.stderr)
    today = []
    try:
        today = ecowitt_today_points(app_key, api_key, mac)
    except Exception as e:
        print(f"  today's high/low unavailable: {e}", file=sys.stderr)
    row = {
        "icao": "HOME",
        "name": "Santa Cruz",
        "temp_c": val(out, "temperature", "temp", "C"),
        "dewp_c": val(out, "dew_point", "temp", "C"),
        "humidity_pct": val(out, "humidity"),
        "wdir": val(wind, "wind_direction"),
        "wspd_kt": val(wind, "wind_speed", "wind", "kt"),
        "obs_time": iso_utc(t["time"]) if t.get("time") else None,
        "temp_c_24h": then[1] if then else None,
        "obs_time_24h": iso_utc(then[0]) if then else None,
        **hilo(today),
        # No lat/lon on purpose: this file is public if the repo is public.
    }
    write_json("home_station.json", [row])


ECOWITT_HISTORY_URL = "https://api.ecowitt.net/api/v3/device/history"
HISTORY_DAYS = 7          # whole local days before today, plus today so far


def ecowitt_today_points(app_key, api_key, mac):
    """[(unix_ts, temp °C)] for every 5-minute reading since local midnight."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(LOCAL_TZ)
    now = datetime.now(tz)
    resp = get_json(ECOWITT_HISTORY_URL, {
        "application_key": app_key,
        "api_key": api_key,
        "mac": mac,
        "start_date": now.strftime("%Y-%m-%d 00:00:00"),
        "end_date": now.strftime("%Y-%m-%d %H:%M:%S"),
        "cycle_type": "5min",
        "call_back": "outdoor",
        "temp_unitid": 1,
    })
    if resp.get("code") != 0:
        raise RuntimeError(f"Ecowitt history error: {resp.get('msg')}")
    block = ((resp.get("data") or {}).get("outdoor") or {}).get("temperature") or {}
    unit = block.get("unit", "C")
    midnight = local_midnight_ts()
    return [(int(ts), temp_to(float(v), unit, "C")) for ts, v in (block.get("list") or {}).items()
            if v not in (None, "") and int(ts) >= midnight]


def ecowitt_temp_near(target_ts, app_key, api_key, mac):
    """(timestamp, temp °C) of the 5-minute reading closest to target_ts, or None."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(LOCAL_TZ)
    fmt = "%Y-%m-%d %H:%M:%S"
    target = datetime.fromtimestamp(target_ts, tz)
    resp = get_json(ECOWITT_HISTORY_URL, {
        "application_key": app_key,
        "api_key": api_key,
        "mac": mac,
        "start_date": (target - timedelta(minutes=40)).strftime(fmt),
        "end_date": (target + timedelta(minutes=40)).strftime(fmt),
        "cycle_type": "5min",
        "call_back": "outdoor",
        "temp_unitid": 1,
    })
    if resp.get("code") != 0:
        raise RuntimeError(f"Ecowitt history error: {resp.get('msg')}")
    block = ((resp.get("data") or {}).get("outdoor") or {}).get("temperature") or {}
    points = [(int(ts), float(v)) for ts, v in (block.get("list") or {}).items() if v not in (None, "")]
    if not points:
        return None
    ts, v = min(points, key=lambda p: abs(p[0] - target_ts))
    if abs(ts - target_ts) > NEAR:  # guards against a time-zone mix-up
        return None
    return ts, temp_to(v, block.get("unit", "C"), "C")


def fetch_ecowitt_history():
    """Save the last 7 days (+ today) of 5-minute readings to data/home_history.json.

    Units: °F and mph. One request per local day, so each stays well inside
    the API's limits for 5-minute data.
    """
    import time
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    app_key = os.environ.get("ECOWITT_APPLICATION_KEY")
    api_key = os.environ.get("ECOWITT_API_KEY")
    mac = os.environ.get("ECOWITT_MAC")
    if not (app_key and api_key and mac):
        print("Ecowitt secrets not set, skipping history")
        return

    tz = ZoneInfo(LOCAL_TZ)
    today = datetime.now(tz).date()
    fields = {
        ("outdoor", "temperature"): "temp_f",
        ("outdoor", "dew_point"): "dewp_f",
        ("outdoor", "humidity"): "humidity_pct",
        ("wind", "wind_speed"): "wind_mph",
        ("wind", "wind_gust"): "gust_mph",
        ("wind", "wind_direction"): "wdir",
    }
    by_ts = {}
    for back in range(HISTORY_DAYS, -1, -1):
        day = today - timedelta(days=back)
        params = {
            "application_key": app_key,
            "api_key": api_key,
            "mac": mac,
            "start_date": f"{day} 00:00:00",
            "end_date": f"{day} 23:59:59",
            "cycle_type": "5min",
            "call_back": "outdoor,wind",
            "temp_unitid": 2,         # 2 = degrees F
            "wind_speed_unitid": 9,   # converted below from the unit the API reports
        }
        resp = get_json(ECOWITT_HISTORY_URL, params)
        if resp.get("code") != 0:
            raise RuntimeError(f"Ecowitt history error for {day}: {resp.get('msg')}")
        data = resp.get("data") or {}
        for (group, key), col in fields.items():
            block = (data.get(group) or {}).get(key) or {}
            unit = block.get("unit", "")
            for ts, v in (block.get("list") or {}).items():
                if v in (None, ""):
                    continue
                v = float(v)
                if col in ("temp_f", "dewp_f"):
                    v = temp_to(v, unit or "F", "F")
                elif col in ("wind_mph", "gust_mph"):
                    v = wind_to(v, unit or "mph", "mph")
                by_ts.setdefault(int(ts), {})[col] = v
        time.sleep(1.5)  # be gentle with the API's rate limit

    rows = []
    for ts in sorted(by_ts):
        r = {"time": iso_utc(ts)}
        r.update(by_ts[ts])
        if "temp_f" in r:
            rows.append(r)
    if not rows:
        raise RuntimeError("Ecowitt history returned no temperature readings")
    write_json("home_history.json", rows)


def main():
    failed = False
    for step in (fetch_airports, fetch_ecowitt, fetch_ecowitt_history):
        try:
            step()
        except Exception as e:  # keep going so one source can't block the other
            print(f"{step.__name__} failed: {e}", file=sys.stderr)
            failed = True
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
