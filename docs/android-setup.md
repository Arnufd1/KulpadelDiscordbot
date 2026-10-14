# Android Authenticator: aansluiten, spiegelen en login kalibreren

Doel: de officiële KU Leuven Authenticator op de eigen geregistreerde Android gebruiken voor een specifieke door onze bot gestarte login. De pc opent de bijbehorende loginlink op die telefoon. Het is een lokale opstelling; de telefoon blijft nodig voor authenticatorhandelingen.

## Huidige status

ADB, uiautomator2 en scrcpy zijn op deze werkplek geïnstalleerd. De code heeft USB-diagnose, scherminspectie, spiegeling, versleutelde PIN-opslag en een passwordless OIDC-login via de eigen challenge-link. Die webflow is gecontroleerd tot de publieke QR-pagina en getest met gesimuleerde antwoorden.

De eigen Samsung is op 8 oktober 2026 succesvol gekoppeld via draadloos ADB. `android doctor` bevestigt Android 16 en Authenticator 2.1.21. Een volledige automatische login is live geslaagd: de bot koos de accountknop, selecteerde Samsung's normale **Use PIN**-optie en voerde de versleuteld opgeslagen app-PIN één keer in. OIDC-tokens zijn opgeslagen, de reservatie-API gaf HTTP 200 en stille HTTP-sessievernieuwing werkte.

Op deze werkplek staat `PADELBOT_ANDROID_AUTOLOGIN=true`; voor nieuwe installaties blijft de standaard `false` tot het eigen toestel gekalibreerd en getest is. De telefoon moet bereikbaar en ontgrendeld zijn. Tijdens spiegeling houdt scrcpy het scherm wakker zolang het toestel aan de lader hangt. Herverbinding na een telefoon- of pc-herstart en langdurige onbeheerde werking zijn nog niet bewezen.

Ook de heraanmeldingsroute van de Discord-bot is live bevestigd: in een gecontroleerde test zijn de lokale IdP-sessiegegevens leeg gemaakt. `renew_access_token` herkende dat aanmelden nodig was, gebruikte automatisch de gekalibreerde Android en sloeg nieuwe tokens op. De reservatie-API accepteerde die tokens met HTTP 200. Dit bewijst het herstelpad bij ontbrekende sessiegegevens; de natuurlijke vervaltijd van de KU Leuven-sessie is niet afgewacht.

## Telefoon aansluiten

