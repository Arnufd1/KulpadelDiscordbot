"""CLI entrypoints for the bot.

Commands:
  padelbot init-key    one-time: generate the master encryption key
  padelbot login       one-time: interactive PKCE login (get refresh_token)
  padelbot status      show stored token state and try a refresh
  padelbot whoami      hit /auth on the booking API to verify access works
  padelbot notify-test send a test message to Discord
"""
from __future__ import annotations
import sys
import time
import webbrowser

import click
import httpx
from loguru import logger

from datetime import datetime, timezone

from .auth import (
    build_authorize_url,
    exchange_code,
    get_access_token,
    login_via_browser,
    parse_redirect,
    refresh,
    silent_refresh_via_session,
)
from .booking import book as book_slot, find_slot_at_time, find_slots, find_slots_window
from .booking_manager import cancel_booking, list_my_bookings
from .card_store import Card, CardStore
from .client import BackboneClient
from .notify import booked as notify_booked, send as notify_send
from .config import settings
from .notify import send as notify_send
from .store import TokenStore


def _store() -> TokenStore:
    return TokenStore(settings.padelbot_key_file, settings.padelbot_token_file)


@click.group()
def cli() -> None:
    logger.remove()
    logger.add(sys.stderr, level=settings.log_level)


@cli.command("init-key")
def init_key() -> None:
    """Generate a fresh master key. Refuses to overwrite an existing one."""
    s = _store()
    s.init_key()
    click.echo(f"Master key written to {s.key_file}")
    click.echo("Keep this file safe. If it leaks, attacker can decrypt your tokens.")


@cli.command()
@click.option(
    "--manual",
    is_flag=True,
    help="Manual mode: print the auth URL and prompt for the redirect URL "
    "(use if Playwright isn't installed).",
)
def login(manual: bool) -> None:
    """Interactive PKCE login. Default: Playwright captures the code automatically."""
    if manual:
        url, state, verifier = build_authorize_url()
        click.echo("\n1) Open this URL in a browser (DevTools → Network → 'Preserve log' first):")
        click.echo(f"   {url}\n")
        click.echo("2) Sign in + tap MFA. After redirect, in DevTools find the request to")
        click.echo("   /oidc/auth-callback?code=... and copy its full URL.\n")
        try:
            webbrowser.open(url)
        except Exception:
            pass
        callback = click.prompt("Paste the redirect URL").strip()
        code = parse_redirect(callback, state)
        bundle = exchange_code(code, verifier)
    else:
        click.echo("Launching browser. Sign in + tap MFA on your phone — I'll catch the code automatically.")
        bundle = login_via_browser(storage_state_path=settings.padelbot_storage_file)
        if settings.padelbot_storage_file.exists():
            click.echo(f"  storage state saved:     {settings.padelbot_storage_file}")
    _store().save(bundle)
    click.echo("\nTokens saved.")
    click.echo(f"  access_token expires in: {int(bundle.expires_at - time.time())}s")
    click.echo(f"  refresh_token returned:  {'YES' if bundle.refresh_token else 'NO (autonomous mode unavailable)'}")
    click.echo(f"  scopes granted:          {bundle.scope}")
    if not bundle.refresh_token:
        click.secho(
            "\nWARNING: no refresh_token. The IdP did not grant offline_access.\n"
            "Bot will not be able to run autonomously. Fall back to Playwright headless flow.",
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
    if bundle.refresh_token:
        click.echo("\nForcing a refresh to verify...")
        new_bundle = refresh(bundle)
        s.save(new_bundle)
        click.secho(
            f"Refresh OK. New access_token expires in {int(new_bundle.expires_at - now)}s.",
            fg="green",
        )


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
@click.option("--show-browser", is_flag=True, help="Show the browser window (debug).")
@click.option("--no-hint", is_flag=True, help="Skip id_token_hint (debug).")
def silent_refresh_cmd(show_browser: bool, no_hint: bool) -> None:
    """Try a silent renewal using the saved IdP session cookies (no MFA, no password).

    Tells you whether the session is still valid. This is the test that decides
    whether we can avoid storing your password.
    """
    s = _store()
    existing = s.load()
    hint = None if no_hint else (existing.id_token if existing else None)
    try:
        bundle = silent_refresh_via_session(
            settings.padelbot_storage_file,
            id_token_hint=hint,
            headless=not show_browser,
        )
    except RuntimeError as e:
        click.secho(f"Silent refresh FAILED: {e}", fg="red")
        sys.exit(1)
    s.save(bundle)
    click.secho("Silent refresh OK", fg="green")
    click.echo(f"  access_token expires in: {int(bundle.expires_at - time.time())}s")
    click.echo(f"  scope:                   {bundle.scope}")
    click.echo("\nIf you ran this many hours/days after `padelbot login` and it still")
    click.echo("worked, the IdP session is long-lived — we can avoid password storage.")


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
    notify_send(
        f":money_with_wings: **PAYMENT NEEDED** — open the KU Leuven Sport app and pay "
        f"{result.amount or '?'} EUR for booking `{result.booking_id}` within ~15 min "
        f"or the slot may be released."
    )


@cli.command("set-card")
def set_card_cmd() -> None:
    """Interactively prompt for credit card details and store them encrypted on the Pi.

    Used as a fallback for accounts without a SEPA mandate. The bot's primary
    payment path is direct debit if your account has a mandate.
    """
    holder = click.prompt("Card holder full name").strip()
    number = click.prompt("Card number (16 digits, no spaces)", hide_input=False).strip().replace(" ", "")
    if not number.isdigit() or len(number) < 12:
        click.secho("Card number looks invalid.", fg="red")
        sys.exit(1)
    exp_month = click.prompt("Expiry month (1-12)", type=int)
    exp_year = click.prompt("Expiry year (4-digit)", type=int)
    cvv = click.prompt("CVV (3-4 digits)", hide_input=True).strip()
    card = Card(holder=holder, number=number, exp_month=exp_month, exp_year=exp_year, cvv=cvv)
    store = CardStore(settings.padelbot_key_file, settings.padelbot_card_file)
    store.save(card)
    click.secho(f"Saved: {card.masked()}", fg="green")
    click.echo(f"Encrypted blob at: {settings.padelbot_card_file}")


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
