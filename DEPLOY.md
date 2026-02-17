# Deployment Guide

## Quick Deploy (pre-built images)

**Prerequisites:** Docker and Docker Compose installed.

```bash
# 1. Download the production compose file
curl -O https://raw.githubusercontent.com/taylormadearmy/u1-slicer-bridge/master/docker-compose.prod.yml

# 2. Start all services
docker compose -f docker-compose.prod.yml up -d

# 3. Open your browser
#    http://localhost:8080
```

That's it. On first startup the database is created automatically and a starter filament library (PLA, PETG, ABS, TPU) is seeded. You can start uploading 3MF files immediately.

### Optional configuration

Create a `.env` file next to the compose file:

```bash
# Change the web UI port (default 8080)
WEB_PORT=9090

# Connect to your printer's Moonraker API (can also be set in the UI)
MOONRAKER_URL=http://192.168.1.100:7125

# Change database credentials
POSTGRES_PASSWORD=my_secure_password
```

See [.env.prod.example](.env.prod.example) for all options.

---

## Build from Source

For contributors or anyone who wants to modify the code.

```bash
# 1. Clone the repository
git clone https://github.com/taylormadearmy/u1-slicer-bridge.git
cd u1-slicer-bridge

# 2. (Optional) Set printer URL
cp .env.example .env
# Edit .env to set MOONRAKER_URL if desired

# 3. Build and start
docker compose up -d --build

# 4. Open http://localhost:8080
```

**Development workflow:**

- Web changes: `docker compose build --no-cache web && docker compose up -d web` (then Ctrl+Shift+R in browser)
- API changes: `docker compose build --no-cache api && docker compose up -d api`
- Database reset: `docker compose down -v` (deletes all data)

---

## Connecting your printer

Moonraker is optional — slicing works without it. To enable print controls:

1. Open the web UI and click the gear icon (Settings)
2. Enter your printer's Moonraker URL, e.g. `http://192.168.1.100:7125`
3. Click **Save**

The URL is persisted in the database, so you only set it once.

---

## Accessing from other devices

No configuration needed. The app allows connections from any device on your network by default.

- Server at `192.168.1.100` → open `http://192.168.1.100:8080` from any phone/tablet/PC on the same LAN.

---

## First-time walkthrough

1. **Upload** — Click "Upload 3MF" and select a file. MakerWorld downloads, Bambu Studio exports, and PrusaSlicer exports all work.
2. **Configure** — Choose plate (if multi-plate), assign filaments, adjust settings. Defaults are sensible for PLA.
3. **Slice** — Click "Slice Now" and wait 10-60 seconds.
4. **Preview** — Interactive 3D viewer loads automatically. Left-drag to rotate, scroll to zoom, right-drag to pan.
5. **Print** — Download the G-code or click "Send to Printer" if Moonraker is configured.

---

## Updating

**Pre-built images:**
```bash
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

**From source:**
```bash
git pull
docker compose up -d --build
```

Your data (uploads, filaments, settings) is preserved across updates.

---

## Troubleshooting

**Services not starting?**
```bash
docker compose ps              # Check status
docker compose logs api        # Check API logs
```

**Filaments missing?**
Open Settings → click "Reset to Starter Library".

**CORS errors in browser console?**
Should not happen with default config. If you set `ALLOWED_ORIGINS`, make sure your device's IP is listed.

**Slicing seems stuck?**
Large models can take several minutes. Check `docker compose logs api` for progress. Restart with `docker compose restart api` if truly stuck.

**Port conflict?**
Set `WEB_PORT=9090` in your `.env` file (or any free port).

---

## Uninstalling

```bash
# Remove containers and all data
docker compose -f docker-compose.prod.yml down -v
```
