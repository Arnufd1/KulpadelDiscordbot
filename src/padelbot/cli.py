"""CLI entrypoints for the bot.

Commands:
  padelbot init-key    one-time: generate the master encryption key
  padelbot login       interactive PKCE login (browser or HTTP)
  padelbot status      show stored token state and try a refresh
  padelbot whoami      hit /auth on the booking API to verify access works
  padelbot notify-test send a test message to Discord
"""
from __future__ import annotations
import sys
import time
import os

import click
import httpx
from loguru import logger

from datetime import datetime, timezone

from .auth import (
    get_access_token,
    login_via_browser,
    login_via_http,
    login_via_android,
    renew_access_token,
)
from .booking import book as book_slot, find_slot_at_time, find_slots, find_slots_window
from .booking_manager import cancel_booking, list_my_bookings
from .card_store import Card, CardStore
from .client import BackboneClient
from .notify import booked as notify_booked, send as notify_send
from .config import settings
from .store import TokenStore


def _store() -> TokenStore:
    return TokenStore(settings.padelbot_key_file, settings.padelbot_token_file)


@click.group()
def cli() -> None:
    logger.remove()
    logger.add(sys.stderr, level=settings.log_level)


from .android_cli import android_cli
cli.add_command(android_cli)


@cli.command("init-key")
def init_key() -> None:
    """Generate a fresh master key. Refuses to overwrite an existing one."""
    s = _store()
    s.init_key()
    click.echo(f"Master key written to {s.key_file}")
    click.echo("Keep this file safe. If it leaks, attacker can decrypt your tokens.")


@cli.command()
@click.option("--http", "http_mode", is_flag=True, help="Log in over HTTP without Chromium; approve MFA when requested.")
@click.option("--android", "android_mode", is_flag=True, help="Passwordless login using the calibrated registered Android.")
@click.option("--manual-approval", is_flag=True, help="With --android: open the exact login on the phone for initial calibration.")
@click.option("--username", envvar="KULEUVEN_USERNAME", help="KU Leuven username (HTTP mode).")
@click.option("--device", envvar="KULEUVEN_AUTH_DEVICE", help="Authenticator device name if several are registered.")
def login(http_mode: bool, android_mode: bool, manual_approval: bool, username: str | None, device: str | None) -> None:
    """Interactive PKCE login. Default: Playwright captures the code automatically."""
    if http_mode and android_mode:
        raise click.UsageError("Choose --http or --android.")
    if manual_approval and not android_mode:
        raise click.UsageError("--manual-approval requires --android.")
    # Fail before contacting the IdP if the encryption key has not been created.
    if not settings.padelbot_key_file.exists():
        raise click.ClickException("Run `padelbot init-key` before login.")
    if android_mode:
        from .android import AndroidApprover
        try:
            approver = AndroidApprover(manual=manual_approval)
            if manual_approval:
                click.echo("Opening this login on your Android. Approve it on the phone or in the mirror window.")
            bundle = login_via_android(approver, fresh=True)
        except (RuntimeError, httpx.HTTPError) as error:
            raise click.ClickException(str(error)) from error
    elif http_mode:
        username = username or click.prompt("KU Leuven username")
        password = os.environ.get("KULEUVEN_PASSWORD") or click.prompt("KU Leuven password", hide_input=True)
        try:
            bundle = login_via_http(
                username, password, settings.padelbot_storage_file,
                provide_totp=lambda: click.prompt("TOTP code", hide_input=True),
                device_name=device,
                on_approval=lambda name: click.echo(f"Approve the KU Leuven Authenticator request on {name} (120s)."),
            )
        except (RuntimeError, httpx.HTTPError) as error:
            raise click.ClickException(str(error)) from error
    else:
        click.echo("Launching browser. Sign in + tap MFA on your phone — I'll catch the code automatically.")
        bundle = login_via_browser(storage_state_path=settings.padelbot_storage_file)
        if settings.padelbot_storage_file.exists():
            click.echo(f"  storage state saved:     {settings.padelbot_storage_file}")
    _store().save(bundle)
    click.echo("\nTokens saved.")
    click.echo(f"  access_token expires in: {int(bundle.expires_at - time.time())}s")
    click.echo(f"  refresh_token returned:  {'YES' if bundle.refresh_token else 'NO (uses saved IdP session)'}")
    click.echo(f"  scopes granted:          {bundle.scope}")
    if not bundle.refresh_token:
        mfa_message = (
            "New MFA logins use the configured Android automatically."
            if settings.padelbot_android_autologin else
            "Configure Android autologin or run `padelbot login --http` / `padelbot login` when MFA is required."
        )
        click.secho(
            "\nRenewal uses the saved IdP session over HTTP.\n"
            + mfa_message,
            fg="yellow",
        )


