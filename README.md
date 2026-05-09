# KU Leuven Padel Reservation Bot

Automatische padel reservatie bot voor KU Leuven Sport, geoptimaliseerd voor Raspberry Pi deployment.

## Features

- 🔐 KU Leuven authenticatie met sessie persistentie
- 🎾 Automatische padel reservaties
- 📅 Configurable scheduling (exact 1 week vooruit)
- 🔔 Email/Telegram notificaties
- 🍓 Raspberry Pi optimized (low memory, systemd service)
- 🔒 Encrypted session storage
- 📊 HAR file API analysis support

## Architecture

```
kul-padel-bot/
├── src/
│   ├── auth/           # Authenticatie module
│   ├── reservation/    # Reservatie logica
│   ├── scheduler/      # Cron jobs
│   ├── utils/          # Helpers (crypto, logger, etc.)
│   ├── tools/          # HAR analyzer, debug tools
│   └── index.ts        # Entry point
├── data/               # Session storage (git-ignored)
├── logs/               # Application logs
└── systemd/            # Systemd service files
```

## Setup

### 1. Install Dependencies

```bash
npm install
```

### 2. Configure Environment

```bash
cp .env.example .env
# Edit .env with your credentials
```

### 3. Analyze KU Leuven API (HAR files)

1. Open browser DevTools (F12) → Network tab
2. Login to https://usc.kuleuven.cloud
3. Export HAR file (right-click → "Save all as HAR")
4. Place in project root as `auth.har`
5. Run analyzer:

```bash
npm run analyze-har
```

### 4. Development

```bash
npm run dev
```

### 5. Build for Production

```bash
npm run build
```

## Raspberry Pi Deployment

### 1. Transfer to Pi

```bash
# On your machine
rsync -avz --exclude node_modules . pi@raspberry.local:~/kul-padel-bot/

# On Raspberry Pi
cd ~/kul-padel-bot
npm install --production
npm run build
```

### 2. Setup Systemd Service

```bash
sudo cp systemd/kul-padel-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable kul-padel-bot
sudo systemctl start kul-padel-bot
```

### 3. Check Status

```bash
sudo systemctl status kul-padel-bot
journalctl -u kul-padel-bot -f
```

## HAR File Analysis Workflow

1. **Auth Flow**: Capture login → MFA → dashboard navigation
2. **Reservation Flow**: Capture search → select → cart → payment
3. Run `npm run analyze-har` to extract API endpoints
4. Implement API calls based on extracted data

## Reverse Engineering Strategy (if sessions expire < 3 days)

If session persistence is insufficient, we can reverse engineer the KU Leuven Authenticator:

### Option A: Android APK Analysis
1. Download APK from device/Play Store
2. Decompile with `apktool` / `jadx`
3. Extract crypto keys and auth protocol
4. Reimplement SIGMA-I handshake

### Option B: Network Interception
1. Setup MITM proxy (mitmproxy)
2. Install CA cert on Android device
3. Capture app ↔ server traffic
4. Reverse engineer protocol

### Option C: Rooted Device Extraction
1. Root Android device/emulator
2. Extract app's private storage (`/data/data/be.kuleuven.icts.authenticator/`)
3. Extract crypto keys/seeds
4. Clone authentication state

**⚠️ Legal Warning**: Reverse engineering may violate KU Leuven's Terms of Service. Use only for personal, non-commercial purposes.

## Tailscale Access

```bash
# On Raspberry Pi
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# From your machine
tailscale ssh pi@raspberrypi
```

## Troubleshooting

### Session Expired
- Check logs: `journalctl -u kul-padel-bot -n 100`
- Manually re-authenticate: `npm run auth:refresh`

### Raspberry Pi Out of Memory
- Reduce logging verbosity in `.env`: `LOG_LEVEL=error`
- Increase swap: `sudo dphys-swapfile swapoff && sudo nano /etc/dphys-swapfile`

## License

MIT - Personal use only. Respect KU Leuven's terms of service.
