"""Small cached Leuven forecast. Weather failures never block reservations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import threading
import time
from zoneinfo import ZoneInfo

import httpx

TZ = ZoneInfo("Europe/Brussels")
CREDIT = "Leuven · % kans op neerslag · Open-Meteo"
RAIN_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 97, 99}


def icon(code: int | None) -> str:
    if code in RAIN_CODES:
        return "🌧️"
    if code in (0, 1):
        return "☀️"
    if code == 2:
        return "🌤️"
    if code in (3, 45, 48):
        return "☁️"
    return "❔"


def _badge(values) -> str:
    if not values:
        return ""
    codes = [code for code, _ in values if code is not None]
    chances = [chance for _, chance in values if chance is not None]
    code = next((c for c in codes if c in RAIN_CODES), max(codes, default=None))
    return f"{icon(code)} {max(chances)}%" if chances else f"{icon(code)} ?%"


@dataclass(frozen=True)
class Forecast:
    hourly: dict[str, tuple[int | None, int | None]]
    daily: dict[str, tuple[int | None, int | None]]

    def badge(self, start: datetime, end: datetime | None = None) -> str:
        start = start.astimezone(TZ)
        end = (end or start + timedelta(minutes=90)).astimezone(TZ)
        # Open-Meteo's probability describes the preceding hour. Include all
        # hourly periods touched by the game, and show their highest probability.
        cursor = start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        values = []
        while cursor - timedelta(hours=1) < end:
            value = self.hourly.get(cursor.strftime("%Y-%m-%dT%H:%M"))
            if value is not None:
                values.append(value)
            cursor += timedelta(hours=1)
        return _badge(values)

    def day_badge(self, day: date) -> str:
        value = self.daily.get(day.isoformat())
        return _badge([value]) if value is not None else ""


def parse_forecast(data: dict) -> Forecast:
    def series(kind, chance_key):
        section = data.get(kind) or {}
        codes = section.get("weather_code") or []
        chances = section.get(chance_key) or []
        out = {}
        for i, stamp in enumerate(section.get("time") or []):
            code = codes[i] if i < len(codes) else None
            chance = chances[i] if i < len(chances) else None
            if code is not None:
                code = int(code)
            if chance is not None:
                chance = max(0, min(100, round(chance)))
            out[str(stamp)] = (code, chance)
        return out
    return Forecast(series("hourly", "precipitation_probability"), series("daily", "precipitation_probability_max"))


_lock = threading.Lock()
_cached: Forecast | None = None
_last_success = 0.0
_next_fetch = 0.0


def peek_forecast() -> Forecast | None:
    return _cached if time.time() - _last_success < 3600 else None


def get_forecast() -> Forecast | None:
    global _cached, _last_success, _next_fetch
    with _lock:
        now = time.time()
        if now < _next_fetch:
            return peek_forecast()
        try:
            response = httpx.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": 50.8798, "longitude": 4.7005,
                "hourly": "weather_code,precipitation_probability",
                "daily": "weather_code,precipitation_probability_max",
                "timezone": "Europe/Brussels", "forecast_days": 16,
            }, timeout=5)
            response.raise_for_status()
            forecast = parse_forecast(response.json())
            if not forecast.hourly and not forecast.daily:
                raise ValueError("Empty forecast")
            _cached, _last_success, _next_fetch = forecast, now, now + 900
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            _next_fetch = now + 60
        return peek_forecast()
