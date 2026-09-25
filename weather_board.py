#!/usr/bin/env python3
"""Show current temperature, rainfall, and wind force on a Vestaboard Note.

Temperature and Beaufort wind force come from Open-Meteo. Rainfall for Bisley
(COPS) is the Miserden Environment Agency gauge. Rainfall for Dinas Cross
(FAGW) is the Maenclochog gauge from Natural Resources Wales rivers-and-seas.
Both are 15-minute totals in millimetres, summed for the last 24 hours and 7 days.

This uses Environment Agency rainfall data from the real-time data API (Beta).
Contains Natural Resources Wales information © Natural Resources Wales and Database Right.

The Note is 3 rows by 15 columns. 24 is the last 24 hours of rain,
7D is the last 7 days, and F is the Beaufort force:

         °C 24 7D F
    COPS 14 .0 .2 2
    FAGW 12  2 13 2

Set the two places in config.json. Preview locally with:

    python3 weather_board.py

Send to the board (Read/Write token from https://www.vestaboard.com/developers):

    VESTABOARD_TOKEN=... python3 weather_board.py --send
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROWS = 3
COLS = 15
EA_ROOT = "https://environment.data.gov.uk/flood-monitoring"
NRW_ROOT = "https://rivers-and-seas.naturalresources.wales"
USER_AGENT = "vestaboard-weather/0.1"
NAME_WIDTH = 4

# Vestaboard character codes. Gaps are colour tiles, which this layout does not use.
CHAR_CODES = {
    " ": 0,
    "A": 1,
    "B": 2,
    "C": 3,
    "D": 4,
    "E": 5,
    "F": 6,
    "G": 7,
    "H": 8,
    "I": 9,
    "J": 10,
    "K": 11,
    "L": 12,
    "M": 13,
    "N": 14,
    "O": 15,
    "P": 16,
    "Q": 17,
    "R": 18,
    "S": 19,
    "T": 20,
    "U": 21,
    "V": 22,
    "W": 23,
    "X": 24,
    "Y": 25,
    "Z": 26,
    "1": 27,
    "2": 28,
    "3": 29,
    "4": 30,
    "5": 31,
    "6": 32,
    "7": 33,
    "8": 34,
    "9": 35,
    "0": 36,
    "!": 37,
    "@": 38,
    "#": 39,
    "$": 40,
    "(": 41,
    ")": 42,
    "-": 44,
    "+": 46,
    "&": 47,
    "=": 48,
    ";": 49,
    ":": 50,
    "'": 52,
    '"': 53,
    "%": 54,
    ",": 55,
    ".": 56,
    "/": 59,
    "?": 60,
    "°": 62,
}


def get_json(url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def load_config(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    locations = data.get("locations") or []
    if len(locations) != 2:
        raise SystemExit(f"{path} must list exactly two locations")
    cleaned = []
    for location in locations:
        name = "".join(ch for ch in str(location.get("name", "")).upper() if ch.isalnum())
        if not name:
            raise SystemExit("each location needs a name")
        rainfall = location.get("rainfall") or {}
        cleaned.append(
            {
                "name": name[:NAME_WIDTH].ljust(NAME_WIDTH),
                "place": location.get("place") or name,
                "lat": float(location["lat"]),
                "lon": float(location["lon"]),
                "rainfall": {
                    "source": str(rainfall.get("source") or "ea").lower(),
                    "station": str(rainfall.get("station") or ""),
                    "label": rainfall.get("label") or "",
                },
            }
        )
    return cleaned


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def nearest_stations(lat: float, lon: float, limit: int = 8) -> list[dict]:
    found: list[dict] = []
    for dist in (10, 25, 50):
        query = urllib.parse.urlencode(
            {
                "parameter": "rainfall",
                "lat": f"{lat:.5f}",
                "long": f"{lon:.5f}",
                "dist": dist,
                "_limit": 100,
            }
        )
        payload = get_json(f"{EA_ROOT}/id/stations?{query}")
        stations = []
        for item in as_list(payload.get("items")):
            if item.get("lat") is None or item.get("long") is None:
                continue
            item = dict(item)
            item["distance_km"] = haversine_km(lat, lon, float(item["lat"]), float(item["long"]))
            stations.append(item)
        stations.sort(key=lambda item: item["distance_km"])
        found = stations
        if stations:
            break
    return found[:limit]


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def sum_readings(readings: dict[str, float], now: datetime) -> dict:
    cutoff = now - timedelta(hours=24)
    last_24 = 0.0
    last_7 = 0.0
    count_24 = 0
    latest: datetime | None = None
    for stamp, value in readings.items():
        when = parse_time(stamp)
        last_7 += value
        if latest is None or when > latest:
            latest = when
        if when >= cutoff:
            last_24 += value
            count_24 += 1
    return {
        "mm_24h": last_24,
        "mm_7d": last_7,
        "readings": len(readings),
        "readings_24h": count_24,
        "latest": latest,
    }


def rainfall_totals(station_reference: str, now: datetime) -> dict:
    since = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    readings: dict[str, float] = {}
    offset = 0
    page_size = 2000
    while True:
        query = urllib.parse.urlencode(
            {
                "parameter": "rainfall",
                "since": since,
                "_limit": page_size,
                "_offset": offset,
            }
        )
        payload = get_json(
            f"{EA_ROOT}/id/stations/{urllib.parse.quote(station_reference)}/readings?{query}"
        )
        items = as_list(payload.get("items"))
        for item in items:
            if "value" not in item or not item.get("dateTime"):
                continue
            readings[item["dateTime"]] = float(item["value"])
        if len(items) < page_size:
            break
        offset += page_size

    return sum_readings(readings, now)


def ea_station(station_reference: str, label: str) -> dict:
    payload = get_json(f"{EA_ROOT}/id/stations/{urllib.parse.quote(station_reference)}")
    item = payload.get("items") or {}
    return {
        "label": label or station_reference,
        "reference": station_reference,
        "grid": item.get("gridReference") or "",
        "source": "Environment Agency",
    }


def nrw_rainfall(station_name: str, label: str, now: datetime) -> tuple[dict, dict]:
    stations = as_list(get_json(f"{NRW_ROOT}/map/GetStations"))
    wanted = station_name.casefold()
    match = next(
        (
            station
            for station in stations
            if str((station.get("name") or {}).get("english", "")).casefold() == wanted
        ),
        None,
    )
    if match is None:
        raise SystemExit(f"Natural Resources Wales has no rainfall station named {station_name}")
    parameter = next(
        (
            item
            for item in match.get("parameters") or []
            if item.get("typeId") == 2 or (item.get("typeText") or {}).get("english") == "Rainfall"
        ),
        None,
    )
    if parameter is None:
        raise SystemExit(f"{station_name} has no rainfall parameter")
    start = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    query = urllib.parse.urlencode({"parameterId": parameter["id"], "from": start, "to": end})
    payload = get_json(f"{NRW_ROOT}/graph/getdata?{query}")
    readings = {}
    for point in payload.get("data") or []:
        if point.get("y") is None or not point.get("x"):
            continue
        readings[point["x"]] = float(point["y"])
    station = {
        "label": label or (match.get("name") or {}).get("english") or station_name,
        "reference": str(match.get("id")),
        "grid": match.get("nationalGridReference") or "",
        "source": "Natural Resources Wales",
    }
    return station, sum_readings(readings, now)


def beaufort(wind_ms: float | None) -> int | None:
    """World Meteorological Organization Beaufort force from a 10 m wind speed."""
    if wind_ms is None:
        return None
    limits = (0.3, 1.6, 3.4, 5.5, 8.0, 10.8, 13.9, 17.2, 20.8, 24.5, 28.5, 32.7)
    for force, limit in enumerate(limits):
        if wind_ms < limit:
            return force
    return 12


def current_conditions(lat: float, lon: float) -> tuple[float | None, float | None]:
    query = urllib.parse.urlencode(
        {
            "latitude": f"{lat:.5f}",
            "longitude": f"{lon:.5f}",
            "current": "temperature_2m,wind_speed_10m",
            "wind_speed_unit": "ms",
        }
    )
    payload = get_json(f"https://api.open-meteo.com/v1/forecast?{query}")
    current = payload.get("current") or {}
    temp = float(current["temperature_2m"]) if "temperature_2m" in current else None
    wind = float(current["wind_speed_10m"]) if "wind_speed_10m" in current else None
    return temp, wind


def format_mm(value: float | None, width: int) -> str:
    if value is None:
        return "?".rjust(width)
    if width == 1:
        return f"{value:.0f}"
    if width == 2 and value < 0.95:
        text = f".{min(9, int(round(value * 10)))}"
    elif width >= 4 and value < 99.95:
        text = f"{value:.1f}"
    elif value < 9.95 and width >= 3:
        text = f"{value:.1f}"
    else:
        text = f"{value:.0f}"
    if len(text) > width:
        text = f"{value:.0f}"
    return text[-width:].rjust(width)


def format_temp(value: float | None) -> str:
    if value is None:
        return "??"
    return f"{int(round(value)):>2}"[-2:]


def format_force(force: int | None) -> str:
    if force is None:
        return "?"
    return str(max(0, min(12, force)))


def location_line(
    name: str,
    temp: float | None,
    mm_24h: float | None,
    mm_7d: float | None,
    force: int | None,
) -> str:
    # Columns: name(4), gap, temp(2), gap, 24h rain(2), gap, 7d rain(2), gap, force.
    # Force 10+ uses the gap before it.
    chars = [" "] * COLS
    chars[0:4] = list(f"{name[:NAME_WIDTH]:<{NAME_WIDTH}}")
    chars[5:7] = list(format_temp(temp))
    chars[8:10] = list(format_mm(mm_24h, 2))
    chars[11:13] = list(format_mm(mm_7d, 2))
    force_text = format_force(force)
    if len(force_text) == 1:
        chars[14] = force_text
    else:
        chars[13:15] = list(force_text[-2:])
    line = "".join(chars)
    if len(line) != COLS:
        raise RuntimeError(f"line is {len(line)} chars, expected {COLS}: {line!r}")
    return line


def header_line() -> str:
    chars = [" "] * COLS
    chars[5:7] = list("°C")
    chars[8:10] = list("24")
    chars[11:13] = list("7D")
    chars[14] = "F"
    return "".join(chars)


def board_lines(rows: list[dict]) -> list[str]:
    lines = [header_line()]
    for row in rows:
        lines.append(
            location_line(row["name"], row["temp_c"], row["mm_24h"], row["mm_7d"], row["wind_force"])
        )
    if len(lines) != ROWS:
        raise RuntimeError(f"expected {ROWS} lines, got {len(lines)}")
    return lines


def encode(lines: list[str]) -> list[list[int]]:
    grid = []
    for line in lines:
        row = []
        for char in line:
            row.append(CHAR_CODES.get(char, CHAR_CODES.get(char.upper(), 0)))
        if len(row) != COLS:
            raise RuntimeError(f"encoded row has {len(row)} columns")
        grid.append(row)
    return grid


def collect(locations: list[dict]) -> list[dict]:
    now = datetime.now(timezone.utc)
    rows = []
    for location in locations:
        temp, wind_ms = current_conditions(location["lat"], location["lon"])
        rain = None
        station = None
        spec = location["rainfall"]
        if spec["source"] == "nrw":
            station, rain = nrw_rainfall(spec["station"], spec["label"], now)
        elif spec["station"]:
            station = ea_station(spec["station"], spec["label"])
            rain = rainfall_totals(spec["station"], now)
        else:
            for candidate in nearest_stations(location["lat"], location["lon"]):
                totals = rainfall_totals(candidate["stationReference"], now)
                if totals["readings"] == 0:
                    continue
                age = now - totals["latest"] if totals["latest"] is not None else None
                totals["age"] = age
                fresh = age is not None and age <= timedelta(hours=36)
                if station is None or fresh:
                    station = {
                        "label": candidate["stationReference"],
                        "reference": candidate["stationReference"],
                        "grid": candidate.get("gridReference") or "",
                        "source": "Environment Agency",
                    }
                    rain = totals
                if fresh:
                    break
        if rain is None:
            rain = {
                "mm_24h": None,
                "mm_7d": None,
                "readings": 0,
                "readings_24h": 0,
                "latest": None,
                "age": None,
            }
        rows.append(
            {
                "name": location["name"],
                "lat": location["lat"],
                "lon": location["lon"],
                "temp_c": temp,
                "wind_ms": wind_ms,
                "wind_force": beaufort(wind_ms),
                "mm_24h": rain["mm_24h"],
                "mm_7d": rain["mm_7d"],
                "station": station,
                "rain": rain,
            }
        )
    return rows


def send(grid: list[list[int]], token: str) -> dict:
    body = json.dumps({"characters": grid}).encode()
    request = urllib.request.Request(
        "https://cloud.vestaboard.com/",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Vestaboard-Token": token,
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Vestaboard rejected the message ({error.code}): {detail}") from error


def print_report(rows: list[dict], lines: list[str]) -> None:
    print("+" + "-" * COLS + "+")
    for line in lines:
        print("|" + line + "|")
    print("+" + "-" * COLS + "+")
    print()
    for row in rows:
        station = row["station"]
        rain = row["rain"]
        if station is None:
            print(f"{row['name']}: no rainfall station found")
            continue
        latest = rain["latest"].isoformat() if rain["latest"] else "none"
        if row["temp_c"] is None:
            print(f"{row['name'].strip()}: temperature unavailable")
        else:
            wind = "wind n/a" if row["wind_force"] is None else f"force {row['wind_force']}"
            print(f"{row['name'].strip()}: {row['temp_c']:.1f}°C, {wind}")
        print(
            f"  {station['source']} {station['label']} "
            f"({station['reference']}, {station['grid'] or 'no grid'})"
        )
        mm_24 = "n/a" if rain["mm_24h"] is None else f"{rain['mm_24h']:.1f} mm"
        mm_7 = "n/a" if rain["mm_7d"] is None else f"{rain['mm_7d']:.1f} mm"
        print(
            f"  24h {mm_24} from {rain['readings_24h']} readings, "
            f"7d {mm_7} from {rain['readings']} readings, latest {latest}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Update a Vestaboard Note with temperature and rainfall")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--send", action="store_true", help="post the layout to the Vestaboard cloud API")
    args = parser.parse_args()

    locations = load_config(args.config)
    rows = collect(locations)
    lines = board_lines(rows)
    grid = encode(lines)
    print_report(rows, lines)

    if not args.send:
        print("\nPreview only. Re-run with --send and VESTABOARD_TOKEN to update the Note.")
        return

    token = os.environ.get("VESTABOARD_TOKEN", "").strip()
    if not token:
        raise SystemExit("Set VESTABOARD_TOKEN to the Read/Write key from the Vestaboard developer console.")
    result = send(grid, token)
    print(f"\nSent to Vestaboard: {result.get('status', result)}")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.URLError as error:
        print(f"Network error: {error}", file=sys.stderr)
        sys.exit(1)
