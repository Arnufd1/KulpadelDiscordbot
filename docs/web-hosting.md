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

https://usc.kuleuven.cloud/members/profile/myshopping#storedpaymethodcard

De nieuwe gebruikersscheduler volgt de gecontroleerde API-grens van 168 uur vóór de start, berekend in UTC zodat ook een zomer-/wintertijdwissel klopt. Als er nog geen terrein vrij is, controleert hij de wekelijkse regel om de twee minuten opnieuw binnen het opgegeven startvenster. Alleen een uitdrukkelijk leesresultaat zonder reservatie mag opnieuw gecontroleerd worden; een uitgevoerde of onzekere schrijftaak behoudt zijn ontdubbelingssleutel.

Instellingen bevat een persoonlijke kaartkluis: één kaart per gebruiker, vervangen en verwijderen. Kaartnummer, kaarthouder en vervaldatum worden vóór een database-write met AES-256-GCM versleuteld. Elke vervanging gebruikt een nieuwe willekeurige datasleutel en nonces; de datasleutel wordt zelf versleuteld. Authenticatie van de ciphertext bindt beide lagen aan profiel en kaart-ID. De API geeft uitsluitend merk, laatste vier cijfers en vervaldatum terug. Onbekende velden (waaronder CVC/CVV) worden geweigerd en validatiefouten echoën nooit de aanvraag. Auditregels bevatten alleen de actie, geen kaartgegevens. Er is geen endpoint dat het volledige kaartnummer teruggeeft en beheer toont geen kaarten.

De aparte hoofdsleutel is een bestand van 32 willekeurige bytes op `/etc/padelbot/card-vault.key`, map 0700 root/root en bestand 0600 root/root. `LoadCredential` geeft de webservice een private, alleen-lezen kopie; de sleutel staat niet in Git, de database, bronarchieven of worker-environment. Core dumps staan voor de webservice uit. Maak deze sleutel bij installatie één keer met een cryptografische randombron; vervang een bestaande sleutel nooit automatisch. Een ontbrekende of verkeerde sleutel blokkeert de kluis. Bewaar een aparte private back-up van deze sleutel als herstel vereist is. Rotatie vereist het opnieuw verpakken van alle datasleutels en een consistente database-back-up. Verwijderen wist de actieve rij; bestaande back-ups volgen hun eigen bewaartermijn.

Deze versleuteling beschermt een losse databasekopie; zij geeft geen bescherming tegen een volledig gecompromitteerde webserver/root of een VM-back-up die ook de sleutel bevat, en is geen PCI-DSS-certificering. HTTPS beschermt het transport; de TLS-terminatie bij Cloudflare blijft onderdeel van het vertrouwde pad. Voor het ontwerp zijn de [OWASP-richtlijnen voor cryptografische opslag](https://cheatsheetseries.owasp.org/cheatsheets/Cryptographic_Storage_Cheat_Sheet.html) gevolgd. De webapp vraagt of bewaart geen CVC: die mag ook voor terugkerende betalingen niet na autorisatie worden bewaard ([PCI SSC](https://www.pcisecuritystandards.org/faqs/1280/)). De nieuwe kluis importeert de eerdere Discord-kaartbestanden niet.

Een opgeslagen-kaartbetaling voor nieuwe gebruikers is **niet geïmplementeerd**. De kaartkluis bewaart gegevens en geeft geen betaalmachtiging bij KUL. De gecontroleerde USC-frontend gebruikt Ogone; een algemene Worldline-tokenintegratie is daardoor niet zonder meer beschikbaar voor dit KUL-account. Werkelijke kaartbetalingen vereisen een ondersteunde KUL/providerkoppeling; web-autobooking gebruikt voorlopig eigen KUL-saldo of een bestaand mandaat.

De bestaande Discord-bot behoudt zijn bestaande regel- en betaalinstellingen. De webapp kan dezelfde eigenaarsregels bekijken, activeren, pauzeren en verwijderen. Het webbetalingsvinkje en prijsmaximum gelden voor webboekingen en de nieuwe gebruikersscheduler; zij wijzigen de bestaande Discord-betaalconfiguratie niet. Er is geen tweede scheduler voor de eigenaar in de webapp.

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

Deelname wordt op deze website bijgehouden; de KUL-reservering blijft op naam van de organisator. Er wordt geen KUL-deelnemer toegevoegd en geen nieuwe boeking/betaling uitgevoerd. Gedeelde velden bevatten alleen terrein, tijd, voornamen en aantallen, geen e-mails, KUL-reservatienummers of accountgegevens. Er staan momenteel geen komende eigen KUL-reservaties; er zijn geen voorbeeldmatches op de productiesite gezet.

Nog door een echte gebruiker te bevestigen: registratie van een nieuw eigen KUL-account en een expliciet gekozen echte boeking/betaling. Testboekingen die geld kosten worden niet als deploymentcontrole aangemaakt.

De uitbreiding is getest met 481 volledige Linux-tests (25 Windows-tests overgeslagen), 43 gerichte Windows-webtests, en aparte browsercontroles op desktop en mobiel. De browsercontroles gebruiken een tijdelijke lokale database, testkaartnummers en gesimuleerde KUL-resultaten; zij zetten geen kaarten of voorbeeldmatches in productie. De echte KUL-clientcontrole voor de eigenaar gaf nul komende reservaties terug.
