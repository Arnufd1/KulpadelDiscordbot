# Setup — KU Leuven Padel Bot (Discord interface)

## One-time: Discord bot registration

1. Go to https://discord.com/developers/applications and click **New Application**. Name it `KUL Padel Bot` (or whatever).
2. In the left nav, click **Bot**. The default bot is fine.
3. **Reset Token** → copy the token. Paste it into `.env` as `DISCORD_BOT_TOKEN=...`. (Treat this like a password.)
4. Under **Privileged Gateway Intents**: nothing extra is needed. Slash commands work without intents.
5. **Installation** (left nav) → set Install Link → "Discord Provided Link". Under **Default Install Settings**, choose:
   - **Scopes:** `bot`, `applications.commands`
   - **Permissions:** `Send Messages` is enough (the bot uses ephemeral slash command responses + DMs)
6. Copy the install URL, open it, pick your server, click **Authorize**.

## One-time: get your Discord IDs

1. In Discord, **Settings → Advanced → Developer Mode** = **on**.
2. Right-click your own name in any channel → **Copy User ID**. Paste into `.env` as `DISCORD_OWNER_ID=...`.
3. Right-click your server in the sidebar → **Copy Server ID**. Paste into `.env` as `DISCORD_GUILD_ID=...`. *(Optional but recommended — without it slash commands take up to 1 hour to appear.)*

## One-time: KU Leuven login

```powershell
.venv\Scripts\padelbot init-key
.venv\Scripts\padelbot login
.venv\Scripts\padelbot whoami        # confirm bearer auth works
```

## Start the bot

```powershell
.venv\Scripts\padelbot discord
```

That's it. Open Discord, type `/` in any channel where the bot is, and you'll see:

| Command | What it does |
|---|---|
| `/status` | Auth health, next scheduled fire, recent history |
| `/slots day:2026-05-13` | List padel slots for that date |
| `/book day:2026-05-13 time:18:00` | Book a single slot now |
| `/auto-add weekday:Monday time:18:00` | Add a weekly recurring rule |
| `/auto-list` | List all rules + their next fire moment |
| `/auto-remove rule_id:3` | Delete a rule |
| `/auto-toggle rule_id:3 enabled:False` | Pause a rule without deleting it |
| `/history` | Last 10 booking attempts |

## Re-login flow (when the IdP session dies)

The Shibboleth session lifetime appears to be roughly 1 hour. When it dies:

1. The bot's `/status` shows `Auth: DEAD`.
2. The scheduler will fail the next attempt and DM you the error.
3. SSH into the Pi (Tailscale) and run `padelbot login`, tap MFA on your phone.
4. Resume normal operation.

For weekly bookings, this means: re-login within ~1h before the slot opens. The bot will DM you a reminder if you set up rules and the auth dies before fire time.

## Pi deployment (later)

Once tested locally, copy the project to the Pi, install, and create a systemd unit that runs `padelbot discord`. Use Tailscale for remote SSH access.
