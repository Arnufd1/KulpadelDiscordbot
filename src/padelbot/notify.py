"""Discord webhook notifier. Used for booking outcomes and auth failures."""
from __future__ import annotations
import httpx
from loguru import logger

from .config import settings


def send(content: str, *, username: str = "Padel Bot") -> bool:
    """Fire-and-forget POST to a Discord webhook. Returns True on 2xx."""
    if not settings.discord_webhook_url:
        logger.warning("DISCORD_WEBHOOK_URL not set; skipping notification: {}", content)
        return False
    try:
        r = httpx.post(
            settings.discord_webhook_url,
            json={"content": content[:1900], "username": username},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except httpx.HTTPError as e:
        logger.error("Discord notify failed: {}", e)
        return False


def booked(slot_desc: str, booking_id: int | str) -> None:
    send(f":tennis: **Padel booked!** {slot_desc} (id `{booking_id}`)")


def failed(slot_desc: str, reason: str) -> None:
    send(f":x: **Booking failed** for {slot_desc}\n```{reason[:1500]}```")


def auth_dead(reason: str) -> None:
    send(
        ":warning: **Auth expired — re-login needed**\n"
        f"Reason: `{reason}`\n"
        "SSH into the Pi and run `padelbot login`."
    )