@cli.command()
def status() -> None:
    """Show token state. Forces a refresh to verify it works."""
    s = _store()
    bundle = s.load()
    if bundle is None:
        click.secho("No tokens stored. Run `padelbot login`.", fg="red")
        sys.exit(1)
    now = time.time()
    click.echo(f"access_token expires_at: {bundle.expires_at:.0f} ({int(bundle.expires_at - now)}s from now)")
    click.echo(f"refresh_token present:   {'YES' if bundle.refresh_token else 'NO'}")
    click.echo(f"scope:                   {bundle.scope}")
    click.echo("\nForcing a renewal to verify the saved session...")
    try:
        renew_access_token(s)
    except (RuntimeError, httpx.HTTPError) as error:
        raise click.ClickException(str(error)) from error
    click.secho("Renewal OK.", fg="green")


@cli.command()
def whoami() -> None:
    """Call backbone-web-api /auth with the access_token. Verifies bearer auth works."""
    token = get_access_token(_store())
    r = httpx.get(
        f"{settings.backbone_api_base}/auth?cf=0",
        headers={
            "Authorization": f"Bearer {token}",
            "Origin": "https://usc.kuleuven.cloud",
            "Referer": "https://usc.kuleuven.cloud/",
            "x-custom-lang": "nl",
        },
        timeout=15,
    )
    click.echo(f"status: {r.status_code}")
    if r.status_code == 200:
        d = r.json()
        click.secho(
            f"Hello {d.get('firstName')} {d.get('lastName')} "
            f"(member {d.get('id')}, {d.get('email')})",
            fg="green",
        )
    else:
        click.echo(r.text[:500])


@cli.command("silent-refresh")
def silent_refresh_cmd() -> None:
    """Renew the token; Android handles MFA when autologin is enabled."""
    s = _store()
    try:
        renew_access_token(s)
    except (RuntimeError, httpx.HTTPError) as e:
        click.secho(f"Silent refresh FAILED: {e}", fg="red")
        sys.exit(1)
    bundle = s.load()
    click.secho("Session renewal OK", fg="green")
    click.echo(f"  access_token expires in: {int(bundle.expires_at - time.time())}s")
    click.echo(f"  scope:                   {bundle.scope}")
    click.echo("\nRenewal reuses the IdP session or invokes the configured Android when MFA is needed.")


@cli.command("notify-test")
def notify_test() -> None:
    ok = notify_send(":wave: Padel bot notify test")
    click.echo("sent" if ok else "FAILED — check DISCORD_WEBHOOK_URL")


@cli.command("list-slots")
@click.option("--date", "date_str", required=True, help="Target date YYYY-MM-DD")
def list_slots_cmd(date_str: str) -> None:
    """List padel slots for a date (read-only, safe to run any time)."""
    from zoneinfo import ZoneInfo
    target = datetime.strptime(date_str, "%Y-%m-%d")
    tz = ZoneInfo("Europe/Brussels")
    with BackboneClient() as client:
        me = client.me()
        click.echo(f"Logged in as {me.get('firstName')} {me.get('lastName')} (id {me.get('id')})")
        slots = find_slots(client, target_date=target)
    if not slots:
        click.secho("No padel slots found for that date.", fg="yellow")
        return
    click.echo(f"\nFound {len(slots)} unique slot(s) on {date_str} (Brussels time):")
    for s in slots:
        local = s.start_dt.astimezone(tz)
        end_local = datetime.strptime(s.end_iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).astimezone(tz)
        avail = ":white_check_mark:" if s.raw.get("isAvailable") else " "
        click.echo(
            f"  {local:%H:%M}-{end_local:%H:%M}  parent={s.bookable_product_id} "
            f"slot={s.bookable_linked_product_id}"
        )


