# Sigmadvisor Padel op de mini-pc

De webapp en Discord-bot draaien permanent in VM 141 (`sigma-padel-app`, Ubuntu 24.04, 1 GiB RAM). VM 140 (`sigma-padel-auth`, 4 GiB RAM) bewaart de afzonderlijke Android-toestellen en start uitsluitend wanneer native authenticatie nodig is. Beide staan in Proxmox-resourcepool `sigma-padel`; kies Pool View om ze samen te zien. De openbare URL is `https://padel.sigmadvisor.com`. Cloudflare Tunnel maakt uitsluitend de HTTP-app op `localhost:8090` bereikbaar; ADB, Android-console en Proxmox blijven privé.

De app bewaart tokens, cookies, profielmetadata, versleutelde PIN en de bestaande sleutels. Native Android-userdata blijft uitsluitend op de authenticator-VM. Een vast SSH-bridgeprogramma start deze VM via een apart, beperkt powercommando voor alleen VM 140. SSH gebruikt afzonderlijke sleutels, gecontroleerde hostkeys, vaste opdrachten en geen port forwarding. PIN en challenge gaan via de private stdin-verbinding, nooit via commandoregel of logs. De native werker gebruikt tijdelijk materiaal in `/run/padelbot` en synchroniseert Android-opslag voor afsluiten. Lease-heartbeats voorkomen afsluiten tijdens werk; de Proxmox-timer sluit de VM na een ongebruikte periode netjes af. Er is geen hard power-off-pad voor normale opruiming.

Beheer → Systeem toont vanaf de installatie echte metingen: HTTP-sessievernieuwing, MFA-aanmeldingen, emulatorstarts, looptijd, wachttijd en gemeten procesgeheugen. Registratie en technische probes tellen niet mee als natuurlijke heraanmeldfrequentie. Die frequentie wordt uitsluitend uit opeenvolgende geslaagde MFA-aanmeldingen van hetzelfde profiel afgeleid; onvoldoende data wordt expliciet getoond. Gegevens bevatten geen PIN, QR, tokens, cookies of providerfouttekst. De aparte telemetrydatabase bewaart maximaal 90 dagen; samenvattingen worden met beperkt geheugen gestreamd en daggrenzen volgen Europe/Brussels.

De wachtrij heeft twee leesplekken en één seriële plek voor authenticatie en wijzigingen. Taken voor hetzelfde profiel blijven op volgorde; deelnemen beschermt ook het organisatorprofiel. Een leesopdracht die MFA nodig heeft parkeert zijn bestaande wachtrijplek, meldt maximaal eenmaal opnieuw aan en hervat daarna. Schrijftaken met een onzekere uitkomst worden niet herhaald. De limieten van drie taken per gebruiker en honderd totaal omvatten ook geparkeerde taken. De scheduler ververst profiel- en regelgegevens per vijftien seconden en controleert openingsmomenten elke seconde; voorwaarden worden voor een echte reservering opnieuw gecontroleerd.

## Productiepilot: gedeeld dashboard en Discord

Het dashboard bevat beschikbaarheid en drie inklapbare delen: eigen boekingen, autobooking en instellingen. Vandaag tot en met dag zes worden in één aanvraag opgehaald; Vandaag is standaard geselecteerd. Dagfilters werken lokaal; Vernieuwen haalt hetzelfde volledige venster opnieuw op. Het resultatenpaneel reserveert zijn ruimte tijdens laden, lege resultaten en filterwissels. Matches heeft een eigen tab. Beheer is zichtbaar voor het Google-eigenaarsprofiel. De pagina toont een bevestigingsknop zolang de Tailscale-bevestiging ontbreekt of verlopen is; de gebruikerslijst en beheeracties blijven achter die controle. Beheer openen onder Instellingen opent dezelfde pagina. De KUL-wizard beperkt zich tot KUL openen, registratie-QR plakken/uploaden en tweemaal de PIN invullen. Er is geen toestelbeeld of webbediening.

Beheer bevat openklapbare gebruikers en Discord-gasten. Details tonen de Discord-koppeling, bekende serverinstallaties en waargenomen persoonlijk gebruik, gecontroleerde komende boekingen, actieve autobookingregels, betaalmethodestatus en deelnames bij anderen. Serverinformatie beschrijft bekende installaties en eerder gebruik; zij bevestigt geen huidig serverlidmaatschap. Een gekoppeld Discord-profiel levert de profielfoto; anders wordt de Google-foto gebruikt. Discord-gasten hebben een afzonderlijk icoon en het label Niet geregistreerd, zonder fictief Google-account. Boekings- en betaalstatus komen uit de laatste controles en starten geen emulator bij het openen van Beheer. Beheer krijgt nooit kaartnummers, PIN, tokens of privébetaallinks.

