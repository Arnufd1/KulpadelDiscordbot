# Auth-onderzoek en implementatiestatus

Onderzocht op 8 oktober 2026. Projectbasis: `Arnufd1/KulpadelDiscordbot`, commit `97bfb44`.

Referentieversie van `python-kuleuven`: [commit 6de0d2f](https://github.com/breitburg/python-kuleuven/commit/6de0d2fe587f80fbbd6816e4d8aabae093be890f), van 23 augustus 2026.

## Bruikbare bestaande implementatie

[breitburg/python-kuleuven](https://github.com/breitburg/python-kuleuven) is de rechtstreeks relevante gevonden repo. De [auth-documentatie](https://github.com/breitburg/python-kuleuven/blob/main/docs/AUTH.md) beschrijft de Shibboleth-formulieren, localStorage-probes en Authenticator-push via WebSocket. De [Shibboleth-client](https://github.com/breitburg/python-kuleuven/blob/main/src/kuleuven/shibboleth.py) verwerkt wachtwoord- en TOTP-stappen; de [push-client](https://github.com/breitburg/python-kuleuven/blob/main/src/kuleuven/nextauth.py) wacht op goedkeuring via `LOGIN`.

De repo bedient Toledo, Blackboard en KURT via SAML. De sporttool gebruikt USC via OIDC met een ander callback- en tokenpad. Daarom is de volledige library geen directe padelclient. Het gedeelde IdP-protocol is wel bruikbaar als referentie voor een eigen OIDC-adapter.

Een pushlogin vereist daar nog goedkeuring op de gsm. De beschreven device-trust-cookie bewijst niet dat USC dezelfde MFA- of sessielevensduur hanteert. Er is in de uitgevoerde zoekopdrachten geen tweede actuele repo bevestigd die volledig onbeheerde USC-authenticatie levert. Het oudere [Kotnet-loginproject](https://github.com/studentenraad/kotnetlogin-extension) betreft netwerklogin en is gearchiveerd; het lost de huidige sportlogin niet op.

## Vervolgonderzoek: volledig autonome Authenticator-login

Het gewenste resultaat is opnieuw aanmelden na sessieverloop zonder menselijke goedkeuring. Op 8 oktober 2026 zijn daarvoor extra webzoekopdrachten en GitHub-repositoryzoekopdrachten uitgevoerd, waaronder de exacte frase `"KU Leuven Authenticator" in:readme`. Repositoryzoekopdrachten doorzoeken geen volledige broncode; een deel van de verdere GitHub-API-zoekopdrachten liep tegen de publieke rate limit aan. Geen gevonden project is bevestigd als een zelfstandig werkende vervanging van het geregistreerde Authenticator-toestel.

| Project | Wat het werkelijk automatiseert | Volledige Authenticator-goedkeuring? |
|---|---|---|
| [python-kuleuven](https://github.com/breitburg/python-kuleuven) | HTTP-login en het verzenden van een push; `nextauth.py` wacht op `LOGIN` na goedkeuring op de gsm | Niet door deze code |
| [authenticate-to-ntfy](https://github.com/jovandeginste/authenticate-to-ntfy) | Browserextensie stuurt de loginlink naar de gsm via ntfy | Nee, de gebruiker opent de link |
| [ku-leuven-autobooker](https://github.com/YJimmyZhang/ku-leuven-autobooker) | Boekt KURT-studieplaatsen met een sessiecookie die een Firefox-extensie aanlevert | Nee, opnieuw aanmelden en cookies aanleveren blijven nodig |
| [Automatic-KU-Leuven-VPN-login](https://github.com/BigLMatt/Automatic-KU-Leuven-VPN-login) | VPN-aanmelding na een bestaande Toledo-login | Nee, de README vereist vooraf handmatige Toledo-login met MFA |
| [VIVES-Dynamic-Wallpaper](https://github.com/SanderHenderyckx/VIVES-Dynamic-Wallpaper) | Rooster ophalen na interactieve login in een persistent browserprofiel | Nee, de README beschrijft handmatige Authenticator-goedkeuring |
| [kotnetcli](https://github.com/GijsTimmers/kotnetcli) | Kotnet-netwerkaanmelding | Geen centrale KU Leuven-authenticatieportal |

De actuele [KU Leuven MFA-FAQ](https://admin.kuleuven.be/icts/services/mfa/faq), bijgewerkt op 2 september 2026, noemt voor de centrale IdP-sessie minstens vier en maximaal twaalf uur, gekoppeld aan het IP-adres. De toepassingssessie duurt typisch twee uur, met mogelijke afwijkingen per toepassing. Keepalive alleen bewijst daarom geen onbeperkte autonome werking. De door een andere repo beschreven trusted-device-cookie moet afzonderlijk voor USC worden getest.

Volgens de [nextAuth-technologiebeschrijving](https://www.nextauth.com/technology/) gebruikt het geregistreerde mobiele component een privésleutel en SIGMA-I voor wederzijdse authenticatie. Het WebSocket-bericht `LOGIN` vertelt de browser dat die authenticatie geslaagd is; wachten op dat bericht implementeert de mobiele authenticatie zelf niet.

De praktische route via de officiële app op een eigen geregistreerde Android is inmiddels uitgevoerd met [openatx/uiautomator2](https://github.com/openatx/uiautomator2). Op 8 oktober 2026 is onze eigen integratie live getest op Android 16 met Authenticator 2.1.21: de bot opent uitsluitend zijn eigen loginlink, kiest de accountknop, selecteert de normale Samsung-optie **Use PIN** en voert de lokaal versleutelde app-PIN één keer in. De OIDC-uitwisseling en de reservatie-API (`/auth`, HTTP 200) zijn geslaagd, evenals stille HTTP-vernieuwing. Een gecontroleerde test met lege lokale sessiegegevens bevestigde ook automatische heraanmelding via dezelfde `renew_access_token`-route als de Discord-bot. Zie [Android-installatie en teststatus](android-setup.md).

Dit is een lokale Android-opstelling die een bereikbaar, ontgrendeld toestel vereist. Langdurige werking en herverbinding na herstart zijn nog niet bewezen. Een directe protocolclient zonder telefoon vraagt afzonderlijk onderzoek naar de geregistreerde toestelidentiteit en het mobiele protocol; er is daarvoor nog geen bruikbare publieke KUL-client gevonden.

## Onderzoek naar een opstelling zonder fysieke gsm

Update 9 oktober 2026: de officiële universele APK bevat ook `x86_64`-bibliotheken. Versie 2.1.16 is in een officiële Google-emulator met Android API 35 geregistreerd. Een volledig automatische nieuwe USC-login en toegang tot `/auth` zijn live bevestigd. Ook de normale herstelroute bij een verlopen token en lege sessiegegevens startte een afgesloten emulator zonder venster en meldde zelfstandig opnieuw aan. De actieve bot is overgezet. Zie [software-authenticator: gecontroleerde resultaten en configuratie](software-authenticator.md). De onderstaande APK-analyse betreft de eerdere fysieke Android-build.

De geïnstalleerde Authenticator 2.1.21 is op 8 oktober 2026 lokaal statisch onderzocht. Alleen APK-bestanden zijn gekopieerd; geregistreerde accountgegevens en privésleutels zijn niet uitgelezen. APK's, gedecompileerde wrappers en analysebestanden staan onder de genegeerde map `.tools/authenticator-apk/2.1.21/` en horen niet in deze repo.

De manifestgegevens van deze build noemen minimaal Android API 32 en target API 36. De kopie van dit ARM64-toestel bevat `libgojni.so`: een native Go-component met een Android-wrapper. De Java-wrapper gebruikt `AndroidKeyStore`, `secp256r1` en `SHA256withECDSA`. Een wrapper bevat een alias `nextAuth_biometric`, optionele attestation en een fallback bij ontbrekende StrongBox-ondersteuning. Dat bewijst geen verplichte hardwarebinding van de volledige PIN-login; er is nog geen onafhankelijk werkende native protocolclient gemaakt.

De [Android-emulator](https://developer.android.com/studio/run/emulator) draait inmiddels de officiële universele APK op `x86_64`, zonder ARM-vertaling. Het virtuele toestel is via de normale KUL-procedure als extra authenticator geregistreerd. De ADB-driver gebruikt een afzonderlijk gekalibreerd profiel en versleutelde PIN. Bij een koude start moet het SDK eerst zijn geregistreerde account laden voordat de loginlink wordt geopend; vervolgens wordt gewacht op de eigen bevestigingsactiviteit. Ondersteuning op ARM-hosts is hiermee niet getest.

Een zelfstandig programma op Windows is een afzonderlijke route. Volgens de [nextAuth-technologiebeschrijving](https://www.nextauth.com/technology/) horen geregistreerde sleutels en het SIGMA-I-protocol bij de mobiele authenticatie. Alleen de app decompileren of de bestaande browser-WebSocket gebruiken vervangt die identiteit niet. Een nieuwe normale registratie verdient de voorkeur boven een poging de bestaande telefoonidentiteit over te zetten. Ook een emulatoropstelling vereist een draaiende pc en virtuele Android.

## Betaalonderzoek en correcties

De publieke USC-bundles `main-ONGV3MVM.js`, `chunk-32EMBWR3.js` en `chunk-Z7LBPPSR.js` zijn op 8 oktober 2026 onderzocht. De huidige website biedt `POST /sales/{id}/pay` met `storedPayMethodId`, `payMethod`, `onSuccessRedirectUrl`, `createStoredPayMethod` en `paymentProviderType`. Het antwoord kan een `userInitiatedPaymentResult.hostedPaymentPageUrl` of een resultaat op een opgeslagen betaalmethode bevatten. De bot gebruikt nu dit checkoutpad; het oude handmatig samengestelde `/payments`-record en de vaste bankreferentie zijn vervangen. Voordat een betaling wordt gevraagd controleert de bot de eigenaar, het bedrag, de verkoopstatus en bestaande betalingen. Onzekere schrijfacties worden niet herhaald.

De website controleert na de callback opnieuw `GET /sales/{id}` met de betalingen. `statusCode=0` betekent een afgeronde verkoop; `collectStatus=0` betekent geïnd, `2` nog te innen en `8` geannuleerd. Een leescontrole van drie bestaande eigen verkopen bevestigde deze vorm en de Ogone-provider (`paymentProviderType=8`). Dit is geen live bewijs van de volledige nieuwe checkoutflow.

De bot herkent nu uitsluitend de exacte HTTPS-callback op `usc.kuleuven.cloud/sales/callback` met één `status=success`. Die callback is onvoldoende om `paid=True` te zetten: de bot controleert daarna de verkoop-ID, het bedrag en de serverstatus. Voor kaart/saldo zijn bovendien geïnde betalingen nodig; bij domiciliëring kan een afgeronde verkoop vóór de bankinning geaccepteerd zijn. Een onzekere bevestiging leidt tot een melding om eerst de verkoop te controleren. De bevestigingspoll doet alleen GET-verzoeken en maakt geen tweede betaling aan. `/pay` claimt ook geen betaling meer na het aanmaken van een checkoutrecord en controleert vooraf bestaande betalingen.

Betaalfouten bewaren alleen veldmetadata. Volledige HTML, screenshots en querystrings van betaalpagina's worden niet meer als diagnose opgeslagen; Playwright-fouten tijdens het invullen loggen geen ingevoerde waarde.

### Vastgelegde handmatige betaling en browsertest

De gebruiker leverde een HAR en Chrome NetLog aan in `hars etc/`. Deze gehele map blijft buiten Git, omdat de opnamen credentials en financiële gegevens kunnen bevatten. De HAR is ongeldig/afgebroken; 223 volledige entries konden worden teruggelezen. De complete NetLog bevat 199.707 events en nog de gedecodeerde HTTP-responsebodies. Alleen toegestane veldmetadata en betaalstatussen zijn in de onderzoeksuitvoer opgenomen; kaartwaarden, cookies, tokens en bankcodes zijn niet uitgeprint of overgezet naar de kaartopslag.

Deze opname bevestigt een handmatige betaling van EUR 8 via `payMethod=6`, Ogone-provider `8`, Worldline en een Revolut 3DS-stap. De flow is: verkoop aanmaken, `POST /sales/{id}/pay`, backendcheckoutlink, Worldline-merkselectie, kaartformulier, 3DS, terugkeer naar KUL. Na terugkeer heeft het betaalrecord `collectStatus=0` en de verkoop `statusCode=0`. Een verse verkoop bevat eerst een te innen `payMethod=12`-placeholder; die geldt niet als een al gestarte kaartbetaling en moet de eerste checkout niet blokkeren.

De server levert nog een HTTP-link naar de bekende SAP-redirect op `www.kuleuven.be`; de bot gebruikt de HTTPS-versie met dezelfde backendparameters. De kaartdriver vult uitsluitend de aangetroffen Worldline-host en gebruikt nu de vastgelegde velden `payment-cardnumber`, `payment-cardholdername`, `payment-cardexpirationmonth`, `payment-cardexpirationyear`, `payment-cvc` en `payment-submit`. Ontbrekende vervaldatumvelden stoppen vóór submit. Een onzekere submit wordt niet via een ander knopalternatief herhaald. De aangetroffen Worldline-vervolgknoppen `payment-ContinueToPaymentPage` en `payment-ContinueToMerchantWebsite` worden afgehandeld; de driver bedient geen bankformulier.

Een echte Chromium-test met uitsluitend nagebouwde pagina's en fictieve kaartgegevens controleert het invullen, één submit, de vervolgknoppen en de callback. Een ontbrekende vervaldatum leidt aantoonbaar tot geen submit. Deze test bewijst geen volledig automatische echte bankbetaling. Bankbevestiging, langdurige werking en eventueel gebruik van een opgeslagen betaalmethode moeten afzonderlijk worden gevalideerd. De bot heeft tijdens dit onderzoek geen nieuwe boeking of betaling uitgevoerd.

## Eigen waarnemingen

Een publieke GET naar de USC OIDC-authorize-URL gaf een Shibboleth localStorage-read-formulier. Na het beantwoorden van die opslagprobe verschenen `nextauthLoginForm` en `pwdLoginForm`, met onder meer `username`, `password`, `rememberlogin` en `_eventId`.

Dit is opnieuw gecontroleerd met de nieuwe HTTP-walker: hij verwerkt de opslagprobe en stopt bij de wachtwoordpagina. Er zijn geen echte credentials verzonden, geen MFA-pushes gestart, geen sport-API-schrijfoperaties gedaan en geen betalingen uitgevoerd.

## Wijzigingen in deze branch

- `idp.py`: begrensde HTTP-formulierflow met behoud van verborgen velden, cookies, localStorage-reads en letterlijke opslagwrites. Sessievernieuwing stopt vóór interactieve login. De sportcallback wordt nooit opgehaald.
- `auth.py`: HTTP-vernieuwing vervangt Chromium, browserlogin blokkeert de callback vóór de SPA, callback/state-validatie is aangescherpt. Workers delen een lock rond tokenrotatie en opslag.
- `login --http`: expliciete wachtwoordlogin met verborgen prompt, TOTP-code of geselecteerde Authenticator-push. De WebSocket wordt vóór de push geregistreerd; alleen `LOGIN` voltooit de tweede factor.
- Token- en sessiebestanden worden atomisch vervangen. Credentials worden niet door de bot opgeslagen.
- Discord keepalive stopt niet meer permanent na een eerste fout. Een nieuwe lokale login of herstelde netwerkverbinding kan automatisch herstellen.
- De scheduler verwerkt alle regels met hetzelfde openingsmoment, ook wanneer een eerdere boeking of Discord-melding mislukt. Gepauzeerde of verwijderde regels worden vóór de poging opnieuw gecontroleerd.
- README, setup en `.env.example` beschrijven de daadwerkelijke Python-code.

## Validatie en vervolgstappen

De regressietests gebruiken gesimuleerde HTTP- en WebSocket-antwoorden. Ze controleren onder meer callback-races, MFA-stops, origincontrole, opslagrotatie, verborgen velden, gelijktijdige tokenvernieuwing en keepalive-herstel. De live controle omvat uitsluitend de publieke stappen vóór het aanmelden.

1. Test `login --http`, `whoami` en `silent-refresh` met het eigen sportaccount, aanvankelijk zonder boekingsregels. Noteer hoe lang de sessie daadwerkelijk vernieuwbaar blijft.
2. Controleer welke MFA-vorm dit account gebruikt en welke trusted-device-cookie USC accepteert. Stel sessieduur vast uit eigen waarnemingen; veronderstel geen onbeperkte keepalive.
3. Verifieer de boekingsopening. De [KU Leuven-handleiding](https://www.kuleuven.be/sport/onlinetool/handleiding) noemt maximaal één week vooruit en een definitieve reservatie na betaling. Het huidige uur `00:00` en de offset van 200 ms zijn nog aannames.
4. De schedulerfout met gelijktijdige regels is opgelost en offline getest. De vroege offset en het ontbreken van een korte retry bij nog gesloten slots verdienen nog een live timingtest en zo nodig een fix.
5. Valideer boekingsstatus en betaalbevestiging end-to-end voordat een geplande reservatie als definitief wordt beschouwd.
