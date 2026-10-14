import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx

from padelbot import weather, discord_ui

GET_FORECAST = weather.get_forecast
TZ = ZoneInfo("Europe/Brussels")


def payload():
    return {"hourly": {"time": ["2030-06-05T14:00", "2030-06-05T15:00", "2030-06-05T16:00"],
        "weather_code": [0, 61, 3], "precipitation_probability": [5, 70, 20]},
        "daily": {"time": ["2030-06-05"], "weather_code": [61], "precipitation_probability_max": [70]}}


def test_rain_badge_covers_game_hours_and_converts_utc_to_leuven():
    forecast = weather.parse_forecast(payload())
    start = datetime(2030, 6, 5, 11, 30, tzinfo=ZoneInfo("UTC"))  # 13:30 local
    assert forecast.badge(start) == "🌧️ 70%"
    assert forecast.badge(start, start.replace(hour=12, minute=0)) == "☀️ 5%"
    assert forecast.day_badge(start.date()) == "🌧️ 70%"
    assert forecast.day_badge(start.replace(day=6).date()) == ""


def test_missing_probability_is_unknown_instead_of_zero():
    forecast = weather.parse_forecast({"daily": {"time": ["2030-06-05"], "weather_code": [3], "precipitation_probability_max": [None]}})
    assert forecast.day_badge(datetime(2030, 6, 5).date()) == "☁️ ?%"
    assert weather.icon(0) == "☀️" and weather.icon(3) == "☁️" and weather.icon(61) == "🌧️"


def test_forecast_is_cached_and_outage_never_fabricates_a_dry_forecast(monkeypatch):
    monkeypatch.setattr(weather, "get_forecast", GET_FORECAST)
    clock = [10000]
    monkeypatch.setattr(weather.time, "time", lambda: clock[0])
    calls = []
    def get(url, **kwargs):
        calls.append(kwargs)
        return httpx.Response(200, json=payload(), request=httpx.Request("GET", url))
    monkeypatch.setattr(weather.httpx, "get", get)
    first = weather.get_forecast()
    assert weather.get_forecast() is first and len(calls) == 1
    assert calls[0]["params"]["timezone"] == "Europe/Brussels"
    assert calls[0]["params"]["forecast_days"] == 16
    def failure(*args, **kwargs): raise httpx.ConnectError("offline")
    monkeypatch.setattr(weather.httpx, "get", failure)
    clock[0] += 901
    assert weather.get_forecast() is first  # recent cached forecast
    clock[0] += 3600
    assert weather.get_forecast() is None


def test_weather_survives_selection_changes_and_appears_on_reservations():
    forecast = weather.parse_forecast(payload())
    start = datetime(2030, 6, 5, 13, 30, tzinfo=TZ)
    slot = SimpleNamespace(start_dt=start, bookable_product_id=4385)
    async def run():
        view = discord_ui.DaySlotsView(42, start, [slot], {4385: "Padel 4"}, forecast=forecast)
        assert "🌧️ 70%" in view.embed().description
        edits = []
        async def edit(**kwargs): edits.append(kwargs)
        select = view.children[0]
        select._values = ["13:30"]
        await select.callback(SimpleNamespace(response=SimpleNamespace(edit_message=edit)))
        assert edits[-1]["view"].forecast is forecast
        mine = [SimpleNamespace(id=1, start_dt=start, raw={"productId": 4385})]
        bookings = discord_ui.BookingsView(42, mine, {4385: "Padel 4"}, forecast=forecast)
        assert "🌧️ 70%" in bookings.embed().description
        assert "Open-Meteo" in bookings.embed().footer.text
    asyncio.run(run())