1. Gebruik de al geregistreerde Android met KU Leuven Authenticator.
2. Zet Ontwikkelaarsopties aan door zeven keer op Buildnummer te tikken. Zet daarna USB-foutopsporing aan. Zie de [Android-instructies](https://developer.android.com/studio/debug/dev-options).
3. Verbind de telefoon met de pc via een USB-kabel die data ondersteunt. Ontgrendel de telefoon. Kies indien nodig Bestandsoverdracht in de USB-melding.
4. Accepteer de toestemming voor USB-foutopsporing op de telefoon.

```powershell
.\.venv\Scripts\padelbot.exe android devices
.\.venv\Scripts\padelbot.exe android doctor
```

`device` betekent dat ADB de telefoon kan bedienen. Bij `unauthorized` moet de USB-toestemming op de telefoon nog worden bevestigd. Een lege lijst betekent dat er nog geen ADB-verbinding is; controleer kabel, USB-poort, USB-foutopsporing en zo nodig de USB-driver van de fabrikant. Stel `PADELBOT_ANDROID_DEVICE_ID` in `.env` in op de `device_id` uit `android doctor` om altijd dezelfde telefoon te kiezen. `PADELBOT_ANDROID_SERIAL` kan aanvullend een specifieke ADB-verbinding kiezen.

## Zonder USB-kabel via wifi

Android 11 en hoger ondersteunen [draadloos ADB met een koppelcode](https://developer.android.com/tools/adb#connect-to-a-device-over-wi-fi). Bluetooth-koppeling maakt geen ADB-verbinding. De telefoon en pc moeten op hetzelfde lokale netwerk zitten; de pc mag via ethernet aan de router hangen.

1. Open op de telefoon Instellingen → Ontwikkelaarsopties → Draadloos debuggen. Zet dit aan.
2. Tik op Apparaat koppelen met koppelcode. Houd dit venster open.
3. Start op de pc het volgende script. Voer het IP-adres en de poort van het koppelvenster in. ADB vraagt daarna lokaal de tijdelijke koppelcode.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\connect-android-wifi.ps1
```

Ga na het koppelen terug naar het hoofdscherm Draadloos debuggen. Het script vraagt het IP-adres en de verbindingspoort uit dat scherm; die poort verschilt van de koppelpoort. Daarna opent het scrcpy voor dat toestel. Dit vraagt de tijdelijke ADB-koppelcode, niet de Authenticator-app-PIN.

De verbindingspoort kan veranderen als draadloos debuggen opnieuw wordt gestart. Controleer dan het huidige adres en verbind opnieuw als ADB niet automatisch herverbindt. Selectie via `PADELBOT_ANDROID_DEVICE_ID` en een profiel met `device_id` blijft aan dezelfde fysieke telefoon gekoppeld, ook bij een andere poort of meerdere ADB-verbindingen naar dat toestel. Een permanente wifi-opstelling met automatische herverbinding is nog niet getest.

## Scherm op de pc

[scrcpy](https://github.com/Genymobile/scrcpy) toont het Android-scherm en ondersteunt muis- en toetsenbordbediening. Met een beschadigd fysiek scherm kun je de telefoon via dit venster bedienen zodra USB-foutopsporing werkt.

```powershell
.\.venv\Scripts\padelbot.exe android mirror
```

Het venster heet `KUL-Android`. Opstartinformatie staat in `data/android/mirror.log`. Spiegeling heeft vooraf een toegestane ADB-verbinding via USB of wifi nodig. Het Samsung-biometrievenster werd op dit toestel zwart weergegeven. De driver kan de normale **Use PIN**-knop daarvan selecteren, uitsluitend als de achterliggende app de Authenticator is en het venster **Log in → to: KU Leuven** toont.

Voor een volgende Windows-installatie:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup-android.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\setup-mirror.ps1
```

Deze scripts installeren de lokale Python-afhankelijkheden en downloaden de [officiële Android Platform Tools](https://developer.android.com/tools/releases/platform-tools) en [scrcpy-release](https://github.com/Genymobile/scrcpy/blob/master/doc/windows.md). De scrcpy-download wordt gecontroleerd tegen de gepubliceerde SHA-256. Toolbestanden staan onder `.tools/`, buiten Git.

## Eenmalig de echte app-flow vaststellen

```powershell
.\.venv\Scripts\padelbot.exe android inspect --open-app
# init-key alleen als er nog geen sleutel is:
.\.venv\Scripts\padelbot.exe init-key
.\.venv\Scripts\padelbot.exe login --android --manual-approval
.\.venv\Scripts\padelbot.exe whoami
```

De eerste login opent uitsluitend de link van die specifieke OIDC-poging. Je bevestigt die op de telefoon of in het spiegelvenster. De HTTP-client registreert vooraf de WebSocket, wacht op echte Authenticator-goedkeuring, wisselt daarna de code uit en bewaart tokens en sessiegegevens. Er wordt hierbij geen KU Leuven-wachtwoord gevraagd.

`android inspect` schrijft een overzicht van app-controls naar `data/android/screen.json`; de calibratielogin schrijft de eerste app-weergave naar `data/android/login-screen.json`. PIN-velden, accountnummers, e-mailadressen en links worden afgeschermd. Deze bestanden blijven lokaal. Scherminspectie bevestigt nog niet dat alle beveiligde controls geautomatiseerd kunnen worden.

## Automatische bediening na kalibratie

De app-flow wordt vastgelegd in `data/android_profile.json`, specifiek voor het toestel en de geïnstalleerde app-versie. Iedere stap gebruikt selectors van daadwerkelijk waargenomen controls, een herkenningsvoorwaarde en een expliciete actie (`click` of `pin`). Gok geen knopnamen of coördinaten. De driver ondersteunt een PIN-tekstveld of een gekalibreerd cijferpaneel. Een `keypad`-stap bevat alle tien waargenomen cijferselectors, hun exacte cijferlabels, de gemeten PIN-lengte en een `empty_selector` voor de lege PIN-indicatoren. Op deze Samsung zijn vier lege indicatoren en de knoppen `button0` tot `button9` waargenomen.

Voorbeeldstructuur met placeholders, niet direct bruikbaar:

```json
{
  "version": 1,
  "device_id": "DEVICE_ID_UIT_ANDROID_DOCTOR",
  "app_version": "WAARGENOMEN_VERSIE",
  "steps": [
    {
      "action": "pin",
      "guard": {"resourceId": "WAARGENOMEN_PIN_SCHERM"},
      "selector": {"resourceId": "WAARGENOMEN_PIN_VELD"},
      "optional": true
    },
    {
      "action": "click",
      "guard": {"resourceId": "WAARGENOMEN_BEVESTIGING"},
      "selector": {"resourceId": "WAARGENOMEN_LOGIN_KNOP"}
    }
  ]
}
```

Voer de app-PIN uitsluitend in de verborgen lokale terminalprompt in:

```powershell
.\.venv\Scripts\padelbot.exe android set-pin
.\.venv\Scripts\padelbot.exe login --android
.\.venv\Scripts\padelbot.exe whoami
```

De PIN wordt met de bestaande master key versleuteld in `data/android_pin.enc`. De driver controleert toestel, app-versie, foreground-package en unieke controls voordat hij handelt. Een PIN-poging wordt niet herhaald binnen dezelfde login. Als de login na PIN-invoer mislukt, blokkeert een lokaal foutbestand verdere automatische PIN-pogingen. Controleer de app en gebruik daarna pas `padelbot android reset-failure`.

Iedere nieuwe challenge start met een herstart van uitsluitend de Authenticator-app, zodat bevestigingsschermen van oude pogingen niet blijven staan. Accountregistratie en appgegevens blijven behouden. Tijdens een automatische poging bedient de bot ook de accountkeuze; een gelijktijdige handmatige klik kan de control laten verdwijnen en de poging onderbreken.

Na een geslaagde volledige test kan `PADELBOT_ANDROID_AUTOLOGIN=true` worden gezet. De bot probeert dan de Android-flow wanneer de IdP een nieuwe login vereist. Netwerkfouten starten op zichzelf geen Android-login. De telefoon moet bereikbaar zijn en de app toegankelijk; een Android-vergrendelscherm heeft een afzonderlijke ontgrendeling en wordt niet door deze driver omzeild.

## Later beschikbaar maken voor anderen

De code en installatiestappen kunnen hergebruikt worden. Iedere gebruiker heeft een eigen geregistreerde Android, lokale configuratie, master key, PIN en sessiegegevens nodig. `.env`, `.tools/` en `data/` blijven buiten Git. Een gedeelde clouddienst met meerdere accounts is een afzonderlijke toekomstige stap; er is nu niets gepubliceerd of gedeeld.
