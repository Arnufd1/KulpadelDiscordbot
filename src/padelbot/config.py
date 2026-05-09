from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    kul_client_id: str = "usc"
    kul_authorize_url: str = "https://idp.kuleuven.be/idp/profile/oidc/authorize"
    kul_token_url: str = "https://idp.kuleuven.be/idp/profile/oidc/token"
    kul_redirect_uri: str = "https://usc.kuleuven.cloud/oidc/auth-callback"
    kul_scope: str = "openid profile email cardinfo offline_access"

    backbone_api_base: str = "https://backbone-web-api.production.leuven.delcom.nl"

    padelbot_key_file: Path = Path("./data/master.key")
    padelbot_token_file: Path = Path("./data/tokens.enc")
    padelbot_storage_file: Path = Path("./data/storage_state.json")
    padelbot_card_file: Path = Path("./data/card.enc")

    discord_webhook_url: str = ""
    discord_bot_token: str = ""
    discord_owner_id: int = 0          # only this Discord user can use commands
    discord_guild_id: int = 0          # if set, slash commands appear instantly in this server (otherwise global, ~1h)

    padelbot_rules_file: Path = Path("./data/rules.sqlite3")
    padel_member_id: int = 0
    padel_opens_days_ahead: int = 7
    padel_open_hour_local: int = 0     # hour-of-day (Brussels) when slots open (best guess: midnight)
    padel_fire_offset_ms: int = 200    # fire this many ms before slot opens
    padel_local_tz: str = "Europe/Brussels"

    # Keep the IdP session warm: call silent-refresh every N seconds.
    # Default 25 min — well under typical Shibboleth idle timeout (30-60 min).
    padelbot_keepalive_interval_s: int = 1500

    # Credit card for the headless Worldline driver. .env is gitignored.
    # Alternative: `padelbot set-card` stores them encrypted in data/card.enc.
    kul_card_holder: str = ""
    kul_card_number: str = ""
    kul_card_exp_month: int = 0
    kul_card_exp_year: int = 0
    kul_card_cvv: str = ""
    # Live slots message refresh cadence. The API has no realtime feed —
    # we just poll. 120s (2 min) is a reasonable balance.
    padelbot_slots_refresh_s: int = 120
    # Live bookings message refresh cadence.
    padelbot_bookings_refresh_s: int = 300
    # Slot watcher (Type B alerts for cancellations).
    padelbot_watcher_poll_s: int = 120

    log_level: str = "INFO"


settings = Settings()
