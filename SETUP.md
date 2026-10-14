# Setup — KU Leuven Padel Bot

## Discord-app registreren

1. Maak een app op [Discord Developer Portal](https://discord.com/developers/applications).
2. Open **Bot**, maak een token aan en vul `DISCORD_BOT_TOKEN` in je lokale `.env` in.
3. Open **Installation** en schakel **User Install** en **Guild Install** in. Bij **User Install** gebruik je alleen `applications.commands`. Bij **Guild Install** gebruik je `bot` en `applications.commands`, met rechten om berichten te bekijken/verzenden, embeds te plaatsen en berichtgeschiedenis te lezen voor de live overzichten.
4. Zet Discord Developer Mode aan en kopieer je eigen gebruikers-ID naar `DISCORD_OWNER_ID`.
5. Kopieer eventueel je server-ID naar `DISCORD_GUILD_ID` voor een extra snelle servergebonden commandoregistratie. Globale commando's worden daarnaast altijd geregistreerd voor DM's en groepschats.

De bot weigert commando's als de eigenaar-ID niet is ingesteld. Privileged Gateway Intents zijn niet nodig.

Op Windows kun je token en eigenaar-ID ook lokaal invoeren zonder het token weer te geven:

```powershell
.\.venv\Scripts\python.exe scripts/setup-discord.py
```

Dit script controleert het token en de gebruikers-ID bij Discord en past alleen deze twee instellingen in `.env` aan. De bestaande Android-instellingen blijven behouden. Plak het token in de verborgen terminalprompt.

### Gebruiken in een gewone groepschat

Installeer de app op je eigen account via de **Install Link** op de Installation-pagina en kies **Add to My Apps**. Daarna kun je de slash commands in een groeps-DM gebruiken. Discord staat niet toe dat een botaccount als gewone deelnemer aan een groeps-DM wordt toegevoegd; een gebruikersinstallatie werkt via interacties. Zie [Discord-installatiecontexten](https://docs.discord.com/developers/quick-start/getting-started) en [commandocontexten](https://docs.discord.com/developers/interactions/application-commands).

De code ondersteunt server, bot-DM en gebruikersinstallaties in privé-/groepschats. Alleen de ingestelde eigenaar kan commando's uitvoeren. De drie commando's openen een interactief paneel. Bestaande live achtergrondoverzichten vereisen een server met de bot geïnstalleerd, omdat die kanaalberichten later zelfstandig moeten worden bijgewerkt. Registratie en gebruik in Discord zijn pas live bewezen na configuratie van de app en het token.

De bot gebruikt vier globale commando's: `/checkbookings`, `/autobook`, `/bookings` en `/watcher`, in server-, DM- en groepschatcontexten. Bij het starten synchroniseert hij deze lijst en verwijdert hij de oude commando's. Als `DISCORD_GUILD_ID` is ingesteld, wordt diezelfde lijst ook voor die server geregistreerd. Credentials blijven uitsluitend in de lokale `.env`.

`/watcher dag:0 van:13 tot:20` volgt starttijden op vandaag, `dag:1` op morgen. Zonder dag toont `/watcher` de actieve watchers en hun gekoppelde chats. Een watcher blijft in de chat waar je hem start; meldingen gaan daar naartoe. Er wordt niets automatisch geboekt door deze dagwatchers. Het weer voor Leuven wordt eenvoudig getoond met een icoon en percentage neerslagkans.

Kies na het instellen **Privé** of **Openbaar + @everyone**. Privé toont het paneel en meldingen alleen aan jou in de oorspronkelijke chat. Openbaar deelt de watcher meteen met iedereen in diezelfde chat; hiervoor moet de app `Mention @everyone` mogen gebruiken. Doorlopende publieke meldingen vereisen een serverinstallatie. Privémeldingen en publieke meldingen via een gebruikersinstallatie werken tot vijftien minuten na een chatactie, met maximaal vijf vervolgberichten per gebruikersinstallatie-interactie. Klik op **Status vernieuwen** of **Vernieuw groepsbericht** om de tijdelijke chattoegang te vernieuwen; na een bot-herstart is dat ook nodig. Er is geen DM-terugval. Alleen de eigenaar kan de watcher beheren of boeken. De keuze en chat blijven opgeslagen en kunnen opnieuw naar privé worden gezet.

Voor automatisch starten en herstellen op Windows: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install-discord-autostart.ps1`. De pc moet aan, wakker en aangemeld blijven. Zie [README.md](README.md) voor uitschakelen en de achtergrondbewaker.

Test met `/checkbookings dag:0`, `/checkbookings dag:1`, `/checkbookings dag:2`, `/checkbookings dag:0-5`, `/bookings` en `/autobook`. `0`, `1` en `2` tonen respectievelijk vandaag, morgen en overmorgen. `0-5` toont vandaag tot en met over vijf dagen (zes dagen); spaties zoals `0 - 5` zijn ook toegestaan. **Deel met groep** deelt het beschikbaarheidsoverzicht openbaar in dezelfde chat. Het openen van deze panelen en de boekingsbevestiging maakt geen boeking of betaling aan. Na de keuze van tijdstip en veld verschijnt één bevestiging met het bedrag. Alleen **Bevestigen en betalen** start de reservatie en automatische betaling. De bot controleert eerst of een automatische betaalmethode beschikbaar is en meldt betaling pas als KUL ze bevestigt. **Annuleer deze reservatie** en het opslaan of hervatten van een automatische regel voeren ook echte acties uit. Een wekelijkse regel reserveert op een vrij veld en controleert op later vrijgekomen slots; hij blijft actief tot je hem pauzeert of verwijdert.

## Installeren en aanmelden op Windows

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item .env.example .env
# Vul de Discord-instellingen in .env in.
.\.venv\Scripts\padelbot.exe init-key
.\.venv\Scripts\padelbot.exe login --http
```

De gebruikersnaam en het wachtwoord worden gevraagd in de terminal; het wachtwoord wordt niet weergegeven of opgeslagen. Bij meerdere Authenticator-toestellen geeft de CLI hun namen terug. Herhaal de login met een gekozen toestel:

```powershell
.\.venv\Scripts\padelbot.exe login --http --device "Mijn gsm"
```

Bij een TOTP-account vraagt de CLI de code. Bij Authenticator-push heb je maximaal 120 seconden om op je gsm goed te keuren. Dit pad is voorbereid en met gesimuleerde antwoorden getest; verifieer de volledige login met je eigen account.

Gebruik zo nodig de browserlogin:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[login]"
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\padelbot.exe login
```

Het browservenster sluit zodra de logincode is onderschept. Handmatig kopiëren van een callback is verwijderd: de sportwebsite kan die eenmalige code al gebruikt hebben voordat je hem plakt.

## Controleren en starten

```powershell
.\.venv\Scripts\padelbot.exe whoami
.\.venv\Scripts\padelbot.exe silent-refresh
.\.venv\Scripts\padelbot.exe list-slots-week
.\.venv\Scripts\padelbot.exe discord
```

De eerste drie commando's boeken niets. `whoami` moet je eigen sportaccount tonen. `silent-refresh` controleert vernieuwing via de bestaande sessie; met Android-autologin ingeschakeld kan dit ook de automatische MFA-route gebruiken. Start pas daarna de bot en voeg gewenste regels toe in Discord.

Voor autonome aanmelding via een eigen geregistreerde Android volg je [de Android-installatie en kalibratie](docs/android-setup.md). Op de huidige werkplek zijn automatische login, tokenvernieuwing en heraanmelding na ontbrekende sessiegegevens live bevestigd. Zet `PADELBOT_ANDROID_AUTOLOGIN=true` pas nadat het eigen toestel en de app-PIN zijn gekalibreerd en getest.

Voor gebruik zonder fysieke gsm is op deze Windows-werkplek ook een geregistreerde virtuele Android getest, inclusief automatische start op de achtergrond en opnieuw aanmelden na sessieverloop. Zie [software-authenticator](docs/software-authenticator.md) voor registratie, afzonderlijke PIN/profielbestanden en `PADELBOT_ANDROID_EMULATOR_AUTOSTART`.

Zonder geconfigureerde Android-route voer je na verlopen MFA opnieuw `login --http` of `login` uit op dezelfde machine. Keepalive leest de vernieuwde bestanden bij de volgende poging; de bot hoeft niet opnieuw gestart te worden. Netwerkfouten worden opnieuw geprobeerd. Eén DM meldt de eerste fout; succesvolle vernieuwing zet die meldingsstatus terug.

## Linux / Raspberry Pi

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env
# Vul je Discord-instellingen in.
.venv/bin/padelbot init-key
.venv/bin/padelbot login --http
.venv/bin/padelbot whoami
.venv/bin/padelbot silent-refresh
.venv/bin/padelbot discord
```

HTTP-login en sessievernieuwing hebben geen Chromium nodig. Voor de Worldline-kaartdriver moet je de browserafhankelijkheden wel installeren. Voer de bot met een procesbeheerder zoals systemd uit vanuit de projectmap, zodat `.env` en `data/` gevonden worden.

Cookies en localStorage in `data/storage_state.json` zijn niet versleuteld. Beperk toegang tot de projectmap; deel sessiebestanden, `.env`, sleutels en HAR-captures niet. Linux-gebruikers kunnen `chmod 700 data` en `chmod 600 data/* .env` toepassen. Stel op Windows toegang via bestandsrechten in.

## Linux-service op Proxmox

De geteste opstelling gebruikt Ubuntu Server 24.04, een eigen systeemgebruiker `padelbot` met home `/var/lib/padelbot` en aanvullend lidmaatschap van de groep `kvm`. De code, venv, Android SDK en persistente gegevens staan onder `/opt/padelbot`. `.env` en `data/` zijn alleen toegankelijk voor deze gebruiker; de bestaande master key hoort bij de versleutelde tokens, kaart en app-PIN. Het overzetten van een werkende installatie maakt geen nieuwe sleutel aan.

Installeer de Python-extras `login` en `android`. Headless Chromium en zijn systeembibliotheken zijn nodig voor de kaartdriver; de geteste installatie gebruikt `PLAYWRIGHT_BROWSERS_PATH=/opt/padelbot/.tools/playwright` en `python -m playwright install --with-deps --only-shell chromium`. De Android SDK staat onder `.tools/android`, de geregistreerde AVD onder `data/emulators` en de bijbehorende ADB-sleutel onder `/var/lib/padelbot/.android`. Zie [software-authenticator](docs/software-authenticator.md) voor de gecontroleerde image en tijdelijke RAM/CPU-instellingen.

Als de bestanden en gebruikersrechten klaarstaan, installeer de service vanuit de projectmap:

```bash
sudo install -m 644 deploy/padelbot.service /etc/systemd/system/padelbot.service
sudo install -m 644 deploy/padelbot-memory.conf /etc/tmpfiles.d/padelbot-memory.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/padelbot-memory.conf
sudo systemctl daemon-reload
sudo systemctl enable --now padelbot.service
```

De service start zonder aangemelde gebruiker en herstart bij fouten. Bij stoppen sluit systemd ook achtergebleven emulatorprocessen in dezelfde procesgroep. De bot start Android uitsluitend bij een echte MFA-stap en sluit zijn eigen emulator na die poging weer af. Gewone HTTP-vernieuwing gebruikt geen Android. De service zet Vulkan uit voor de eenvoudige Authenticator-schermen en bewaart logs in journald.

Controleer met `systemctl status padelbot.service`, `journalctl -u padelbot.service -f` en een API-opvraging. Een herstarttest controleert automatisch opkomen, dezelfde opgeslagen regels en een afgesloten emulator zolang geen MFA nodig is. Schakel bij migratie de Windows-bewaker en autostart uit voordat de Linux-bot actief wordt, zodat één bot de opgeslagen boekingsregels uitvoert.
