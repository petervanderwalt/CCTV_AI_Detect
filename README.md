# CCTV Human Detector

A local-first Windows CCTV monitor for RTSP/ONVIF cameras. It uses YOLO11 Nano to detect configured object classes, shows a live browser dashboard, and keeps searchable evidence locally.

## What it does

- Displays a live multi-camera wall with connection and active-event status.
- Detects people, animals, and other selected COCO classes.
- Records evidence clips, snapshots, timestamps, confidence, and object counts.
- Provides event filtering, video playback, camera settings, storage limits, and optional ONVIF PTZ controls.
- Supports Telegram, webhook, Tuya switch, and Raspberry Pi GPIO alarm actions.
- Keeps camera credentials, SQLite events, clips, and snapshots on the local machine.

## Run locally

### Windows / Python

1. Install Python 3.13 and FFmpeg.
2. Create and activate a virtual environment:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env`, then add camera RTSP URLs and any optional notifications. Do not commit `.env`.
4. Start the monitor:

   ```powershell
   python run.py
   ```

5. Open [http://127.0.0.1:10101](http://127.0.0.1:10101).

### Docker

```powershell
Copy-Item .env.example .env
docker compose up --build
```

Open [http://127.0.0.1:10101](http://127.0.0.1:10101).

## Camera setup

Enable RTSP in each camera's settings and use the camera-specific RTSP account in `.env`, for example:

```json
[
  {
    "id": "driveway",
    "name": "Driveway",
    "rtsp_url": "rtsp://USERNAME:PASSWORD@192.168.0.71:554/stream1",
    "detect_every_seconds": 1.0
  }
]
```

Use the dashboard's **Camera settings** page for ongoing camera, live-wall, storage, detection-rule, and proof-timezone settings.

## Data and privacy

The Windows package saves application data beneath `%LOCALAPPDATA%\CCTV Human Detector\data`. A Python run uses `DATA_DIR` (default: `./data`). This includes the SQLite database, clips, snapshots, thumbnails, and local model data. Camera credentials stay in `.env` or local settings and are excluded from Git.

## Build the Windows package

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean CCTV-Human-Detector.spec
```

The package is created at `dist\CCTV-Human-Detector\CCTV-Human-Detector.exe`. Copy the complete `CCTV-Human-Detector` directory; the executable requires its adjacent `_internal`, `app`, and `models` files.

## Build the installer

Build the package first, then compile the Inno Setup definition:

```powershell
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\CCTV-Human-Detector.iss
```

The installer is created at `release\CCTV-Human-Detector-Setup.exe`. It installs per-user and offers Start Menu and desktop shortcuts.

## Continuous builds

The GitHub Actions workflow builds the package and installer on every push, then uploads `CCTV-Human-Detector-Setup.exe` as a workflow artifact. Bundled FFmpeg runtime files are stored with Git LFS; clone with Git LFS enabled.

## Alerts

Configure optional values in `.env`:

- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` for Telegram notifications.
- `ALERT_WEBHOOK_URL` for a local webhook, such as a network siren.
- `TUYA_*` values for LAN-only Tuya/Smart Life switch control.
- `GPIO_PIN` for a Raspberry Pi GPIO alarm output.

## Notes

YOLO detection is sampled per camera to keep CPU use manageable. For larger camera walls, use substreams, reduce the detection frequency, or run on hardware with a supported GPU.
