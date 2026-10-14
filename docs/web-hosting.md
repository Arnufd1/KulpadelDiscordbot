# Sigmadvisor Padel op de mini-pc

De webapp draait naast de bestaande Discord-bot in VM 140 (`padelbot`, Ubuntu 24.04). De openbare URL is `https://padel.sigmadvisor.com`. Cloudflare Tunnel maakt uitsluitend de HTTP-app op `localhost:8090` bereikbaar; ADB, Android-console en Proxmox blijven privé.

## Uitgevoerd plan

1. Een responsieve webapp met het bestaande Sigmadvisor-logo, Google-login, beschikbaarheid (onder meer dag 0 t/m 5), boekingen, wekelijkse regels en instellingen.
2. Lokale profielen met een vaste Google-subjectidentiteit, server-side sessies en gescheiden KUL-token-, sleutel-, PIN-, regel- en Android-bestanden.
3. Eén gedeelde emulatorwerkplek met een procesoverschrijdende lock. Elk nieuw profiel krijgt verse Android-userdata, nooit een kopie van de geregistreerde eigenaar. De officiële SDK/system image wordt gedeeld. Registratie gebeurt via KUL-accountbeheer, met een private schermbediening en QR-upload. Toestellen sluiten na koppeling, 2 minuten zonder bediening, of maximaal 10 minuten.
4. Een beperkte achtergrondwachtrij met voorrang voor geplande boekingen, een prijsmaximum en controle van saldo/domiciliëring vóór een reservatie. Onderbroken schrijftaken worden niet automatisch herhaald. Boekingen vanuit Discord en web delen een accountgebonden transactielock.
5. Beheer voor het bestaande eigenaarsprofiel, na Google-login én een tijdelijke bevestiging door Tailscale Serve. Het beheer toont profielen/status en kan profielen pauzeren. Het geeft geen toegang tot PIN's, tokens, betaalgegevens of Android-schermen van andere gebruikers.

## Diensten en bestanden

- `padelbot.service`: bestaande Discord-bot, met bestaande eigenaar en regels.
- `padelweb.service`: FastAPI, dezelfde Python-omgeving, servicegebruiker `padelbot`, poort 8090.
- `padel-cloudflared.service`: Cloudflare-connector met root-only tokenbestand `/etc/cloudflared/padel.token`.
- Proxmox-host: `padel-tail-proxy.service`, uitsluitend `127.0.0.1:8191`; Tailscale Serve op HTTPS-poort 8443. Geen Funnel.
- `/opt/padelbot/data/platform/web.env`: Google-clientgegevens, canonieke URL, eigenaar en geheim voor de private Tailscale-brug. Modus 0600, nooit in Git.
- `/opt/padelbot/data/platform/platform.sqlite3`: gebruikers, sessiehashes, eenmalige OAuth-/beheerstaten, taken en auditregels.
- `/opt/padelbot/data/platform/profiles/<random-id>/`: persoonlijke gegevens; nieuwe profielen krijgen elk een eigen versleutelsleutel. Het bestaande eigenaarsprofiel verwijst naar de eerder gemigreerde botbestanden.

De VM heeft 4 GB beschikbaar guestgeheugen voor korte Android-pieken. Free-page-reporting en het vrijgeven van Android-bestandscache laten ongebruikt fysiek hostgeheugen terugkomen. Er staat geen emulator permanent aan en er komt geen VM per gebruiker.

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

Nieuwe webgebruikers stellen expliciet automatisch betalen en hun maximumbedrag per boeking in. De webworker gebruikt uitsluitend het eigen KUL-saldo of een bestaande geldige domiciliëring. Beheer daarvan gebeurt via de officiële KUL-pagina:

De nieuwe gebruikersscheduler volgt de gecontroleerde API-grens van 168 uur vóór de start, berekend in UTC zodat ook een zomer-/wintertijdwissel klopt. Als er nog geen terrein vrij is, controleert hij de wekelijkse regel om de twee minuten opnieuw binnen het opgegeven startvenster. Alleen een uitdrukkelijk leesresultaat zonder reservatie mag opnieuw gecontroleerd worden; een uitgevoerde of onzekere schrijftaak behoudt zijn ontdubbelingssleutel.

https://usc.kuleuven.cloud/members/profile/myshopping#storedpaymethodcard

De webapp neemt geen kaartnummer of CVC aan en leest ook de eerder opgeslagen kaart van de eigenaar niet. Een opgeslagen-kaartbetaling voor nieuwe gebruikers is **niet geïmplementeerd**. De gecontroleerde USC-frontend gebruikt Ogone; de frontend biedt nieuwe opgeslagen betaalmethoden bij andere providerpaden en eventueel legacy-domiciliëring. Een algemene Worldline-tokenintegratie is daardoor niet zonder meer beschikbaar voor dit KUL-account. Een kaart bij Sigmadvisor bewaren is geen vervanging voor een ondersteunde token-/mandaatkoppeling.

De bestaande Discord-bot behoudt zijn bestaande regel- en betaalinstellingen. De webapp kan dezelfde eigenaarsregels bekijken, activeren, pauzeren en verwijderen. Het webbetalingsvinkje en prijsmaximum gelden voor webboekingen en de nieuwe gebruikersscheduler; zij wijzigen de bestaande Discord-betaalconfiguratie niet. Er is geen tweede scheduler voor de eigenaar in de webapp.

## Testen, bijwerken en herstel

Installeer `pip install -e '.[login,android,web,dev]'`. Tests gebruiken uitsluitend tijdelijke lokale profielen; er wordt niet echt geboekt of betaald. Voer op de VM tests uit als servicegebruiker vanuit `/opt/padelbot`, bijvoorbeeld:

```
sudo -u padelbot .venv/bin/python -m pytest -q -p no:cacheprovider
```

Uitrollen gebeurt met een bronarchief zonder `.env`, `data/`, SDK, virtuele omgeving of credentials. Stop/herstart diensten gecontroleerd; overschrijf persoonlijke bestanden niet. Een onderbroken taak krijgt status `unknown` en moet eerst via KU Leuven Sport gecontroleerd worden. Een mislukte authenticator-PIN blijft geblokkeerd; reset die uitsluitend nadat het eigen toestel handmatig gecontroleerd is.

Back-ups moeten zowel gebruikersdata als de bijbehorende afzonderlijke sleutelbestanden bevatten en privé blijven. Een Proxmox-snapshot is op dezelfde schijf geen onafhankelijke back-up. Voor een consistente Android-back-up moeten alle emulators afgesloten zijn. Bewaar relatieve qcow2-backingreferenties bij een verhuizing.

Nog door een echte gebruiker te bevestigen: Google-toestemming en terugkeer op de domein-URL, volledige registratie van een nieuw eigen KUL-account en een expliciet gekozen echte boeking/betaling. Testboekingen die geld kosten worden niet als deploymentcontrole aangemaakt.
