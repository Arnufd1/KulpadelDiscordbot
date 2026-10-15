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

## KUL koppelen

Instellingen bevat een wizard met drie stappen: KUL-accountbeheer openen, de registratie-QR van een nieuw toestel toevoegen (afbeelding kiezen of vanuit het klembord plakken), en een PIN kiezen. Een registratielink is een alternatief. De gewone QR voor aanmelden maakt geen nieuw toestel; bij een herkenbaar aanmeldscherm vraagt de wizard om een registratie-QR.

Het toestel start via een korte POST en een persoonlijke status-API, zodat een koude Android-start geen lange open HTTP-aanvraag vereist. Andere gebruikers kunnen de sessie niet overnemen of de status ervan lezen. De bekende nieuwe-PIN-schermen worden elk eenmaal ingevuld en bevestigd; bestaande accounts, onbekende schermen en aanmeldschermen krijgen geen automatische PIN-invoer. De bestaande PIN-foutblokkering blijft behouden. Pas na een werkelijk geregistreerd account worden profiel/PIN bewaard en wordt een aparte KUL-login uitgevoerd. De interface meldt succes nadat die login slaagt. De handmatige toestelbediening staat onder een optioneel uitklapvak.

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

Nieuwe webgebruikers stellen expliciet automatisch betalen en hun maximumbedrag per boeking in. De webworker gebruikt uitsluitend het eigen KUL-saldo of een bestaande geldige domiciliëring. Beheer daarvan gebeurt via de officiële KUL-pagina:

https://usc.kuleuven.cloud/members/profile/myshopping#storedpaymethodcard

De nieuwe gebruikersscheduler volgt de gecontroleerde API-grens van 168 uur vóór de start, berekend in UTC zodat ook een zomer-/wintertijdwissel klopt. Als er nog geen terrein vrij is, controleert hij de wekelijkse regel om de twee minuten opnieuw binnen het opgegeven startvenster. Alleen een uitdrukkelijk leesresultaat zonder reservatie mag opnieuw gecontroleerd worden; een uitgevoerde of onzekere schrijftaak behoudt zijn ontdubbelingssleutel.

Instellingen bevat een persoonlijke kaartkluis: maximaal vijf kaarten per gebruiker, een standaardkaart selecteren en per kaart verwijderen. Kaartnummer, kaarthouder en vervaldatum worden vóór een database-write met AES-256-GCM versleuteld. Elke nieuwe kaart gebruikt een nieuwe willekeurige datasleutel en nonces; de datasleutel wordt zelf versleuteld. Authenticatie van de ciphertext bindt beide lagen aan profiel en kaart-ID. De API geeft uitsluitend kaart-ID, kaartnaam, merk, laatste vier cijfers, vervaldatum en standaardkeuze terug. Onbekende velden (waaronder CVC/CVV) worden geweigerd en validatiefouten echoën nooit de aanvraag. Auditregels bevatten alleen de actie, geen kaartgegevens. Er is geen endpoint dat het volledige kaartnummer teruggeeft en beheer toont geen kaarten.

De aparte hoofdsleutel is een bestand van 32 willekeurige bytes op `/etc/padelbot/card-vault.key`, map 0700 root/root en bestand 0600 root/root. `LoadCredential` geeft de webservice een private, alleen-lezen kopie; de sleutel staat niet in Git, de database, bronarchieven of worker-environment. Core dumps staan voor de webservice uit. Maak deze sleutel bij installatie één keer met een cryptografische randombron; vervang een bestaande sleutel nooit automatisch. Een ontbrekende of verkeerde sleutel blokkeert de kluis. Bewaar een aparte private back-up van deze sleutel als herstel vereist is. Rotatie vereist het opnieuw verpakken van alle datasleutels en een consistente database-back-up. Verwijderen wist de actieve rij; bestaande back-ups volgen hun eigen bewaartermijn.

