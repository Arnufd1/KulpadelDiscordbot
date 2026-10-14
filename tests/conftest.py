import pytest

from padelbot.config import settings
from padelbot import weather


@pytest.fixture(autouse=True)
def no_real_emulator_startup(monkeypatch):
    # Local production settings must not make ordinary unit tests boot a VM.
    # The explicit emulator tests enable and mock this feature themselves.
    monkeypatch.setattr(settings, "padelbot_android_emulator_autostart", False)


@pytest.fixture(autouse=True)
def no_external_weather_requests(monkeypatch):
    # Ordinary UI / booking tests never call a real third-party weather service.
    monkeypatch.setattr(weather, "get_forecast", lambda: None)
    monkeypatch.setattr(weather, "_cached", None)
    monkeypatch.setattr(weather, "_last_success", 0.0)
    monkeypatch.setattr(weather, "_next_fetch", 0.0)