@cli.command("list-slots-week")
@click.option("--start", "start_str", default=None, help="Start date YYYY-MM-DD (default: today)")
@click.option("--days", default=7, type=int, help="Number of days (default: 7)")
@click.option("--court", type=int, default=None, help="Filter to a single court (1-5)")
def list_slots_week_cmd(start_str: str | None, days: int, court: int | None) -> None:
    """All available padel slots in the next N days as a court grid per day."""
    from zoneinfo import ZoneInfo
    from .booking import get_product_names
    tz = ZoneInfo("Europe/Brussels")
    start_dt = datetime.strptime(start_str, "%Y-%m-%d") if start_str else datetime.now(tz).replace(tzinfo=None)
    with BackboneClient() as client:
        me = client.me()
        click.echo(f"Logged in as {me.get('firstName')} {me.get('lastName')} (id {me.get('id')})")
        slots = find_slots_window(client, start_date=start_dt, days=days)
        court_ids = {s.bookable_product_id for s in slots}
        court_names = get_product_names(client, court_ids)
    if not slots:
        click.secho(f"No padel slots in the next {days} days.", fg="yellow")
        return

    if court is not None:
        target = f"padel {court}".lower()
        slots = [s for s in slots if (court_names.get(s.bookable_product_id) or "").strip().lower() == target]
        if not slots:
            click.secho(f"No slots on Padel {court} in the next {days} days.", fg="yellow")
            return
        court_ids = {s.bookable_product_id for s in slots}

    courts_sorted = sorted(court_ids, key=lambda c: court_names.get(c, str(c)))

    def col_label(cid: int) -> str:
        n = court_names.get(cid, f"#{cid}")
        if n.lower().startswith("padel"):
            rest = n[5:].strip()
            return f"P{rest}" if rest else "P?"
        return n[:4]

    by_day: dict[str, dict[str, set]] = {}
    day_meta: dict[str, tuple[bool, bool]] = {}
    for s in slots:
        local = s.start_dt.astimezone(tz)
        day_label = local.strftime("%a %d %b")
        by_day.setdefault(day_label, {}).setdefault(local.strftime("%H:%M"), set()).add(s.bookable_product_id)
        day_meta[day_label] = (s.is_holiday, s.day_has_bookings)

    click.echo(f"\n{len(slots)} open slots in the next {days} days  ({', '.join(f'{col_label(c)}={court_names.get(c, c)}' for c in courts_sorted)})")
    header = "Time   " + "  ".join(f"{col_label(c):>3s}" for c in courts_sorted)
    for day_label, time_map in by_day.items():
        total = sum(len(time_map[t]) for t in time_map)
        is_hol, has_bk = day_meta.get(day_label, (False, False))
        tag = ""
        if is_hol and not has_bk:
            tag = "  [HOLIDAY - likely closed, no bookings yet]"
        elif is_hol and has_bk:
            tag = "  [holiday but appears open]"
        click.echo(f"\n{day_label}  ({total} open){tag}")
        click.echo(header)
        for t in sorted(time_map.keys()):
            cells = "  ".join(("  X" if c in time_map[t] else "  .") for c in courts_sorted)
            click.echo(f"{t}  {cells}")


@cli.command()
@click.option("--date", "date_str", required=True, help="Target date YYYY-MM-DD")
@click.option("--time", "time_str", required=True, help="Local start time HH:MM (Brussels)")
@click.option("--court", type=int, default=None, help="Optional: court number 1-5")
@click.option("--label", default=None, help="Optional: free-text booking note (primaryPurchaseMessage)")
@click.option("--yes", is_flag=True, help="Skip confirmation — actually books.")
def book(date_str: str, time_str: str, court: int | None, label: str | None, yes: bool) -> None:
    """Book a specific padel slot. Without --yes, prints a dry-run preview."""
    from .booking import get_product_names
    target = datetime.strptime(date_str, "%Y-%m-%d")
    with BackboneClient() as client:
        me = client.me()
        click.echo(f"Member: {me.get('firstName')} {me.get('lastName')} (id {me.get('id')})")
        slots = find_slots(client, target_date=target)
        names = get_product_names(client, {s.bookable_product_id for s in slots})
        slot = find_slot_at_time(slots, time_str, court=court, court_names=names)
        if not slot:
            court_msg = f" on Padel {court}" if court else ""
            click.secho(f"No slot found at {time_str}{court_msg} on {date_str}.", fg="red")
            click.echo("Available at this time on other courts:")
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("Europe/Brussels")
            for s in slots:
                local = s.start_dt.astimezone(tz)
                if local.strftime("%H:%M") == time_str:
                    click.echo(f"  {names.get(s.bookable_product_id, '?')}")
            sys.exit(1)
        click.echo(f"\nWill book: {slot.label()} on {names.get(slot.bookable_product_id, '?')}")
        click.echo(f"  bookable_product_id:        {slot.bookable_product_id}")
        click.echo(f"  bookable_linked_product_id: {slot.bookable_linked_product_id}")
        click.echo(f"  start: {slot.start_iso}")
        click.echo(f"  end:   {slot.end_iso}")
        if not yes:
            click.secho("\nDRY RUN — re-run with --yes to actually book.", fg="yellow")
            return
        result = book_slot(client, slot, member_id=me["id"], label=label)
    click.secho(
        f"\nBooked! sale_id={result.sale_id} booking_id={result.booking_id} "
        f"amount={result.amount} '{result.description}'",
        fg="green",
    )
    notify_booked(result.description, result.booking_id or result.sale_id)
    from .payment import payment_message
    message = payment_message(result)
    click.echo(message)
    notify_send(message)


