"""Configure Discord locally with a hidden token prompt; preserve Android settings."""
from getpass import getpass
from pathlib import Path
import os
import re
import sys

import httpx


def main():
    root = Path(__file__).resolve().parents[1]
    env_file = root / '.env'
    print('Discord-configuratie voor de padelbot. Het token wordt niet weergegeven.')
    token = getpass('Bot-token uit Discord Developer Portal > Bot: ').strip()
    owner = input('Je eigen Discord-gebruikers-ID (Developer Mode > Copy User ID): ').strip()
    if not token or '\n' in token or '\r' in token or not re.fullmatch(r'[A-Za-z0-9_.-]+', token):
        sys.exit('Ongeldig tokenformaat; niets gewijzigd.')
    if not re.fullmatch(r'[0-9]{17,20}', owner) or int(owner) <= 0:
        sys.exit('Ongeldige gebruikers-ID; niets gewijzigd.')
    try:
        with httpx.Client(base_url='https://discord.com/api/v10', timeout=15,
                          headers={'Authorization': f'Bot {token}'}) as client:
            response = client.get('/users/@me')
            if response.status_code != 200 or not response.json().get('bot'):
                sys.exit(f'Discord accepteert dit bot-token niet (HTTP {response.status_code}); niets gewijzigd.')
            bot_id = response.json()['id']
            profile = client.get(f'/users/{owner}')
            if profile.status_code != 200 or profile.json().get('bot'):
                sys.exit('Je gebruikers-ID kon niet worden bevestigd; niets gewijzigd.')
    except httpx.HTTPError:
        sys.exit('Discord kon niet worden bereikt; niets gewijzigd.')

    existing = env_file.read_text(encoding='utf-8') if env_file.exists() else ''
    updates = {'DISCORD_BOT_TOKEN': token, 'DISCORD_OWNER_ID': owner}
    lines = []
    for line in existing.splitlines():
        match = re.match(r'\s*(DISCORD_BOT_TOKEN|DISCORD_OWNER_ID)\s*=', line, re.I)
        if match:
            continue
        lines.append(line)
    lines.extend(f'{key}={value}' for key, value in updates.items())
    temporary = env_file.with_name('.env.discord-setup.tmp')
    try:
        temporary.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        os.replace(temporary, env_file)
    finally:
        if temporary.exists():
            temporary.unlink()
    print('Token en eigenaar lokaal ingesteld; Android-instellingen behouden.')
    print('Developer Portal > Installation: schakel User Install in met applications.commands.')
    print(f'Installeren op je account: https://discord.com/oauth2/authorize?client_id={bot_id}&integration_type=1&scope=applications.commands')
    print('Start de bot: .\\.venv\\Scripts\\padelbot.exe discord')
    print('Eerste test: /status, /slots day:2026-10-09 en /card-status.')


if __name__ == '__main__':
    main()
