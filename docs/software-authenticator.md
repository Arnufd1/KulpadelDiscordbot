# Authenticator zonder fysieke telefoon

Status op 14 oktober 2026: de Discord-bot draait in een Ubuntu-VM op Proxmox. De geregistreerde virtuele Android is vanaf de Windows-host overgezet. Een volledig nieuwe USC-login vanaf een afgesloten emulator, dezelfde KUL-account, toegang tot de API en automatisch afsluiten na de login zijn live bevestigd. De Windows-pc en fysieke gsm zijn voor deze host niet nodig. Alleen de bot blijft permanent draaien; Android start wanneer MFA nodig is.

## Wat daadwerkelijk is gecontroleerd

- De [officiële KUL-download](https://icts-p-ccis-nextauth-apk.cloud.icts.kuleuven.be/) levert momenteel een universele APK van versie 2.1.16. Het bestand bevat ook `x86_64`-bibliotheken. De gepubliceerde SHA-256 is gecontroleerd en de ondertekenaar komt overeen met de APK van versie 2.1.21 op de fysieke telefoon.
- Op deze Windows-pc meldt de officiële Google-emulator dat WHPX beschikbaar en bruikbaar is. Een officiële Google APIs API 35 / x86_64-image is opgestart; `sys.boot_completed=1` is gecontroleerd.
- De ongewijzigde APK is via de normale accountprocedure als extra toestel geregistreerd. Een nieuwe USC-login bevestigde zelfstandig de accountkeuze en voerde de lokaal versleutelde PIN één keer in. OIDC-uitwisseling, dezelfde accountidentiteit en toegang tot `/auth` zijn gecontroleerd.
- Een tweede test begon met een verlopen testtoken, lege cookies/localStorage en een afgesloten emulator. De normale `renew_access_token`-route startte de emulator met `-no-window`, meldde opnieuw aan en bereikte `/auth`. De PIN-stap volgde op het laden van het geregistreerde account en het verschijnen van de juiste bevestigingsschermen.
- Binaries staan onder `.tools/android/` en `.tools/authenticator-apk/`. De virtuele schijf en logs staan onder `data/emulators/`. Deze mappen zijn genegeerd door Git.
- Op 14 oktober is de geregistreerde AVD offline overgezet naar dezelfde emulatorversie op Linux (37.2.12, build 16428233) en dezelfde API 35 / Google APIs / x86_64-image (revisie 9). Nested KVM is gecontroleerd. De verse login en API-controle duurden samen ongeveer 57 seconden, inclusief koude start en automatisch afsluiten. Het beschikbaarheidsoverzicht voor dag 0 t/m 5 is daarna via de echte API gecontroleerd.

## Eenmalige registratie

Start het virtuele toestel met een venster:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/start-software-authenticator.ps1 -ShowWindow
```

Open KU Leuven Authenticator in het virtuele toestel. Gebruik **Initialise** of de normale procedure om via [KUL-accountbeheer](https://account.kuleuven.be/mfa) een extra toestel toe te voegen. Gebruik een herkenbare toestelnaam, bijvoorbeeld `KUL Padel - virtueel`. Het bestaande toestel blijft geregistreerd. Wachtwoorden en de app-PIN worden uitsluitend lokaal ingevoerd.

Registratie kan één bevestiging met de bestaande authenticator vragen. De [KUL-FAQ](https://admin.kuleuven.be/icts/services/mfa/faq) beschrijft het toevoegen van een toestel met behulp van het bestaande toestel. Deze procedure heeft het virtuele toestel bij de eigenaar van deze werkplek geaccepteerd.

Voer daarna de gekozen virtuele app-PIN in een verborgen lokale prompt in:

```powershell
.\.venv\Scripts\python.exe scripts/setup-software-authenticator-pin.py
```

Het script bewaart uitsluitend een versleutelde PIN in `data/software-authenticator/pin.enc`. De gekalibreerde schermselectoren staan afzonderlijk in `data/software-authenticator/profile.json`.

## Test na registratie

1. Maak een afzonderlijk Android-profiel en een versleuteld PIN-bestand voor `emulator-5554`. Gebruik de appversie en toestelidentiteit van de emulator; kopieer het Samsung-profiel niet blind.
2. Gebruik een aparte tokens- en sessiemap voor een test met een lege beginsessie. De productiegegevens blijven behouden totdat de nieuwe route slaagt.
3. Start de normale USC-login en laat uitsluitend de emulator de eigen loginlink afhandelen. Succes vereist een echte OIDC-uitwisseling en een succesvolle `/auth`-controle.
4. Herhaal na een herstart met uitsluitend de emulator geselecteerd. Controleer ook dat de route na sessieverloop opnieuw aanmeldt.
5. Pas daarna de actieve botconfiguratie aan.

Voor achtergrondgebruik kan hetzelfde script zonder `-ShowWindow` starten. Sluit eerst de bestaande emulator via zijn normale afsluitfunctie; er kunnen niet twee schrijvende instanties op dezelfde virtuele schijf draaien.

De actuele botconfiguratie bevat:

```dotenv
PADELBOT_ANDROID_SERIAL=emulator-5554
PADELBOT_ANDROID_DEVICE_ID=EMULATOR37X2X12X0
PADELBOT_ANDROID_PROFILE_FILE=./data/software-authenticator/profile.json
PADELBOT_ANDROID_PIN_FILE=./data/software-authenticator/pin.enc
PADELBOT_ANDROID_FAILURE_FILE=./data/software-authenticator/failure.json
PADELBOT_ANDROID_AUTOLOGIN=true
PADELBOT_ANDROID_EMULATOR_AUTOSTART=true
PADELBOT_ANDROID_EMULATOR_NAME=KulPadelAuth
PADELBOT_ANDROID_EMULATOR_HOME=./data/emulators
```

Neem toestelidentiteit en profiel niet over voor een ander virtueel toestel. Bij het verplaatsen van dezelfde geregistreerde AVD moet de bron afgesloten zijn en blijven userdata, encryptieschijf en relatieve qcow2-backingreferenties samen behouden. Een onnodige wijziging van die referenties kan de emulator een nieuwe gegevenslaag laten maken. Met autostart controleert de login eerst het expliciete lokale emulatortransport en start het geregistreerde toestel alleen wanneer het ontbreekt. Een mislukte PIN-poging blijft geblokkeerd door dezelfde failure latch als de fysieke Android-route. Windows met WHPX en Linux met nested KVM zijn getest; langdurige werking wordt na ingebruikname verder gevolgd.

## Tijdelijk draaien op een kleine host

De bot blijft online voor Discord, maar Android hoeft niet permanent te draaien. De login controleert eerst of HTTP volstaat. Pas wanneer de IdP een Authenticator-stap vraagt, start de bot de emulator. Bij een keuzescherm gebeurt de koude start voordat de kort geldige loginlink wordt opgevraagd. Het eigen proces blijft beschikbaar tot de OIDC-uitwisseling klaar is en wordt daarna afgesloten, ook bij een mislukte login of koude start. De geregistreerde Android-schijf blijft bewaard; er wordt geen toestel gewist. Een vooraf handmatig gestart toestel wordt hergebruikt en blijft open voor kalibratie.

Voor een kleine host kunnen de tijdelijke emulatorprocessen beperkt worden:

```dotenv
PADELBOT_ANDROID_EMULATOR_MEMORY_MB=2560
PADELBOT_ANDROID_EMULATOR_CORES=1
```

Zonder deze instellingen blijft de RAM/CPU-configuratie van de gekalibreerde AVD gelden. Google documenteert 1536 MB als ondergrens voor de [`-memory`-optie](https://developer.android.com/studio/run/emulator-commandline), maar de gebruikte emulator/image verhoogt kleinere verzoeken zelf naar 2560 MB. De Linux-configuratie vraagt daarom expliciet 2560 MB en één core. De oude Windows-AVD vroeg 2048 MB en twee cores. Een VM-plafond van 3 GB bleek te laag voor de koude start met alle emulatoroverhead.

De Proxmox-opstelling gebruikt een minimale Ubuntu Server 24.04-VM zonder desktop: CPU-type `host`, twee gedeelde vCPU's, een dun toegewezen schijf van 24 GB en 4096 MB beschikbaar guestgeheugen. `balloon=4096` houdt dat geheugen bereikbaar voor de korte Android-piek. Een lage automatische balloon-minimumwaarde bleek de emulator vóór de start te blokkeren, omdat de guest op dat moment te weinig beschikbaar geheugen zag.

Het fysieke hostverbruik wordt beperkt met [free-page-reporting](https://docs.kernel.org/mm/free_page_reporting.html), dat in de QEMU-balloondevice aanstaat. [De guest kan daarmee ongebruikte pagina's aan Proxmox teruggeven](https://lore.proxmox.com/all/5803a37d-7308-1e0d-89f7-fc80c9b297d8@proxmox.com/t/). De configuratie [deploy/padelbot-memory.conf](../deploy/padelbot-memory.conf) stelt de minimale rapportageblokgrootte in op 64 KiB (`page_reporting_order=4`), zodat ook kleinere vrije blokken na Android-gebruik terugkomen. Na afsluiten adviseert de bot Linux bovendien de ongebruikte SDK- en AVD-bestandscache vrij te geven. De schijfgegevens blijven behouden.

Op 14 oktober is na een VM-herstart gecontroleerd dat Discord, tokenvernieuwing, de echte API en het zesdaagse overzicht zelfstandig terugkomen, met één actieve bot en zonder draaiende emulator. In een aparte koude start/stop-cyclus piekte het fysieke host-RSS op ongeveer 3599 MiB en daalde het na afsluiten naar 757 MiB; direct na de herstart was dat ongeveer 727 MiB. Dit zijn momentopnamen, geen gegarandeerde vaste waarden. De botservice zelf gebruikte circa 60–100 MB. De Linux-testsuite bevatte 465 geslaagde tests.

## Verschil met een directe protocolclient

Deze opstelling gebruikt de officiële authenticator in een virtuele Android. Een zelfstandige Python- of Go-client zonder Android is nog niet geïmplementeerd. De [nextAuth-documentatie](https://docs.nextauth.com/mobile-sdk/v2/quick-start/deep-links.html) beschrijft afzonderlijke registratie- en loginlinks; het mobiele SDK handelt de authenticatie af. Het IdP-WebSocketbericht `LOGIN` is slechts het resultaat daarvan.

Voor meerdere gebruikers zijn afzonderlijk gekoppelde accounts en gecontroleerde toegang tot hun sessies nodig. Deze ene testopstelling biedt nog geen openbare dienst. Ook werking op ARM/Raspberry Pi is hiermee niet aangetoond.

## Statische aanwijzingen voor verder protocolonderzoek

De universele APK is lokaal onderzocht zonder geregistreerde accountgegevens uit te lezen. De native authenticatiebibliotheek biedt een kleine JNI-ingang: `Mobileapp.test(byte[]) -> byte[]`, een initialisatiefunctie en een Java-callback. Een Java-wrapper serialiseert verzoeken met onder meer een functienummer, booleans, byte-arrays, strings en integers. Die ingang vormt een aanknopingspunt om de platformgrens te begrijpen; functienummers en de volledige berichtcodering zijn nog niet in kaart gebracht.

De `x86_64`-bibliotheek is een Android ELF-bestand en hangt af van `libandroid.so`, `liblog.so`, `libdl.so` en `libc.so`. Daarmee is het geen direct laadbare Windows/Python-bibliotheek. Er is nog geen zelfstandig platformadapter, mobiele protocolimplementatie of nieuwe accountregistratie buiten Android getest. Analyseverslagen blijven onder de genegeerde APK-map; er worden geen vendorbinaries mee gepubliceerd.