@cli.command("set-card")
def set_card_cmd() -> None:
    """Prompt locally for card details and store them encrypted on this computer.

    Used as a fallback for accounts without a SEPA mandate. The bot's primary
    payment path is direct debit if your account has a mandate.
    """
    click.echo("Automatisch betalen instellen. Voer kaartgegevens alleen in dit lokale venster in.")
    holder = click.prompt("Naam kaarthouder").strip()
    number = click.prompt("Kaartnummer (verborgen invoer)", hide_input=True).strip().replace(" ", "").replace("-", "")
    exp_month = click.prompt("Vervalmaand (1-12)", type=int)
    exp_year = click.prompt("Vervaljaar (4 cijfers)", type=int)
    cvv = click.prompt("CVC (verborgen invoer)", hide_input=True).strip()
    card = Card(holder=holder, number=number, exp_month=exp_month, exp_year=exp_year, cvv=cvv)
    store = CardStore(settings.padelbot_key_file, settings.padelbot_card_file)
    try:
        store.save(card)
    except ValueError as error:
        raise click.ClickException(str(error)) from None
    click.secho("Kaart versleuteld opgeslagen. De bot gebruikt ze bij je volgende boeking; herstarten is niet nodig.", fg="green")
    click.echo("Een eventuele goedkeuring in je bank-app blijft nodig.")


@cli.command("clear-card")
def clear_card_cmd() -> None:
    """Remove the saved card."""
    store = CardStore(settings.padelbot_key_file, settings.padelbot_card_file)
    store.clear()
    click.secho("Card cleared.", fg="yellow")


@cli.command("show-card")
def show_card_cmd() -> None:
    """Show the masked stored card."""
    store = CardStore(settings.padelbot_key_file, settings.padelbot_card_file)
    card = store.load()
    if card is None:
        click.echo("No card stored.")
        return
    click.echo(card.masked())


@cli.command("my-bookings")
def my_bookings_cmd() -> None:
    """List your upcoming padel bookings."""
    with BackboneClient() as c:
        me = c.me()
        bookings = list_my_bookings(c, member_id=me["id"], upcoming_only=True)
    if not bookings:
        click.echo("No upcoming bookings.")
        return
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Brussels")
    click.echo(f"\nUpcoming bookings — {me.get('firstName')} {me.get('lastName')}:")
    for b in bookings:
        local = b.start_dt.astimezone(tz)
        paid = "PAID" if b.paid_for else "unpaid"
        click.echo(f"  #{b.id}  {local:%a %d %b %H:%M}  status={b.status} {paid}  {b.description or ''}")


@cli.command("cancel-booking")
@click.option("--booking-id", type=int, required=True)
@click.option("--yes", is_flag=True, help="Actually cancel (otherwise dry-run)")
def cancel_booking_cmd(booking_id: int, yes: bool) -> None:
    """Cancel one of your bookings."""
    if not yes:
        click.echo(f"DRY RUN — would cancel booking #{booking_id}. Re-run with --yes.")
        return
    with BackboneClient() as c:
        me = c.me()
        resp = cancel_booking(c, booking_id=booking_id, member_id=me["id"])
    click.secho(f"Cancelled. Response: {resp}", fg="yellow")


@cli.command("discord")
def discord_cmd() -> None:
    """Start the Discord bot (long-running). Reads DISCORD_BOT_TOKEN, DISCORD_OWNER_ID from .env."""
    from .discord_bot import run
    run()


if __name__ == "__main__":
    cli()
