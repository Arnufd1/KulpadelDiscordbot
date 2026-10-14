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