Beperken geeft leestoegang en blokkeert nieuwe boekingen, autobooking, drafts, delen en deelnemen. Stoppen en opruimen blijven mogelijk. Bannen trekt sessies in en blokkeert toegang. Moderatie geldt ook voor Discord-gasten en blijft gelden wanneer zij later hun profiel koppelen; bestaande betaalde KUL-reserveringen worden niet geannuleerd. De eigenaar kan niet worden geblokkeerd. Elke beheeractie vereist opnieuw de servercontrole op eigenaar en geldige Tailscale-bevestiging.

De registratie en versleutelde PIN gebruiken vaste profielpaden, ook in nieuwe processen en na updates. Een geregistreerd toestel wordt nooit stilzwijgend vervangen als Android-userdata ontbreekt. Opslag wordt gesynchroniseerd voor afsluiten. De wizard toont een geanimeerd vinkje en Gekoppeld na een succesvolle KUL-clientcontrole; de profielstatus vernieuwt meteen zonder F5. Een bestaande geverifieerde koppeling openen start geen emulator. Alleen wanneer de KUL-sessie ontbreekt, wordt de bestaande registratie voor aanmelden gebruikt. Een mislukte PIN-poging blijft beveiligd tegen automatisch herhalen.

`PlatformService` koppelt beide frontends aan dezelfde profielen, betaalkeuze en tenant-wachtrij. De bestaande regelbestanden en canonieke `shared_matches`/`match_players` blijven behouden. Ook de eigenaar wordt door de webworker ingepland. In platformmodus starten de oude Discord-scheduler, automatische watcher en periodieke eigenaar-keepalive/live-overzichten/herinneringen niet, en oude boekingspanelen verwijzen naar de gedeelde commando's. Er wordt nooit geboekt op het account van de eigenaar voor een andere Discord-gebruiker.

