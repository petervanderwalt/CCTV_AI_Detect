# CCTV human detector

Local-first CCTV monitor for RTSP/ONVIF cameras. It detects people, captures alarm clips and stills, stores an event catalogue in SQLite, can notify Telegram, and provides a browser dashboard for up to 16 cameras.

## Start

1. Install Docker Desktop (recommended) or Python 3.12 plus FFmpeg.
2. Copy `.env.example` to `.env`, then enter each camera RTSP URL. Keep credentials only in `.env`.
3. Start: `docker compose up --build`.
4. Open `http://localhost:10101`.

## Build the Windows EXE

From this project folder, with the virtual environment installed, run:

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean CCTV-Human-Detector.spec
```

The packaged application is created at `dist\CCTV-Human-Detector\CCTV-Human-Detector.exe`. Keep the complete `CCTV-Human-Detector` folder together when copying it to another PC; the executable depends on the adjacent `_internal`, `app`, and `models` files. Camera settings and event data are kept separately under the user's local application data folder.

## Build the Windows installer

Build the EXE first, then compile the Inno Setup definition:

```powershell
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\CCTV-Human-Detector.iss
```

The installer is written to `release\CCTV-Human-Detector-Setup.exe`. It installs the complete application folder under the current user's local Programs directory and adds Start Menu and optional desktop shortcuts.

GitHub Actions runs this same EXE-and-installer build on every push and uploads `CCTV-Human-Detector-Setup.exe` as a workflow artifact.

The first detection downloads the compact YOLO model to `models/`. Add up to 16 entries in `CAMERAS_JSON`. Tapo RTSP must be enabled in the camera's settings; ONVIF can be used to discover the stream URL, but the monitor consumes RTSP directly.

## Alerts

- Telegram: create a bot and set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
- Network siren: set `ALERT_WEBHOOK_URL`; the alarm event JSON is POSTed to it.
- Tuya/Smart Life Wi-Fi switch: set `TUYA_DEVICE_ID`, `TUYA_DEVICE_IP`, and `TUYA_LOCAL_KEY`. It uses LAN control and pulses the configured DPS (normally `1`) for `TUYA_ACTIVE_SECONDS`; no alarm traffic is sent through the Tuya cloud.
- Raspberry Pi GPIO: set `GPIO_PIN` only when the service runs on that Pi.

Event clips, snapshots, and `events.db` live beneath `data/`. The dashboard intentionally shows JPEG previews rather than direct browser RTSP; this avoids exposing camera credentials to the browser.

## Notes

Detection is sampled per camera to control CPU. Tune `detect_every_seconds`, `PERSON_CONFIDENCE`, and cooldown after observing real scenes. For 16 HD cameras, use a machine with a supported GPU or reduce camera substream resolution/frame rate.
