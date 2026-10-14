"""Local CLI for connecting and inspecting the owner's Android."""
import json
from pathlib import Path

import click

from .android import AndroidPinStore, doctor, inspect_app, list_devices, start_mirror
from .config import settings
from .store import TokenStore


@click.group("android")
def android_cli():
    """Connect your own Android and inspect the KU Leuven Authenticator app."""


@android_cli.command("devices")
def devices_cmd():
    """List Android USB/Wi-Fi connections and their authorization state."""
    try:
        click.echo(json.dumps(list_devices(), indent=2))
    except RuntimeError as error:
        raise click.ClickException(str(error)) from error


@android_cli.command("doctor")
def doctor_cmd():
    """Check ADB authorization, Android version and Authenticator installation."""
    try:
        click.echo(json.dumps(doctor(), indent=2))
    except RuntimeError as error:
        raise click.ClickException(str(error)) from error


@android_cli.command("inspect")
@click.option("--open-app", is_flag=True, help="Open the Authenticator app before reading its screen.")
@click.option("--output", type=click.Path(path_type=Path), default="data/android/screen.json", show_default=True)
def inspect_cmd(open_app: bool, output: Path):
    """Save visible controls; PIN fields, account IDs and links are redacted."""
    try:
        report = inspect_app(output, open_app=open_app)
    except RuntimeError as error:
        raise click.ClickException(str(error)) from error
    click.echo(f"Saved {len(report['nodes'])} Authenticator controls to {output}.")


@android_cli.command("set-pin")
def set_pin_cmd():
    """Enter the app PIN in a hidden local prompt and store it encrypted."""
    store = TokenStore(settings.padelbot_key_file, settings.padelbot_token_file)
    if not store.key_file.exists():
        store.init_key()
    pin = click.prompt("KU Leuven Authenticator app PIN", hide_input=True, confirmation_prompt=True)
    try:
        AndroidPinStore(settings.padelbot_key_file, settings.padelbot_android_pin_file).save(pin)
    except ValueError as error:
        raise click.ClickException(str(error)) from error
    click.echo("App PIN stored encrypted. Automatic approval requires a verified Android profile.")


@android_cli.command("reset-failure")
def reset_failure_cmd():
    """Allow another PIN attempt after you have checked the Authenticator app."""
    settings.padelbot_android_failure_file.unlink(missing_ok=True)
    click.echo("Android failure latch reset. The next login may make one PIN attempt.")


@android_cli.command("mirror")
def mirror_cmd():
    """Show your Android in a scrcpy window, controlled by mouse and keyboard."""
    try:
        process_id = start_mirror()
    except RuntimeError as error:
        raise click.ClickException(str(error)) from error
    click.echo(f"Starting KUL-Android mirror (process {process_id}); log: data/android/mirror.log")