Discord gebruikt de [officiële authorization-code-flow](https://docs.discord.com/developers/topics/oauth2): `identify bot applications.commands`, een tien minuten geldige, eenmalige staat gebonden aan de Google-sessie, en callback `/auth/discord/callback`. Persoonlijke installatie gebruikt `identify applications.commands`. Het Discord-ID komt uitsluitend uit de geauthenticeerde `/users/@me`; het toegangstoken wordt niet bewaard. De serverinstallatie vraagt View Channels, Send Messages, Embed Links en Read Message History (84992), geen Administrator. Stel `PADELWEB_DISCORD_CLIENT_ID` en `PADELWEB_DISCORD_CLIENT_SECRET` in `data/platform/web.env` in, modus 0600. De bot gebruikt `PADELBOT_PLATFORM_ROOT=/opt/padelbot/data/platform` in zijn private `.env`.

Betaalkeuze `kul` gebruikt voldoende KUL-saldo of een door KUL geregistreerde creditcard. De implementatie volgt de publieke USC-frontend: `/stored-pay-methods` gefilterd op `personId`, een geldige voorkeurskaart en `/sales/{id}/pay` met `storedPayMethodId` en de bestaande Ogone-provider. Alleen eigen, niet-verlopen ondersteunde creditcards worden gebruikt; het token wordt vóór betalen opnieuw op eigendom gecontroleerd. Kaartnummer en CVC worden niet voor deze koppeling opgevraagd. De aparte versleutelde kaartkluis blijft los van een KUL-betaalmachtiging. Een succesvolle betaalproviderresponse is onvoldoende: de eigen KUL-sale moet de betaling bevestigen. Gedeeltelijke saldobetaling wordt niet verondersteld: het saldo moet de volledige prijs dekken.

De compatibele voorkeurswaarde `discord` betekent nu een betaallink op de website én optioneel via Discord. Voldoende saldo wordt eerst gebruikt; anders volgt de vastgelegde creditcardcheckout. De website vereist geen Discord-koppeling of bereikbare privéberichten. Checkoutlinks staan afzonderlijk met Fernet versleuteld in de database, gebonden aan het profiel en de verkoop. Alleen de ingelogde boeker kan `/api/payments` opvragen; beheer en andere gebruikers krijgen de link niet. Links worden maximaal één uur getoond en verdwijnen na bevestigde betaling. Jobresultaten en logs bevatten geen checkouttoken. De aparte verzendwachtrij bindt inhoud ook aan de Discord-ontvanger en wist die na aflevering. De sleutel `data/platform/discord-delivery.key` staat afzonderlijk van de database, modus 0600; neem hem mee in de private serverback-up. Onderbroken schrijftaken en onzekere betalingen worden niet herhaald.

`/bookings` en `/deel` delen een opnieuw via het eigen KUL-profiel gecontroleerde boeking in de gekozen Discord-chat. De website biedt een uitnodigingslink `/#match/ID`; deze verwijst na Google-login naar dezelfde canonieke match en spelerslijst. Boekingsmeldingen en herinneringen gebruiken een ontdubbelde wachtrij. Voor een herinnering wordt de eigen boeking kort voor de start opnieuw gecontroleerd; de globale eigenaar-keepalive blijft uitgeschakeld.

De eerste gebruikers moeten echte KUL-registratie, Discord-autorisatie en een zelf bevestigde betaling nog doorlopen. Geautomatiseerde controles gebruiken geïsoleerde databases en gesimuleerde providerresultaten; er worden daarvoor geen betaalde testboekingen of berichten naar echte gebruikers gemaakt.

## Koppelingen en automatische controle

Instellingen gebruikt het blauwe KU Leuven Authenticator-appicoon. Discord heeft twee zichtbare opties: bot toevoegen aan een server en aan het persoonlijke profiel toevoegen. De informatieknoppen werken via hover, toetsenbordfocus en tikken op gsm. Groen gekoppeld verschijnt pas bij een geldige status; foutfeedback staat bij de actie en blijft ook onderaan zichtbaar.

`POST /api/account/check` hergebruikt een lopende eigen account/login-taak. Een geslaagde controle wordt vijf minuten hergebruikt; mislukte controles hebben een afkoeltijd. `GET /api/account/status` leest alleen de status. Er worden geen controles gestart zolang het toestel gekoppeld wordt of bij een vastgelegde authenticatorfout. Terugkomen van de KUL-kaartpagina vraagt een begrensde nieuwe controle. `/api/history` bundelt herhaalde fouten en technische leescontroles tot een regel met het laatste tijdstip; de oorspronkelijke taken en audit blijven bewaard.

Autobooking vereist betaalwijze `kul` en een recent bevestigde eigen KUL-creditcard. De scheduler gebruikt de bewaarde koppelstatus, zodat de website niet open hoeft te blijven. De worker haalt voor de echte boeking de eigen betaalmethodes opnieuw op; ontbrekende, verlopen of ingetrokken kaarten stoppen voor de winkelwagenwrite, ook als er voldoende saldo is. Handmatig boeken met saldo of een betaallink blijft beschikbaar.

## Uitgevoerd plan

1. Een responsieve webapp met het bestaande Sigmadvisor-logo, Google-login, beschikbaarheid (onder meer dag 0 t/m 5), boekingen, wekelijkse regels en instellingen.
2. Lokale profielen met een vaste Google-subjectidentiteit, server-side sessies en gescheiden KUL-token-, sleutel-, PIN-, regel- en Android-bestanden.
3. Eén gedeelde emulatorwerkplek met een procesoverschrijdende lock. Elk nieuw profiel krijgt verse Android-userdata, nooit een kopie van de geregistreerde eigenaar. De officiële SDK/system image wordt gedeeld. Registratie gebeurt via KUL-accountbeheer, met QR-upload of plakken, zonder toestelweergave of bediening via de website. Toestellen sluiten na koppeling, 2 minuten zonder bediening, of maximaal 10 minuten.
4. Een beperkte achtergrondwachtrij met voorrang voor geplande boekingen, een prijsmaximum en controle van saldo/gekoppelde creditcard vóór automatische betaling. Onderbroken schrijftaken worden niet automatisch herhaald. Boekingen vanuit Discord en web delen een accountgebonden transactielock.
5. Beheer voor het bestaande eigenaarsprofiel, na Google-login én een tijdelijke bevestiging door Tailscale Serve. Het beheer toont profielen/status en kan profielen pauzeren. Het geeft geen toegang tot PIN's, tokens, betaalgegevens of Android-schermen van andere gebruikers.

## Diensten en bestanden

- `padelbot.service`: bestaande Discord-bot, met bestaande eigenaar en regels.
- `padelweb.service`: FastAPI, dezelfde Python-omgeving, servicegebruiker `padelbot`, poort 8090.
- `padel-cloudflared.service`: Cloudflare-connector met root-only tokenbestand `/etc/cloudflared/padel.token`.
- Proxmox-host: `padel-tail-proxy.service`, uitsluitend `127.0.0.1:8191`; Tailscale Serve op HTTPS-poort 8443. Geen Funnel.
- `/opt/padelbot/data/platform/web.env`: Google-clientgegevens, canonieke URL, eigenaar en geheim voor de private Tailscale-brug. Modus 0600, nooit in Git.
- `/opt/padelbot/data/platform/platform.sqlite3`: gebruikers, sessiehashes, eenmalige OAuth-/beheerstaten, taken en auditregels.
- `/opt/padelbot/data/platform/profiles/<random-id>/`: persoonlijke gegevens; nieuwe profielen krijgen elk een eigen versleutelsleutel. Het bestaande eigenaarsprofiel verwijst naar de eerder gemigreerde botbestanden.

De web-VM heeft 1 GiB RAM. De aparte authenticator-VM heeft tijdens gebruik 4 GiB beschikbaar voor Android en zijn overhead; uitgeschakeld gebruikt zij geen guest-RAM. Er komt geen VM per gebruiker: alleen de eigen Android-userdata wordt per profiel bewaard, naast de gedeelde SDK en system image. Verwijder of vervang die userdata nooit tijdens een code-update.

De native werker initialiseert de geregistreerde app voordat de IdP een tijdelijke aanmeldvraag maakt. Dezelfde app blijft voor die ene vraag actief. Een mislukte of onzekere PIN-poging wordt nooit automatisch herhaald. Vóór een geplande openingsronde kan de scheduler de sessie maximaal eenmaal per profiel voorbereiden; daarvoor worden opnieuw de actuele boekings- en betaalvoorwaarden gecontroleerd.

De getoonde emulatorwachttijd is alleen de wachttijd op de native lock; taakwachttijd en VM-start horen daar niet bij. De RAM-piek betreft het emulatorproces, exclusief guest-OS en hypervisor. Gebruik looptijd, taakvertraging en waargenomen MFA-frequentie samen voor een capaciteitsbeslissing; een aantal geregistreerde profielen is geen aantal gelijktijdig draaiende emulators.

## KUL koppelen

Instellingen bevat een wizard met drie stappen: KUL-accountbeheer openen, de registratie-QR van een nieuw toestel toevoegen (afbeelding kiezen of vanuit het klembord plakken), en een PIN kiezen. Een registratielink is een alternatief. De gewone QR voor aanmelden maakt geen nieuw toestel; bij een herkenbaar aanmeldscherm vraagt de wizard om een registratie-QR.

Bij **Toestel toevoegen** kan de gebruiker de QR met Win+Shift+S knippen en met Ctrl+V plakken, of **Afbeelding uploaden** kiezen. Dit werkt ook terwijl het toestel start: maximaal één gekozen afbeelding blijft tijdelijk in het browsergeheugen en wordt na het opstarten eenmaal verstuurd. Sluiten wist die wachtrij. Alleen het kruis of **Sluiten** sluit de wizard; een klik erbuiten, Escape of een geslaagde login sluit hem niet vanzelf.

Een zichtbare preview bevestigt meteen welke afbeelding is toegevoegd en blijft staan bij een fout. Verwijderen annuleert een nog niet verstuurde afbeelding; Sluiten en opnieuw starten wissen ook de preview en de tijdelijke afbeeldingsinhoud. Afbeeldingen zijn maximaal 8 MB en 16 megapixels. De server zoekt in de volledige screenshot en zo nodig in overlappende uitsneden, met ondersteuning voor rotatie, EXIF-oriëntatie en extra inhoud rondom de QR. Downloadcodes en andere tekstlinks worden overgeslagen wanneer één KUL-code aanwezig is. Verschillende KUL-codes blijven ambigu en worden geweigerd; herhaalde weergaven van dezelfde code zijn toegestaan. Een download-QR alleen geeft de instructie om bij KUL op Volgende door te gaan tot de registratie-QR. Een afgewezen afbeelding stopt de koppelsessie niet.

Een uitgeschakelde knop toont geen laadcursor. Na het versturen van de QR kan de gebruiker de PIN invullen en de knop gebruiken; de webapp controleert de actuele toestelfase opnieuw voordat hij een PIN verstuurt. Een onbekende fase geeft uitleg en voert geen PIN in. Een verlopen sessie toont de fout en de herstartknop meteen in beeld. Toestelaanvragen hebben een eindige wachttijd en herstellen hun knopstatus bij een fout. De KUL-login wordt maximaal twee minuten gevolgd; daarna kan **Controle vernieuwen** dezelfde taak ophalen zonder een nieuwe login of PIN-poging te starten. Laat binnenkomende antwoorden van een gesloten of vervangen sessie worden genegeerd.

De decoder bewaart geen afbeeldingen en ondersteunt zowel officiële KUL-links als de binaire nextAuth-QR. Voor die laatste worden de oorspronkelijke bytes volgens de [nextAuth-specificatie](https://docs.nextauth.com/server-sdk/login/app-to-app.html) naar ongepaddede base64url onder de vaste officiële KUL-app-URL omgezet. Verkeerde tekstlinks blijven geweigerd. De app controleert de registratie; een leesbare QR alleen is geen bewijs dat de koppeling geslaagd is. Ontbrekende, meerdere en ongeschikte codes krijgen afzonderlijke foutmeldingen.

Het toestel start via een korte POST en een persoonlijke status-API. Andere gebruikers kunnen de sessie niet overnemen. De PIN en bevestiging staan in hetzelfde formulier met afzonderlijke oogknoppen. Alleen twee gelijke viercijferige waarden worden verstuurd. De bekende nieuwe-PIN-schermen worden eenmaal ingevuld; bij de generieke Create new account-variant volgt maximaal eenmaal de expliciet bevestigde tweede invoer. Een fout, onbekend scherm, loginverzoek of gedeeltelijke invoer blokkeert verder invoeren. Er is geen automatische PIN-herhaling na een time-out. De PIN-foutblokkering blijft behouden.

Een accountlabel in een registratie-activiteit bewijst geen voltooide registratie. Het account moet op het beginscherm aanwezig zijn en een app-herstart overleven voordat het profiel en de versleutelde PIN worden bewaard. Android-opslag wordt voor afsluiten gesynchroniseerd. Daarna toont de wizard direct **Gekoppeld**; de aparte logincontrole loopt op de achtergrond en meldt een eventuele fout afzonderlijk. De wizard blijft open tot Sluiten of het kruis. Er zijn geen publieke scherm-, tik- of terugacties; de API weigert die acties.

De herkenbare toestelnaam is `padelreservering-auth` in de wizard, het profiel, de Android-toestelinstelling en de AVD-weergavenaam. De Google-fabrikant, unieke toestelidentiteit en geregistreerde sleutels blijven behouden.

Auditregels bevatten alleen de fase (start aangevraagd, toestel klaar/mislukt, registratie verzonden, PIN-setup en profiel bewaard), zonder QR-link, PIN of tokens. De PIN wordt na verzenden uit het formulier gewist. Een gebruiker moet de officiele KUL-registratie nog zelf bevestigen met het bestaande toestel; de website verzamelt geen KUL-wachtwoord.

## Google en domein

Maak een OAuth-client van type **Web application**, met redirect-URI:

```
https://padel.sigmadvisor.com/auth/google/callback
```

Gebruik alleen `openid email profile`. De server controleert onder meer handtekening, issuer, audience, expiry, nonce, geverifieerd e-mailadres, PKCE en een eenmalige, browsergebonden state. Alleen de Google-subject-ID koppelt terugkerende gebruikers. In Google-testmodus moeten bezoekers bij de testgebruikers staan.

Cloudflare Published application route:

```
padel.sigmadvisor.com -> HTTP localhost:8090
```

De connector heeft daarnaast een lokale hostname-regel in `/etc/cloudflared/padel.yml` (bron: `deploy/padel-cloudflared.yml`), zodat de juiste origin ook beschikbaar is wanneer de dashboardconfiguratie nog niet is aangekomen. Andere hostnames krijgen 404. De service gebruikt uitsluitend het private tokenbestand; er staan geen tunnelcredentials in de broncode.

Het domein heeft ook een proxied DNS-record nodig: type **CNAME**, naam **padel**, doel **1577ae18-08ed-43fe-a973-13021c76beab.cfargotunnel.com**, TTL **Auto**. Een opgeslagen tunnelroute zonder dit DNS-record maakt de URL nog niet bereikbaar. Controleer de configuratie met `cloudflared tunnel --config /etc/cloudflared/padel.yml ingress validate` en de hostmatch met `cloudflared tunnel --config /etc/cloudflared/padel.yml ingress rule https://padel.sigmadvisor.com`.

De beheerder meldt aan met `aafmichiels@gmail.com`. De knop Beheer leidt na Google-login naar `https://sigma-minipc.tailad80cf.ts.net:8443/admin/verify` met een kort geldig ticket. De app accepteert Tailscale-identiteitsheaders uitsluitend vanaf het vaste Proxmox-adres én met het geheime brugkenmerk. Het ticket is gekoppeld aan dezelfde Google-sessie, eenmalig bruikbaar, en verleent 30 minuten beheer. Publiek toegevoegde headers leveren geen beheerrechten op.

De desktop heeft in Tailscale `tag:admin` en daardoor geen menselijke identiteitsheader. De private brug verifieert daarom ook de door Serve overschreven bron-IP via de lokale Tailscale-daemon. Alleen de expliciet vastgelegde stabiele node-ID van deze desktop, met `tag:admin`, mag de eigenaar bevestigen. Andere apparaten met dezelfde tag krijgen hierdoor geen rechten. Google-login als de eigenaar blijft vereist.

## Betalingen en bestaande Discord-regels

Nieuwe webgebruikers kiezen een betaalwijze en stellen voor autobooking expliciet hun maximumbedrag in. De webworker gebruikt het eigen voldoende KUL-saldo, een geldige gekoppelde creditcard of een persoonlijke betaallink. Beheer van de KUL-kaart gebeurt via de officiële KUL-pagina:

https://usc.kuleuven.cloud/members/profile/myshopping#storedpaymethodcard

De nieuwe gebruikersscheduler volgt de gecontroleerde API-grens van 168 uur vóór de start, berekend in UTC zodat ook een zomer-/wintertijdwissel klopt. Als er nog geen terrein vrij is, controleert hij de wekelijkse regel om de twee minuten opnieuw binnen het opgegeven startvenster. Alleen een uitdrukkelijk leesresultaat zonder reservatie mag opnieuw gecontroleerd worden; een uitgevoerde of onzekere schrijftaak behoudt zijn ontdubbelingssleutel.

Eerder bewaarde kaarten blijven in een persoonlijke kaartkluis: maximaal vijf kaarten per gebruiker, een standaardkaart selecteren en per kaart verwijderen. Kaartnummer, kaarthouder en vervaldatum worden vóór een database-write met AES-256-GCM versleuteld. Elke nieuwe kaart gebruikt een nieuwe willekeurige datasleutel en nonces; de datasleutel wordt zelf versleuteld. Authenticatie van de ciphertext bindt beide lagen aan profiel en kaart-ID. De API geeft uitsluitend kaart-ID, kaartnaam, merk, laatste vier cijfers, vervaldatum en standaardkeuze terug. Onbekende velden (waaronder CVC/CVV) worden geweigerd en validatiefouten echoën nooit de aanvraag. Auditregels bevatten alleen de actie, geen kaartgegevens. Er is geen endpoint dat het volledige kaartnummer teruggeeft en beheer toont geen kaarten.

Nieuwe kaarten worden uitsluitend via de bestaande KUL-betaalpagina gekoppeld. Het eigen formulier voor kaartnummer en CVC is verwijderd; `POST /api/payment-card` retourneert 410 zonder kaartdata te verwerken of op te slaan. De mini-pc bewaart voor deze koppeling alleen betaalstatus; de eigen KUL-betaalmethodes worden bij elke boeking opnieuw gelezen. Bestaande versleutelde kaarten blijven alleen gemaskeerd raadpleegbaar, als standaard te kiezen en te verwijderen. Zij geven geen KUL-betaalmachtiging. Kaartnamen met dertien of meer cijfers worden geweigerd en oude onveilige labels worden bij uitlezen afgeschermd.

De aparte hoofdsleutel is een bestand van 32 willekeurige bytes op `/etc/padelbot/card-vault.key`, map 0700 root/root en bestand 0600 root/root. `LoadCredential` geeft de webservice een private, alleen-lezen kopie; de sleutel staat niet in Git, de database, bronarchieven of worker-environment. Core dumps staan voor de webservice uit. Maak deze sleutel bij installatie één keer met een cryptografische randombron; vervang een bestaande sleutel nooit automatisch. Een ontbrekende of verkeerde sleutel blokkeert de kluis. Bewaar een aparte private back-up van deze sleutel als herstel vereist is. Rotatie vereist het opnieuw verpakken van alle datasleutels en een consistente database-back-up. Verwijderen wist de actieve rij; bestaande back-ups volgen hun eigen bewaartermijn.

Deze versleuteling beschermt een losse databasekopie; zij geeft geen bescherming tegen een volledig gecompromitteerde webserver/root of een VM-back-up die ook de sleutel bevat, en is geen PCI-DSS-certificering. HTTPS beschermt het transport; de TLS-terminatie bij Cloudflare blijft onderdeel van het vertrouwde pad. Voor het ontwerp zijn de [OWASP-richtlijnen voor cryptografische opslag](https://cheatsheetseries.owasp.org/cheatsheets/Cryptographic_Storage_Cheat_Sheet.html) gevolgd. De webapp vraagt of bewaart geen CVC: die mag ook voor terugkerende betalingen niet na autorisatie worden bewaard ([PCI SSC](https://www.pcisecuritystandards.org/faqs/1280/)). De bestaande Revolut-kaart van de eigenaar wordt eenmalig expliciet overgenomen: uitsluitend nummer, kaarthouder en vervaldatum, zonder CVC. Een transactiemarker voorkomt dubbele import bij herstart. Het bestaande Discord-betaalbestand blijft apart.

De losse kaartkluis geeft geen betaalmachtiging. Voor automatisch betalen wordt de echte, eigen KUL-koppeling gebruikt: saldo, domiciliëring of een niet-verlopen ondersteunde opgeslagen betaalmethode bij KUL. De methode wordt voor de betaling opnieuw gecontroleerd. Een vereiste bankcontrole gaat via Discord; de provider of bank bevestigt de betaling.

In platformmodus gebruiken Discord en website dezelfde eigenaarsregels, betaalkeuze en limiet. De webworker plant alle profielen in; de oude Discord-scheduler en automatische watcher worden niet daarnaast gestart. Standalone installaties zonder platformroot behouden hun eigen scheduler.

## Testen, bijwerken en herstel

Installeer `pip install -e '.[login,android,web,dev]'`. Tests gebruiken uitsluitend tijdelijke lokale profielen; er wordt niet echt geboekt of betaald. Voer op de VM tests uit als servicegebruiker vanuit `/opt/padelbot`, bijvoorbeeld:

```
sudo -u padelbot .venv/bin/python -m pytest -q -p no:cacheprovider
```

Uitrollen gebeurt met een bronarchief zonder `.env`, `data/`, SDK, virtuele omgeving of credentials. Stop/herstart diensten gecontroleerd; overschrijf persoonlijke bestanden niet. Een onderbroken taak krijgt status `unknown` en moet eerst via KU Leuven Sport gecontroleerd worden. Een mislukte authenticator-PIN blijft geblokkeerd; reset die uitsluitend nadat het eigen toestel handmatig gecontroleerd is.

Back-ups moeten zowel gebruikersdata als de bijbehorende afzonderlijke sleutelbestanden bevatten en privé blijven. Een Proxmox-snapshot is op dezelfde schijf geen onafhankelijke back-up. Voor een consistente Android-back-up moeten alle emulators afgesloten zijn. Bewaar relatieve qcow2-backingreferenties bij een verhuizing.

Op 14 oktober 2026 zijn de publieke DNS-koppeling, HTTPS, HTTP-naar-HTTPS-redirect, pagina en assets op desktop/mobiel en de Google-doorverwijzing gecontroleerd. Een echte Google-aanmelding is via de publieke callback voltooid en aan het bestaande eigenaarsprofiel gekoppeld. Niet-aangemelde bezoekers krijgen geen profiel- of beheergegevens; ongeldige OAuth-state en writes vanaf een andere origin worden geweigerd.

De eigenaar heeft de Google-login en Beheer-pagina bevestigd; een echte Tailscale-beheerbevestiging staat in het auditlog. Alleen dit Google-eigenaarsprofiel ziet de beheernavigatie. Andere profielen worden ook bij `#admin` naar Beschikbaarheid gestuurd en alle beheer-API's weigeren toegang.

## Online boekingen

Bij Mijn boekingen haalt de bestaande KUL-client uitsluitend de eigen reservaties op. Een boeking wordt alleen deelbaar als de geregistreerde authenticator aanwezig is, de client is aangemeld, het product een padelterrein is, status 0 actief is, betaling expliciet bevestigd is en de start nog in de toekomst ligt. De boeker kiest **Online zetten**; persoonlijke boekingen van nieuwe gebruikers worden niet vanzelf gedeeld. Alleen de boeker kan dit ongedaan maken.

Online boekingen toont gedeelde reserveringen die minder dan tien minuten geleden gecontroleerd zijn, met vrije plaatsen (of de eigen deelname), maximaal vier spelers inclusief organisator. Vernieuwen vraagt begrensde, ontdubbelde clientcontroles aan. Bij Deelnemen start een geïsoleerde leestaak met het profiel van de organisator, controleert opnieuw de eigen KUL-reservaties en voegt daarna de aangemelde deelnemer atomair toe. Deelnemers worden nooit door browser-ID's gekozen. Annulering, onbetaalde status, pauzeren of offline zetten blokkeert deelname. Bij een gewijzigd tijdstip vervallen deelname en publicatie, zodat niemand ongemerkt naar een andere match verhuist.

Discord en website gebruiken dezelfde `shared_matches` en `match_players` in `data/rules.sqlite3`. Aantallen en de capaciteit van vier spelers zijn dus atomair gedeeld. Bestaande matches en bericht-ID's blijven behouden. De eigenaar wordt via de bestaande vaste Discord-owner-ID gekoppeld; andere gebruikers koppelen hun eigen Discord-account met een eenmalige code uit Instellingen en `/koppel CODE` in Discord (tien minuten geldig, alleen gehasht opgeslagen). Koppelen voegt dubbele web/Discord-deelnames samen. Reeds geposte serverberichten worden bijgewerkt zonder nieuwe groepsberichten of pings; een user-installed groeps-DM leest de actuele lijst via zijn Vernieuwen-knop.

De KUL-reservering blijft op naam van de organisator. Deelname maakt geen nieuwe KUL-boeking of betaling. Gedeelde velden bevatten alleen terrein, tijd, voornamen en aantallen, geen e-mails, KUL-reservatienummers of accountgegevens. `paidFor` is op zichzelf onvoldoende bewijs: ook de eigen verkoop moet statusCode 0 hebben. Online boekingen opent met een begrensde KUL-controle; de zichtbare lijst wordt periodiek bijgewerkt. Alleen de eigenaar ziet Beheer.

## Drafts

Vanuit een prijsvoorstel kan de organisator een onbetaald voorstel aanmaken. Een draft telt de organisator als eerste van maximaal vier spelers; deelname is interesse, zonder betaalopdracht. De worker controleert het exacte eigen winkelwagenitem elke 45 minuten. Hij hergebruikt een bevestigd passend item en maakt uitsluitend een normaal `/cart-items`-item aan wanneer nodig en nog beschikbaar. Andere winkelwagenitems worden nooit vervangen of verwijderd voor een draft. Een verloren schrijfantwoord wordt eerst met een read gecontroleerd, nooit blind herhaald.

De looptijd vanaf aanmaak is maximaal 24 uur bij meer dan 48 uur voor aanvang, 6 uur tussen 16 en 48 uur, en 45 minuten binnen 16 uur. Looptijden zijn veelvouden van 45 minuten. Het bereiken van een strengere grens verkort de oorspronkelijke looptijd en kan die direct be?indigen; stoppen/opnieuw aanmaken of een andere tijdzone start de limiet voor hetzelfde slot niet opnieuw. Maximaal drie actieve drafts per profiel. Verlopen, pauzeren en annuleren verbergen de draft en ruimen uitsluitend het eigen gelabelde, onbetaalde item op. Opruimen kent maximaal drie automatische pogingen, met vijf minuten ertussen. Bij een onbekende uitkomst wordt de draft verborgen en krijgt de eigenaar een melding om KUL te controleren.

Een winkelwagenitem geeft **geen garantie op een gereserveerd terrein**. Volgens de [KUL-reservatievoorwaarden](https://www.kuleuven.be/sport/accommodatie/reservaties/padel) is een reservatie pas definitief na betaling. Draftcontroles cre?ren geen `/sales` of betaalopdracht en omzeilen geen backend-vervaltijd. Alleen de organisator kan via een nieuwe prijscontrole en expliciete bevestiging boeken en betalen, met behoud van de spelerslijst. Bij een onbekende betaaluitkomst of serverherstart volgt geen automatische nieuwe betaalpoging. Webbetalingen gebruiken dezelfde betaalkeuze als Discord; de losse standaardkaart in de kluis geeft geen KUL-betaalmachtiging.

De telefooninterface heeft vaste navigatie onderaan, ruimte voor de safe area, grote bedieningsknoppen en invoervelden van 16 pixels. Desktop en telefoon gebruiken dezelfde rechtencontroles.

Nog door een echte gebruiker te bevestigen: registratie van een nieuw eigen KUL-account en een expliciet gekozen echte boeking/betaling. Testboekingen die geld kosten worden niet als deploymentcontrole aangemaakt.

De uitbreiding is getest met 509 volledige Linux-tests (25 Windows-tests overgeslagen), 149 gerichte Windows-tests, en aparte browsercontroles op desktop, telefoons van 320/360/390/430 pixels en een liggend scherm van 844 pixels. De browsercontroles gebruiken een tijdelijke lokale database, testkaartnummers en gesimuleerde KUL-resultaten; zij zetten geen kaarten of voorbeeldmatches in productie. De echte KUL-clientcontrole voor de eigenaar gaf nul komende reservaties terug.