Deze versleuteling beschermt een losse databasekopie; zij geeft geen bescherming tegen een volledig gecompromitteerde webserver/root of een VM-back-up die ook de sleutel bevat, en is geen PCI-DSS-certificering. HTTPS beschermt het transport; de TLS-terminatie bij Cloudflare blijft onderdeel van het vertrouwde pad. Voor het ontwerp zijn de [OWASP-richtlijnen voor cryptografische opslag](https://cheatsheetseries.owasp.org/cheatsheets/Cryptographic_Storage_Cheat_Sheet.html) gevolgd. De webapp vraagt of bewaart geen CVC: die mag ook voor terugkerende betalingen niet na autorisatie worden bewaard ([PCI SSC](https://www.pcisecuritystandards.org/faqs/1280/)). De bestaande Revolut-kaart van de eigenaar wordt eenmalig expliciet overgenomen: uitsluitend nummer, kaarthouder en vervaldatum, zonder CVC. Een transactiemarker voorkomt dubbele import bij herstart. Het bestaande Discord-betaalbestand blijft apart.

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

Discord en website gebruiken dezelfde `shared_matches` en `match_players` in `data/rules.sqlite3`. Aantallen en de capaciteit van vier spelers zijn dus atomair gedeeld. Bestaande matches en bericht-ID's blijven behouden. De eigenaar wordt via de bestaande vaste Discord-owner-ID gekoppeld; andere gebruikers koppelen hun eigen Discord-account met een eenmalige code uit Instellingen en `/koppel CODE` in Discord (tien minuten geldig, alleen gehasht opgeslagen). Koppelen voegt dubbele web/Discord-deelnames samen. Reeds geposte serverberichten worden bijgewerkt zonder nieuwe groepsberichten of pings; een user-installed groeps-DM leest de actuele lijst via zijn Vernieuwen-knop.

De KUL-reservering blijft op naam van de organisator. Deelname maakt geen nieuwe KUL-boeking of betaling. Gedeelde velden bevatten alleen terrein, tijd, voornamen en aantallen, geen e-mails, KUL-reservatienummers of accountgegevens. `paidFor` is op zichzelf onvoldoende bewijs: ook de eigen verkoop moet statusCode 0 hebben. Online boekingen opent met een begrensde KUL-controle; de zichtbare lijst wordt periodiek bijgewerkt. Alleen de eigenaar ziet Beheer.

## Drafts

Vanuit een prijsvoorstel kan de organisator een onbetaald voorstel aanmaken. Een draft telt de organisator als eerste van maximaal vier spelers; deelname is interesse, zonder betaalopdracht. De worker controleert het exacte eigen winkelwagenitem elke 45 minuten. Hij hergebruikt een bevestigd passend item en maakt uitsluitend een normaal `/cart-items`-item aan wanneer nodig en nog beschikbaar. Andere winkelwagenitems worden nooit vervangen of verwijderd voor een draft. Een verloren schrijfantwoord wordt eerst met een read gecontroleerd, nooit blind herhaald.

De looptijd vanaf aanmaak is maximaal 24 uur bij meer dan 48 uur voor aanvang, 6 uur tussen 16 en 48 uur, en 45 minuten binnen 16 uur. Looptijden zijn veelvouden van 45 minuten. Het bereiken van een strengere grens verkort de oorspronkelijke looptijd en kan die direct be?indigen; stoppen/opnieuw aanmaken of een andere tijdzone start de limiet voor hetzelfde slot niet opnieuw. Maximaal drie actieve drafts per profiel. Verlopen, pauzeren en annuleren verbergen de draft en ruimen uitsluitend het eigen gelabelde, onbetaalde item op. Opruimen kent maximaal drie automatische pogingen, met vijf minuten ertussen. Bij een onbekende uitkomst wordt de draft verborgen en krijgt de eigenaar een melding om KUL te controleren.

Een winkelwagenitem geeft **geen garantie op een gereserveerd terrein**. Volgens de [KUL-reservatievoorwaarden](https://www.kuleuven.be/sport/accommodatie/reservaties/padel) is een reservatie pas definitief na betaling. Draftcontroles cre?ren geen `/sales` of betaalopdracht en omzeilen geen backend-vervaltijd. Alleen de organisator kan via een nieuwe prijscontrole en expliciete bevestiging boeken en betalen, met behoud van de spelerslijst. Bij een onbekende betaaluitkomst of serverherstart volgt geen automatische nieuwe betaalpoging. Webbetalingen blijven eigen KUL-saldo/bestaand mandaat gebruiken; de standaardkaart in de kluis is nog geen KUL-betaalmachtiging.

De telefooninterface heeft vaste navigatie onderaan, ruimte voor de safe area, grote bedieningsknoppen en invoervelden van 16 pixels. Desktop en telefoon gebruiken dezelfde rechtencontroles.

Nog door een echte gebruiker te bevestigen: registratie van een nieuw eigen KUL-account en een expliciet gekozen echte boeking/betaling. Testboekingen die geld kosten worden niet als deploymentcontrole aangemaakt.

De uitbreiding is getest met 509 volledige Linux-tests (25 Windows-tests overgeslagen), 149 gerichte Windows-tests, en aparte browsercontroles op desktop, telefoons van 320/360/390/430 pixels en een liggend scherm van 844 pixels. De browsercontroles gebruiken een tijdelijke lokale database, testkaartnummers en gesimuleerde KUL-resultaten; zij zetten geen kaarten of voorbeeldmatches in productie. De echte KUL-clientcontrole voor de eigenaar gaf nul komende reservaties terug.
