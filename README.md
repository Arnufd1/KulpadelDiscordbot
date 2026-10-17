# Sigmadvisor Padel Tool

Python-bot om padelvelden van KU Leuven Sport via Discord te bekijken en te reserveren, met wekelijkse regels, meldingen bij vrijgekomen slots, gedeelde reservaties en automatische betaling. De gecontroleerde authenticatie, betalingen en Linux-hosting staan hieronder beschreven.

De persoonlijke webapp voor `padel.sigmadvisor.com` draait op de mini-pc en gebruikt Google-login, gescheiden gebruikersprofielen, een gedeelde emulator die op aanvraag start, en privébeheer via Tailscale. Installatie, betalingsmogelijkheden en resterende verificatie staan in [web-hosting](docs/web-hosting.md).

## Website en Discord

Open [het dashboard](https://padel.sigmadvisor.com), meld aan met Google, koppel KUL en klik **Toevoegen aan Discord**. De Discord-autorisatie koppelt je eigen profiel en installeert de bot; een persoonlijke installatie zonder server staat onder Instellingen. `/checkbookings`, `/bookings` en `/autobook` gebruiken je eigen profiel.

Kies bij Betalen een bij KUL gekoppelde betaalmethode, of een betaallink via Discord. Bij KUL blijft het saldo, de geldige standaardkaart of de domiciliëring leidend. De losse kaartkluis verleent geen betaalmachtiging. Zonder automatische betaalmethode krijgt dezelfde privéboodschap de hosted betaallink. De bot controleert eerst of hij dat privébericht kan afleveren; bij een fout wordt niets gereserveerd. Een bankcontrole voor een gekoppelde kaart wordt eveneens via Discord afgehandeld.

Met `PADELBOT_PLATFORM_ROOT=/opt/padelbot/data/platform` gebruiken Discord en web dezelfde profielkoppeling, voorkeuren, takenwachtrij, boekingsworker en matchdeelnames. De oude automatische Discord-scheduler wordt dan uitgeschakeld: één worker voert de regels van alle profielen uit. Standalone CLI-installaties zonder deze instelling behouden hun bestaande gedrag.

## Snel starten op Windows

Python 3.10 of nieuwer is vereist. Voer de commando's uit vanuit de projectmap.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item .env.example .env
# Vul DISCORD_BOT_TOKEN, DISCORD_OWNER_ID en eventueel DISCORD_GUILD_ID in.
.\.venv\Scripts\padelbot.exe init-key
.\.venv\Scripts\padelbot.exe login --http
.\.venv\Scripts\padelbot.exe whoami
.\.venv\Scripts\padelbot.exe silent-refresh
.\.venv\Scripts\padelbot.exe discord
```

`init-key` voer je één keer uit; een bestaande sleutel wordt niet overschreven. Zie [SETUP.md](SETUP.md) voor de Discord-registratie en Linux-installatie.

## Authenticatie

- `login --http` vraagt je KU Leuven-gebruikersnaam en verborgen wachtwoord. Bij MFA vraagt de CLI een TOTP-code of wacht hij op goedkeuring in de KU Leuven Authenticator-app. Met meerdere geregistreerde toestellen kies je `--device "Toestelnaam"`.
- `login --android` opent de eigen loginlink op een gekalibreerde, geregistreerde Android en bedient de app met de lokaal versleutelde app-PIN. Zie [Android-installatie en kalibratie](docs/android-setup.md).
- `login` opent een browser als alternatief. Installeer hiervoor `pip install -e ".[login]"` en `python -m playwright install chromium`. De callback wordt geblokkeerd voordat de sportwebsite de eenmalige code kan gebruiken.
- `silent-refresh` controleert sessievernieuwing via HTTP. De bot gebruikt een refresh token als de IdP er een verstrekt, anders de opgeslagen cookies en localStorage.
- Stille HTTP-vernieuwing stopt vóór wachtwoord- of MFA-stappen. Met `PADELBOT_ANDROID_AUTOLOGIN=true` start keepalive vervolgens de gekalibreerde Android-flow als MFA nodig is. Keepalive probeert opnieuw na een fout en hervat zonder herstart van de bot.

De Android-route is op 8 oktober 2026 met een eigen sportaccount live getest: account kiezen, Samsung's normale **Use PIN**-optie, app-PIN invoeren, OIDC-tokens opslaan en de reservatie-API bereiken met HTTP 200. Stille HTTP-vernieuwing is ook live bevestigd. De Android moet bereikbaar en ontgrendeld blijven. Zonder geconfigureerde Android-flow vraagt nieuwe MFA handmatige goedkeuring. De implementatie en bronnen staan in [docs/auth-research.md](docs/auth-research.md).

Tokens worden met Fernet versleuteld in `data/tokens.enc`. `data/storage_state.json` bevat cookies en localStorage in leesbare vorm. Beide bestanden en de sleutel blijven buiten Git. De bot bewaart geen KU Leuven-wachtwoord; de HTTP-login kan ook `KULEUVEN_USERNAME`, `KULEUVEN_PASSWORD` en `KULEUVEN_AUTH_DEVICE` uit de procesomgeving lezen.

Op 9 oktober 2026 zijn registratie en automatische USC-aanmelding met de officiële app op een virtuele Android live bevestigd. Op 14 oktober is deze geregistreerde Android overgezet naar een minimale Ubuntu-VM op Proxmox: een nieuwe login vanaf een afgesloten emulator, dezelfde KUL-account en toegang tot de reservatie-API zijn live gecontroleerd. De bot start de emulator alleen bij een echte MFA-vraag en sluit zijn eigen toestel na de loginpoging weer af, ook bij fouten; gewone HTTP-vernieuwing heeft geen emulator nodig. Op Linux geeft hij daarna ongebruikte Android-bestandscache vrij. Een fysieke gsm of draaiende Windows-pc is voor deze host niet nodig. Zie [software-authenticator](docs/software-authenticator.md) en [Linux-service](SETUP.md#linux-service-op-proxmox).

## Discord

Voor de Android-route naar autonome Authenticator-login, inclusief schermspiegeling met scrcpy en de actuele teststatus, zie [docs/android-setup.md](docs/android-setup.md).

Commando's zijn uitsluitend beschikbaar voor `DISCORD_OWNER_ID`.

Slash commands ondersteunen ook gebruikersinstallatie via **Add to My Apps**, zodat de eigenaar ze in een gewone groepschat kan gebruiken. Schakel daarvoor **User Install** in de Developer Portal in. Live achtergrondoverzichten vereisen een serverinstallatie. Zie [Discord-instellingen](SETUP.md).

| Commando | Functie |
|---|---|
| `/checkbookings dag:0-5` | Vandaag tot en met over vijf dagen bekijken (zes dagen); `0` is alleen vandaag, `1` alleen morgen en `2` alleen overmorgen. Kies een dag, tijdstip en veld om te boeken, of klik **Deel met groep** voor het volledige overzicht in de huidige chat. |
| `/autobook` | Kies een weekdag en een begin- en einduur via dropdowns. De bot boekt wekelijks één vrij tijdstip binnen dit interval. Pauzeren, hervatten en verwijderen via de UI. |
| `/bookings` | Eigen komende reservaties bekijken, met de groep delen of met de rode knop annuleren. |
| `/watcher dag:0 van:13 tot:20` | Volg vrije starttijden voor vandaag, inclusief 20:00. `1` is morgen. De watcher en meldingen blijven in de chat waar je hem start. |

Bij `/autobook` kies je één weekdag, een beginuur en een einduur in Belgische tijd. De urenmenu's gaan van 00:00 tot 23:00. Met 18:00–21:00 kiest de bot de vroegste vrije starttijd binnen dat venster, inclusief 21:00 en tussenliggende starttijden zoals 18:30. Dit is één boeking per regel per week; het einduur is de laatste toegestane starttijd van het spel. Kies twee gelijke uren voor één vast tijdstip. Bestaande regels met één tijdstip blijven werken. Na een boeking wordt dezelfde week ook na een herstart niet opnieuw geboekt.

`/watcher` zonder dag toont je actieve watchers. Elke watcher heeft **Stop watcher**, **Status vernieuwen** en **Bekijk vrije slots / boek**. Je krijgt een melding wanneer een starttijd vrijkomt of wanneer het laatste vrije veld voor een eerder vrije starttijd verdwijnt. Gelijkblijvende beschikbaarheid geeft geen herhaalde meldingen; een API-fout wordt nooit als volzet gemeld. Controle gebeurt standaard elke twee minuten, gedeeld per dag. Het tijdsvenster slaat op de starttijd van een spel, niet op hoe laat het spel eindigt. De watcher stopt bij het einduur, blijft na herstart bestaan en boekt zelf niets. De normale boekingsknop vraagt je bevestiging vóór reserveren en betalen. Het opnieuw starten van dezelfde actieve watcher maakt geen dubbel exemplaar.

Weer voor Leuven verschijnt als bijvoorbeeld **☀️ 10%**, **☁️ 30%** of **🌧️ 70%** bij vrije tijden, eigen reservaties, groepsberichten en watchers. De voorspelling komt van [Open-Meteo](https://open-meteo.com/en/docs), wordt maximaal elke vijftien minuten opgehaald en toont de hoogste voorspelde kans op neerslag per uur tijdens het spel (of over de dag bij de dagsamenvatting). Dit is geen gecombineerde kans over de hele dag. Een ontbrekende voorspelling wordt niet als 0% weergegeven en verhindert boeken niet.

Op Windows houdt een achtergrondbewaker de bot online en herstart hij een afgesloten bot. Installeer voor je eigen Windows-account:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install-discord-autostart.ps1
```

De taak **KUL Padel Discord Bot** start bij Windows-aanmelding, controleert elke minuut of de bewaker draait en herstart een mislukte taak. De bewaker controleert het botproces elke vijf seconden. Een proceslock voorkomt twee gelijktijdige botinstanties. Er wordt geen admin-wachtwoord of bot-token in de taak opgeslagen. Als Task Scheduler niet beschikbaar is, gebruikt het script de opstartsleutel van het eigen Windows-account. Uitschakelen: voer hetzelfde script uit met `-Remove`; de huidige bot blijft dan draaien. Logs en status staan in `data/discord.supervisor.*`. De pc moet aan, wakker en aangemeld zijn om Discord-commando's te ontvangen. De virtuele authenticator start afzonderlijk wanneer een KUL-login nodig is.

Bij `/checkbookings` is `dag` optioneel (standaard `0`) en accepteert het één dag of een inclusief bereik tussen 0 en 365: `0-5`, `0 -5` of `0 - 5` toont zes dagen, van vandaag tot en met over vijf dagen. Ook andere bereiken zoals `0-3` blijven mogelijk. De datums worden berekend in Belgische tijd. Het bereik toont vrije tijden en velden per dag; kies een dag om de gewone boekingsbevestiging te openen. Met **Terug naar alle dagen** keer je terug naar het bereik. Je kunt met **Vorige dag**, **Volgende dag** en **Vernieuwen** door het dagoverzicht gaan. KUL opent reservaties maximaal 168 uur vooraf; verder vooruit kijken maakt die slots niet eerder boekbaar. Tijdstippen, dagbereiken en reservaties worden gepagineerd zodat latere opties niet wegvallen.

**Deel met groep** plaatst de getoonde beschikbaarheid openbaar in de huidige chat, zonder iedereen te taggen. Vanuit het bereik deel je alle gekozen dagen; vanuit een dagoverzicht deel je die dag. Groepsleden kunnen door lange gedeelde overzichten bladeren, zonder boekingsknoppen. Het is een momentopname met controletijd; de bot blijft die niet automatisch verversen. Bij meerdere pagina's bevat het bericht ook het volledige overzicht als tekstbestand, zodat alle dagen leesbaar blijven nadat de knoppen verlopen (15 minuten zonder gebruik) of de bot herstart. Een dubbele deelklik maakt geen tweede bericht. De app moet in de chat openbaar mogen antwoorden.

Na het instellen van een watcher kies je onderaan **Privé** of **Openbaar + @everyone**. De watcher is gekoppeld aan de chat waar je hem start. Privé is de standaard: het paneel en meldingen zijn alleen voor jou zichtbaar in die chat. Openbaar plaatst meteen een groepsbericht in dezelfde chat, met `@everyone`; daarvoor moet de app die toestemming hebben. Nieuwe publieke beschikbaarheidsmeldingen verschijnen daar ook met `@everyone`. De keuze en de oorspronkelijke chat blijven bewaard na herstart. Dezelfde dag en uren kunnen in verschillende chats elk een eigen watcher hebben. Een dubbele klik of een onzekere verzenduitkomst tagt de groep niet automatisch opnieuw.

Privémeldingen en publieke meldingen in gewone groepschats of servers met alleen een gebruikersinstallatie gebruiken tijdelijke chattoegang. [Discord-interactiontokens zijn vijftien minuten geldig en gebruikersinstallaties hebben maximaal vijf vervolgberichten per interactie](https://docs.discord.com/developers/interactions/receiving-and-responding#followup-messages). Na afloop, bij het bereiken van die limiet of na een herstart hervat de eigenaar de meldingen via **Status vernieuwen** of `/watcher` in de oorspronkelijke chat; iedereen kan een publieke watcher hervatten met **Vernieuw groepsbericht**. Nog niet verstuurde wijzigingen blijven bewaard. Alleen publieke watchers in serverkanalen waar de bot is geïnstalleerd kunnen doorlopend zelfstandig melden. **Beheren (organisator)** opent alleen voor de eigenaar de privéknoppen om te stoppen, boeken of terug te schakelen naar privé. Terugschakelen stopt toekomstige publieke meldingen en schermt het oorspronkelijke overzicht af waar Discord dit toestaat; eerder verstuurde meldingen blijven zichtbaar. De bot verstuurt geen watcher-DM's, ook niet wanneer chattoegang of kanaalrechten ontbreken. Tijdelijke Discord-tokens blijven uitsluitend in het geheugen.

De panelen zijn alleen voor de eigenaar zichtbaar. Na het kiezen van het veld verschijnt één bevestiging met datum, tijd, veld en je accountprijs. Tot je **Bevestigen en betalen** klikt, worden alleen beschikbaarheid, prijs en betaalconfiguratie gecontroleerd. Daarna reserveert en betaalt de bot automatisch. De bevestiging is twee minuten geldig; dubbelklikken maakt geen tweede boeking. De bot controleert opnieuw of het slot, account en bedrag nog overeenkomen en betaalt nooit een afwijkend verkoopbedrag. Als KUL bij het aanmaken van de aankoop toch een ander bedrag rekent, probeert de bot alleen die nieuwe, nog onbetaalde reservatie te annuleren.

Na een betaalde boeking verschijnen **Deel met groep** en **Deel + @everyone**. In `/bookings` kun je één of meerdere reservaties aanvinken, maximaal vijf per deelactie. De selectie blijft bewaard wanneer je naar een andere pagina bladert; **Selectie wissen** maakt ze leeg. Met **Deel selectie** plaats je voor elke gekozen reservatie een eigen openbaar bericht met datum, tijd, veld en deelnemers in de huidige groepschat of het serverkanaal. **Deel selectie + @everyone** tagt de groep één keer voor de hele selectie; daarvoor moet de app die toestemming hebben. Groepsleden kunnen bij elke reservatie afzonderlijk **Aansluiten** en **Afmelden**. De lijst toont de wijziging; er zijn maximaal vier spelers per reservatie, inclusief de organisator. In een server waar de bot is toegevoegd, verschijnt ook een korte openbare melding die na één uur automatisch verwijderd wordt. De verwijderwachtrij blijft bewaard bij herstarts. In groepschats en servers met alleen een user-install wordt alleen het oorspronkelijke bericht bijgewerkt, met een privébevestiging voor de deelnemer: Discord geeft de bot daar geen blijvende toegang en het antwoordtoken vervalt na vijftien minuten. Aan- en afmeldingen taggen niet opnieuw iedereen. Deelnemers wijzigen alleen de Discord-lijst. Alleen de eigenaar kan een KUL-reservatie maken, betalen of annuleren. Voor annuleren selecteer je één reservatie.

Is een reservatie al gedeeld in een andere chat, dan verschijnen **Ook hier delen** en **Verplaatsen naar hier** voor elke geselecteerde reservatie. Bij ook hier delen blijven de bestaande berichten actief; alle chats delen één deelnemerslijst met maximaal vier spelers. In andere groepschats wordt de getoonde lijst bij de volgende knopactie bijgewerkt. Verplaatsen bevestigt eerst het nieuwe bericht en verwijdert daarna de oude berichten. Als Discord een oud groepsbericht nog niet laat verwijderen, blijft dat bericht gesloten voor aanmelden en krijg je een link: klik daar op **Vernieuwen → Verwijder dit groepsbericht**. De organisator kan ook rechtstreeks een groepsbericht verwijderen met die knop. Dit verandert de KUL-reservatie of deelnemerslijst niet. Dubbelklikken maakt geen tweede actief bericht in dezelfde chat; bij een verloren Discord-antwoord moet je het mogelijk geplaatste bericht eerst vernieuwen.

Deelnemers worden lokaal in SQLite bewaard en de publieke knoppen werken opnieuw na een herstart. Een dubbele deelactie verwijst naar het bestaande groepsbericht. Bij annulering via `/bookings` sluit de deelnemerslijst; het groepsbericht wordt waar Discord dit toestaat meteen bijgewerkt, anders bij de volgende knopactie. Voor groepschats gebruikt de app een openbaar [interaction-followup](https://docs.discord.com/developers/interactions/receiving-and-responding#followup-messages); de app moet in die chat openbaar mogen antwoorden.

Bezette velden, overlappende eigen reservaties en blokkeringen op gedeelde middelen worden weggefilterd, ook als de slots-API `isAvailable=true` teruggeeft. Annuleren gebruikt het aankoop-annuleerpad van de sportwebsite en controleert daarna of de reservatie verdwenen is. Een gecombineerde aankoop moet via KU Leuven Sport geannuleerd worden, zodat andere onderdelen niet mee geannuleerd worden.

Een achtergebleven winkelwagenitem voor hetzelfde slot wordt bij bevestiging hergebruikt. Conflicterende, nog onbetaalde padelitems worden eerst gecontroleerd en gericht verwijderd; andere aankopen en bestaande reservaties blijven behouden. Na het aanmaken of annuleren van de reservatie ruimt de bot het bijbehorende winkelwagenitem op. Een onzekere netwerkuitkomst wordt eerst uitgelezen voordat een nieuwe schrijfopdracht mag starten.

Op 8 oktober 2026 zijn het hergebruiken van een achtergebleven item, het aanmaken van één reservatie en het gericht opruimen van dat item live bevestigd. Vervolgens is een kaartbetaling van €8 automatisch uitgevoerd en door KUL bevestigd: aankoopstatus afgerond en betalingsstatus geïnd. De eerdere onbetaalde test is geannuleerd.

De betalingscode bevat saldo, domiciliëring en een optionele Worldline-browserdriver. Die laatste vereist de `login` extra en Chromium op de machine die de betaling uitvoert. Volgens de [KU Leuven-handleiding](https://www.kuleuven.be/sport/onlinetool/handleiding) is een padelreservatie pas definitief na betaling.

Checkout gebruikt het vastgelegde `/sales/{id}/pay`-pad en de link van de server. De bot controleert eigenaar, bedrag en bestaande betalingen, en wacht na een betaalpoging op serverbevestiging. De Worldline-kaartvelden, moderne betaalknop en terugkeerknoppen zijn met nagebouwde pagina's in Chromium getest. De automatische kaartbetaling is op 8 oktober 2026 live bevestigd zonder handmatige bankgoedkeuring voor die transactie. Andere transacties kunnen een bankgoedkeuring vereisen.

Stel voor automatische kaartbetaling eenmalig je kaart lokaal in:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup-payment.ps1
```

Kaartnummer en CVC worden verborgen ingevoerd. De gegevens worden gevalideerd en versleuteld opgeslagen in `data/card.enc`, buiten Git. De actieve bot leest de kaart bij de volgende boeking; herstarten voor gewijzigde kaartgegevens is niet nodig. Tijdens een bankcontrole toont Discord dat de bot op het resultaat wacht. Een eventuele goedkeuring in de bank-app blijft nodig.

## Code en tests

```text
src/padelbot/auth.py          PKCE, login en gesynchroniseerde tokenvernieuwing
src/padelbot/idp.py           HTTP-formulieren, sessieopslag en MFA-push
src/padelbot/client.py        Delcom Backbone API
src/padelbot/booking.py       Slots, winkelwagen en reserveren
src/padelbot/payment.py       Betaling
src/padelbot/discord_bot.py   Discord-commando's en achtergrondtaken
src/padelbot/scheduler.py     Wekelijkse planning
src/padelbot/watcher.py       Polling voor vrijgekomen slots
src/padelbot/discord_ui.py    Keuzemenu's voor bekijken, boeken en automatisering
src/padelbot/availability_watch.py  Dagwatchers en wijzigingen in vrije tijden
src/padelbot/watch_ui.py      Watcher-beheer en privé-/publieke chatmeldingen
src/padelbot/weather.py       Gecachte Leuven-voorspelling
src/padelbot/web.py           Google-login, persoonlijke API en privébeheer
src/padelbot/web_runtime.py   Gescheiden workers en gebruikersscheduler
src/padelbot/web_device.py    Tijdelijke persoonlijke Android-koppeling
tests/                       Auth, beschikbaarheid, UI, annuleren en betalingen
```

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev,web]"
.\.venv\Scripts\python.exe -m pytest -q
```

De scheduler verwerkt alle regels die hetzelfde openingsmoment delen en controleert opnieuw of ze nog actief zijn voordat hij boekt. De boekingsopening staat voorlopig op middernacht, zeven dagen vooraf. Verifieer die tijd vóór automatische boekingen. Openingstijd en betaalbevestiging hebben nog live validatie nodig; zie de onderzoeksnotities voor concrete vervolgstappen.
