import asyncio
import json
import base64
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import quote, unquote, urlparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from collections import Counter, deque
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import cv2
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

FROZEN = getattr(sys, "frozen", False)
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path.cwd()
load_dotenv(APP_DIR / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("cctv")
DEFAULT_PROOF_TIMEZONE = "Africa/Johannesburg"

BASE = Path(os.getenv("DATA_DIR", str(Path(os.getenv("LOCALAPPDATA", APP_DIR)) / "CCTV Human Detector" / "data") if FROZEN else "data"))
SNAPSHOTS, CLIPS = BASE / "snapshots", BASE / "clips"
DB = BASE / "events.db"
CAMERA_CONFIG = BASE / "cameras.json"
STORAGE_CONFIG = BASE / "storage.json"
DETECTION_CONFIG = BASE / "detection.json"
STARTED_AT = time.monotonic()
TRACKED_CLASSES = {0:"human",1:"bicycle",2:"car",3:"motorcycle",4:"airplane",5:"bus",6:"train",7:"truck",8:"boat",9:"traffic light",10:"fire hydrant",11:"stop sign",12:"parking meter",13:"bench",14:"bird",15:"cat",16:"dog",17:"horse",18:"sheep",19:"cow",20:"elephant",21:"bear",22:"zebra",23:"giraffe",24:"backpack",25:"umbrella",26:"handbag",27:"tie",28:"suitcase",29:"frisbee",30:"skis",31:"snowboard",32:"sports ball",33:"kite",34:"baseball bat",35:"baseball glove",36:"skateboard",37:"surfboard",38:"tennis racket",39:"bottle",40:"wine glass",41:"cup",42:"fork",43:"knife",44:"spoon",45:"bowl",46:"banana",47:"apple",48:"sandwich",49:"orange",50:"broccoli",51:"carrot",52:"hot dog",53:"pizza",54:"donut",55:"cake",56:"chair",57:"couch",58:"potted plant",59:"bed",60:"dining table",61:"toilet",62:"tv",63:"laptop",64:"mouse",65:"remote",66:"keyboard",67:"cell phone",68:"microwave",69:"oven",70:"toaster",71:"sink",72:"refrigerator",73:"book",74:"clock",75:"vase",76:"scissors",77:"teddy bear",78:"hair drier",79:"toothbrush"}
DEFAULT_RECORD_TAGS = ["human", "bird", "cat", "dog", "horse", "sheep", "cow"]
for path in (SNAPSHOTS, CLIPS):
    path.mkdir(parents=True, exist_ok=True)
# Avoid writing model settings under the Windows user profile; this service owns its data.
os.environ.setdefault("YOLO_CONFIG_DIR", str(BASE / "ultralytics"))
from ultralytics import YOLO


@dataclass(frozen=True)
class Camera:
    id: str
    name: str
    rtsp_url: str
    detect_every_seconds: float = 1.0
    order: int = 0
    ptz_enabled: bool = False
    onvif_port: int = 2020


def cameras() -> list[Camera]:
    raw = CAMERA_CONFIG.read_text(encoding="utf-8") if CAMERA_CONFIG.exists() else os.getenv("CAMERAS_JSON", "[]")
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError("CAMERAS_JSON is not valid JSON") from error
    if len(items) > 16:
        raise RuntimeError("A maximum of 16 cameras is supported")
    return [Camera(**{**item, "order": item.get("order", index)}) for index, item in enumerate(items, start=1)]


class CameraSettings(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    name: str = Field(min_length=1, max_length=80)
    order: int = Field(default=1, ge=1, le=999)
    ip: str = Field(min_length=1, max_length=255)
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=255)
    stream_path: str = Field(default="stream1", min_length=1, max_length=255)
    detect_every_seconds: float = Field(default=1.0, ge=0.2, le=60)
    ptz_enabled: bool = False
    onvif_port: int = Field(default=2020, ge=1, le=65535)


class StorageSettings(BaseModel):
    max_gb: float = Field(default=50, ge=0.1, le=2000)
    reserve_free_gb: float = Field(default=5, ge=0, le=1000)
    proof_timezone: str = Field(default=DEFAULT_PROOF_TIMEZONE, min_length=1, max_length=80)


class DetectionSettings(BaseModel):
    record_tags: list[str] = Field(default_factory=lambda: list(DEFAULT_RECORD_TAGS))
    alarm_tags: list[str] = Field(default_factory=lambda: ["human"])

    @field_validator("record_tags", "alarm_tags")
    @classmethod
    def supported_tags(cls, tags: list[str]) -> list[str]:
        tags = list(dict.fromkeys(tags))
        unsupported = set(tags) - set(TRACKED_CLASSES.values())
        if unsupported:
            raise ValueError(f"Unsupported model classes: {', '.join(sorted(unsupported))}")
        return tags


def settings_from_camera(camera: Camera) -> dict[str, Any]:
    parsed = urlparse(camera.rtsp_url)
    return {"id": camera.id, "name": camera.name, "order": camera.order, "ip": parsed.hostname or "", "username": unquote(parsed.username or ""), "password": unquote(parsed.password or ""), "stream_path": parsed.path.lstrip("/") or "stream1", "detect_every_seconds": camera.detect_every_seconds, "ptz_enabled": camera.ptz_enabled, "onvif_port": camera.onvif_port}


def camera_from_settings(item: CameraSettings) -> Camera:
    return Camera(id=item.id, name=item.name, order=item.order, rtsp_url=f"rtsp://{quote(item.username, safe='')}:{quote(item.password, safe='')}@{item.ip}:554/{item.stream_path.lstrip('/')}", detect_every_seconds=item.detect_every_seconds, ptz_enabled=item.ptz_enabled, onvif_port=item.onvif_port)


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with db() as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, camera_id TEXT NOT NULL, camera_name TEXT NOT NULL,
          created_at TEXT NOT NULL, confidence REAL NOT NULL, snapshot_path TEXT NOT NULL,
          clip_path TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0
        )""")
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(events)")}
        if "tags" not in columns:
            connection.execute("ALTER TABLE events ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
        if "object_counts" not in columns:
            connection.execute("ALTER TABLE events ADD COLUMN object_counts TEXT NOT NULL DEFAULT '{}'")


def storage_settings() -> StorageSettings:
    if not STORAGE_CONFIG.exists():
        return StorageSettings()
    try:
        return StorageSettings.model_validate_json(STORAGE_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        LOG.exception("Invalid storage settings; using defaults")
        return StorageSettings()


def configured_proof_timezone(name: str = DEFAULT_PROOF_TIMEZONE) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        LOG.warning("Unknown proof timezone %r; using %s", name, DEFAULT_PROOF_TIMEZONE)
        return ZoneInfo(DEFAULT_PROOF_TIMEZONE)


PROOF_TIMEZONE = configured_proof_timezone(storage_settings().proof_timezone)


def detection_settings() -> DetectionSettings:
    if not DETECTION_CONFIG.exists():
        return DetectionSettings()
    try:
        return DetectionSettings.model_validate_json(DETECTION_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        LOG.exception("Invalid detection settings; using defaults")
        return DetectionSettings()


DETECTION_SETTINGS = detection_settings()


def detection_class_ids() -> list[int]:
    enabled_tags = set(DETECTION_SETTINGS.record_tags) | set(DETECTION_SETTINGS.alarm_tags)
    return [class_id for class_id, label in TRACKED_CLASSES.items() if label in enabled_tags]


def event_media_bytes() -> int:
    total = 0
    with db() as connection:
        rows = connection.execute("SELECT id, snapshot_path, clip_path FROM events").fetchall()
    for row in rows:
        for raw_path in (row["snapshot_path"], row["clip_path"]):
            path = Path(raw_path)
            if path.is_file():
                total += path.stat().st_size
        thumbnail = BASE / "thumbnails" / f"{row['id']}.jpg"
        if thumbnail.is_file():
            total += thumbnail.stat().st_size
    return total


def cleanup_storage() -> int:
    """Remove the oldest event evidence until the retention cap and free-space reserve are met."""
    settings = storage_settings()
    maximum = int(settings.max_gb * 1024 ** 3)
    reserve = int(settings.reserve_free_gb * 1024 ** 3)
    deleted = 0
    with db() as connection:
        rows = connection.execute("SELECT id, snapshot_path, clip_path FROM events ORDER BY created_at ASC").fetchall()
        used = event_media_bytes()
        free = shutil.disk_usage(BASE).free
        for row in rows:
            if used <= maximum and free >= reserve:
                break
            removed = 0
            for raw_path in (row["snapshot_path"], row["clip_path"]):
                path = Path(raw_path)
                if path.is_file():
                    removed += path.stat().st_size
                    path.unlink()
            thumbnail = BASE / "thumbnails" / f"{row['id']}.jpg"
            if thumbnail.is_file():
                removed += thumbnail.stat().st_size
                thumbnail.unlink()
            connection.execute("DELETE FROM events WHERE id = ?", (row["id"],))
            used -= removed
            free += removed
            deleted += 1
    if deleted:
        LOG.warning("Storage retention removed %d old events", deleted)
    return deleted


def storage_status() -> dict[str, Any]:
    settings = storage_settings()
    disk = shutil.disk_usage(BASE)
    return {"max_gb": settings.max_gb, "reserve_free_gb": settings.reserve_free_gb,
            "proof_timezone": settings.proof_timezone, "used_bytes": event_media_bytes(), "free_bytes": disk.free, "total_bytes": disk.total}


class Monitor:
    def __init__(self, camera: Camera):
        self.camera = camera
        self.last_frame: bytes | None = None
        self.frame_sequence = 0
        self.frame_ready = threading.Condition()
        self.detected_boxes: list[tuple[int, int, int, int, float, str]] = []
        self.frame_times: deque[float] = deque()
        self.processed_frames = 0
        self.detection_frames = 0
        self.people_detected = 0
        self.inference_ms_total = 0.0
        self.last_inference_ms = 0.0
        self.last_seen: str | None = None
        self.last_alarm = 0.0
        self.recording = False
        self.event_active_until = 0.0
        self.active_tags: list[str] = []
        self.running = True
        self.model: YOLO | None = None

    def start(self) -> None:
        threading.Thread(target=self.run, name=f"camera-{self.camera.id}", daemon=True).start()

    def run(self) -> None:
        cap = None
        next_detection = 0.0
        next_preview = 0.0
        while self.running:
            if cap is None or not cap.isOpened():
                cap = cv2.VideoCapture(self.camera.rtsp_url, cv2.CAP_FFMPEG)
                if not cap.isOpened():
                    LOG.warning("Cannot connect to %s; retrying", self.camera.name)
                    time.sleep(5)
                    continue
            ok, frame = cap.read()
            if not ok:
                cap.release(); cap = None
                continue
            self.last_seen = datetime.now(timezone.utc).isoformat()
            now = time.monotonic()
            with self.frame_ready:
                self.processed_frames += 1
                self.frame_times.append(now)
                while self.frame_times and self.frame_times[0] < now - 60:
                    self.frame_times.popleft()
            if now >= next_detection:
                next_detection = now + self.camera.detect_every_seconds
                self.detect(frame)
            if now >= next_preview:
                next_preview = now + 0.5  # 2 fps keeps a 16-camera wall usable on a LAN.
                preview = cv2.resize(frame, (640, 360))
                scale_x, scale_y = 640 / frame.shape[1], 360 / frame.shape[0]
                with self.frame_ready: boxes = list(self.detected_boxes)
                for x1, y1, x2, y2, confidence, label in boxes:
                    left, top, right, bottom = int(x1 * scale_x), int(y1 * scale_y), int(x2 * scale_x), int(y2 * scale_y)
                    cv2.rectangle(preview, (left, top), (right, bottom), (220, 40, 230), 2)
                    caption = f"{label.upper()} {confidence:.0%}"
                    cv2.rectangle(preview, (left, max(0, top - 21)), (left + 112, top), (220, 40, 230), -1)
                    cv2.putText(preview, caption, (left + 3, max(15, top - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (255, 255, 255), 1, cv2.LINE_AA)
                draw_proof_timestamp(preview, proof_timestamp())
                _, encoded = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 75])
                with self.frame_ready:
                    self.last_frame = encoded.tobytes()
                    self.frame_sequence += 1
                    self.frame_ready.notify_all()
            if now >= next_detection:
                next_detection = now + self.camera.detect_every_seconds
                self.detect(frame)

    def detect(self, frame: Any) -> None:
        if self.model is None:
            model_path = os.getenv("MODEL_PATH", str(RESOURCE_DIR / "models" / "yolo11n.pt") if FROZEN else "models/yolo11n.pt")
            self.model = YOLO(model_path)
        started = time.perf_counter()
        results = self.model(frame, classes=detection_class_ids(), conf=float(os.getenv("OBJECT_CONFIDENCE", os.getenv("PERSON_CONFIDENCE", "0.55"))), verbose=False)
        inference_ms = (time.perf_counter() - started) * 1000
        objects = [(int(box.xyxy[0][0]), int(box.xyxy[0][1]), int(box.xyxy[0][2]), int(box.xyxy[0][3]), float(box.conf[0]), TRACKED_CLASSES[int(box.cls[0])]) for box in results[0].boxes]
        with self.frame_ready:
            self.detected_boxes = objects
            if objects:
                self.event_active_until = time.monotonic() + 3.0
                self.active_tags = sorted({object_[5] for object_ in objects})
            self.detection_frames += 1
            self.people_detected += sum(label == "human" for *_, label in objects)
            self.inference_ms_total += inference_ms
            self.last_inference_ms = inference_ms
        confidence = max((object_[4] for object_ in objects), default=0.0)
        if confidence and not self.recording and time.monotonic() - self.last_alarm >= int(os.getenv("ALARM_COOLDOWN_SECONDS", "45")):
            self.last_alarm = time.monotonic()
            self.alarm(frame, confidence, sorted({object_[5] for object_ in objects}))

    def alarm(self, frame: Any, confidence: float, tags: list[str]) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        evidence_time = proof_timestamp()
        stem = f"{self.camera.id}-{stamp}"
        snapshot = SNAPSHOTS / f"{stem}.jpg"
        clip = CLIPS / f"{stem}.mp4"
        with self.frame_ready: boxes = list(self.detected_boxes)
        cv2.imwrite(str(snapshot), annotate_evidence(frame, boxes, evidence_time))
        seconds = os.getenv("CLIP_SECONDS", "20")
        with self.frame_ready:
            self.event_active_until = time.monotonic() + float(seconds)
            self.active_tags = tags
        self.recording = True
        threading.Thread(target=self.record_event_clip, args=(clip, seconds, detection_class_ids()), daemon=True).start()
        object_counts = dict(Counter(label for *_, label in boxes))
        event = {"camera_id": self.camera.id, "camera_name": self.camera.name, "created_at": datetime.now(timezone.utc).isoformat(), "confidence": confidence, "snapshot_path": str(snapshot), "clip_path": str(clip), "tags": json.dumps(tags), "object_counts": json.dumps(object_counts)}
        with db() as connection:
            cursor = connection.execute("INSERT INTO events(camera_id,camera_name,created_at,confidence,snapshot_path,clip_path,tags,object_counts) VALUES(:camera_id,:camera_name,:created_at,:confidence,:snapshot_path,:clip_path,:tags,:object_counts)", event)
            event["id"] = cursor.lastrowid
        LOG.warning("Object event: %s (%s, %.0f%%)", self.camera.name, ", ".join(tags), confidence * 100)
        if set(tags) & set(DETECTION_SETTINGS.alarm_tags):
            threading.Thread(target=notify, args=(event,), daemon=True).start()

    def record_event_clip(self, clip: Path, seconds: str, class_ids: list[int]) -> None:
        try:
            record_clip(self.camera.rtsp_url, clip, seconds, class_ids)
        finally:
            self.recording = False


def proof_timestamp() -> str:
    return datetime.now(PROOF_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %Z")


def draw_proof_timestamp(frame: Any, timestamp: str) -> None:
    """Replace camera-supplied clock text with a readable local proof timestamp."""
    origin = (12, 28)
    cv2.putText(frame, timestamp, origin, cv2.FONT_HERSHEY_SIMPLEX, 0.68, (0, 0, 0), 5, cv2.LINE_AA)
    cv2.putText(frame, timestamp, origin, cv2.FONT_HERSHEY_SIMPLEX, 0.68, (255, 255, 255), 2, cv2.LINE_AA)


def annotate_evidence(frame: Any, boxes: list[tuple[int, int, int, int, float, str]], timestamp: str) -> Any:
    evidence = frame.copy()
    for x1, y1, x2, y2, confidence, label in boxes:
        cv2.rectangle(evidence, (x1, y1), (x2, y2), (220, 40, 230), 4)
        cv2.putText(evidence, f"{label.upper()} {confidence:.0%}", (x1, max(30, y1 - 10)), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2, cv2.LINE_AA)
    draw_proof_timestamp(evidence, timestamp)
    return evidence


def record_clip(url: str, destination: Path, seconds: str, class_ids: list[int]) -> None:
    """Record evidence while refreshing person boxes throughout the clip."""
    bundled_ffmpeg = RESOURCE_DIR / "app" / "ffmpeg" / "ffmpeg.exe"
    ffmpeg = os.getenv("FFMPEG_PATH", str(bundled_ffmpeg) if FROZEN else "ffmpeg")
    minimum_duration = float(seconds)
    maximum_duration = max(minimum_duration, float(os.getenv("MAX_CLIP_SECONDS", "120")))
    post_event_seconds = float(os.getenv("POST_EVENT_SECONDS", "4"))
    output_fps = float(os.getenv("CLIP_FPS", "10"))
    detect_interval = 1 / float(os.getenv("CLIP_TRACK_DETECT_FPS", "4"))
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        LOG.warning("Cannot open camera stream for tracked clip: %s", destination.name)
        return
    ok, frame = cap.read()
    if not ok:
        cap.release()
        LOG.warning("Camera stream ended before tracked clip began: %s", destination.name)
        return
    height, width = frame.shape[:2]
    command = [ffmpeg, "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}", "-r", str(output_fps), "-i", "-", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-movflags", "+faststart", str(destination)]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    model_path = os.getenv("MODEL_PATH", str(RESOURCE_DIR / "models" / "yolo11n.pt") if FROZEN else "models/yolo11n.pt")
    model = YOLO(model_path)
    started_at = time.monotonic()
    minimum_finish = started_at + minimum_duration
    finish_at = started_at + maximum_duration
    last_detection = started_at
    next_output, next_detection = 0.0, 0.0
    boxes: list[tuple[int, int, int, int, float, str]] = []
    try:
        while time.monotonic() < finish_at:
            ok, frame = cap.read()
            if not ok:
                break
            now = time.monotonic()
            if now >= next_detection:
                next_detection = now + detect_interval
                results = model(frame, classes=class_ids, conf=float(os.getenv("OBJECT_CONFIDENCE", os.getenv("PERSON_CONFIDENCE", "0.55"))), verbose=False)
                boxes = [(int(box.xyxy[0][0]), int(box.xyxy[0][1]), int(box.xyxy[0][2]), int(box.xyxy[0][3]), float(box.conf[0]), TRACKED_CLASSES[int(box.cls[0])]) for box in results[0].boxes]
                if boxes:
                    last_detection = time.monotonic()
            if now >= next_output:
                next_output = now + 1 / output_fps
                timestamp = proof_timestamp()
                assert process.stdin is not None
                process.stdin.write(annotate_evidence(frame, boxes, timestamp).tobytes())
            if now >= minimum_finish and now - last_detection >= post_event_seconds:
                break
    except (BrokenPipeError, cv2.error):
        LOG.exception("Tracked clip recording failed: %s", destination.name)
    finally:
        cap.release()
        if process.stdin:
            process.stdin.close()
        process.wait(timeout=30)


def notify(event: dict[str, Any]) -> None:
    webhook = os.getenv("ALERT_WEBHOOK_URL")
    if webhook:
        try: requests.request(os.getenv("ALERT_WEBHOOK_METHOD", "POST"), webhook, json=event, timeout=5)
        except requests.RequestException: LOG.exception("Alarm webhook failed")
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat_id:
        try:
            with open(event["snapshot_path"], "rb") as image:
                requests.post(f"https://api.telegram.org/bot{token}/sendPhoto", data={"chat_id": chat_id, "caption": f"Person detected: {event['camera_name']} ({event['confidence']:.0%})"}, files={"photo": image}, timeout=15)
        except requests.RequestException: LOG.exception("Telegram notification failed")
    pin = os.getenv("GPIO_PIN")
    if pin:
        try:
            from gpiozero import OutputDevice
            device = OutputDevice(int(pin)); device.on(); time.sleep(float(os.getenv("GPIO_ACTIVE_SECONDS", "10"))); device.off()
        except Exception: LOG.exception("GPIO alert failed")
    if os.getenv("TUYA_DEVICE_ID"):
        tuya_pulse()


def tuya_pulse() -> None:
    """Pulse a local Tuya outlet/switch without putting its secrets in event records."""
    try:
        import tinytuya
        device = tinytuya.OutletDevice(
            dev_id=os.environ["TUYA_DEVICE_ID"],
            address=os.environ["TUYA_DEVICE_IP"],
            local_key=os.environ["TUYA_LOCAL_KEY"],
            version=float(os.getenv("TUYA_PROTOCOL_VERSION", "3.3")),
        )
        dps = int(os.getenv("TUYA_SWITCH_DPS", "1"))
        device.set_value(dps, True)
        time.sleep(float(os.getenv("TUYA_ACTIVE_SECONDS", "10")))
        device.set_value(dps, False)
    except Exception:
        LOG.exception("Tuya alert failed")


app = FastAPI(title="CCTV Human Detector")
monitors: dict[str, Monitor] = {}

@app.on_event("startup")
async def startup() -> None:
    init_db()
    cleanup_storage()
    apply_cameras(cameras())
    threading.Thread(target=storage_maintenance, daemon=True, name="storage-maintenance").start()


def storage_maintenance() -> None:
    while True:
        time.sleep(60)
        try:
            cleanup_storage()
        except Exception:
            LOG.exception("Storage maintenance failed")


def apply_cameras(camera_list: list[Camera]) -> None:
    for monitor in monitors.values(): monitor.running = False
    monitors.clear()
    for camera in sorted(camera_list, key=lambda item: item.order):
        monitor = Monitor(camera); monitors[camera.id] = monitor; monitor.start()

@app.get("/api/cameras")
def list_cameras():
    pending: dict[str, list[str]] = {}
    stale_after = max(float(os.getenv("CLIP_SECONDS", "20")) * 3, float(os.getenv("MAX_CLIP_SECONDS", "120")) * 1.5, 90)
    cutoff = datetime.now(timezone.utc).timestamp() - stale_after
    with db() as connection:
        rows = connection.execute("SELECT camera_id, tags, clip_path, created_at FROM events ORDER BY id DESC").fetchall()
    for row in rows:
        if row["camera_id"] in pending:
            continue
        clip = Path(row["clip_path"])
        try:
            created = datetime.fromisoformat(row["created_at"]).timestamp()
        except ValueError:
            created = 0
        if created >= cutoff and (not clip.exists() or clip.stat().st_size <= 1024):
            try:
                pending[row["camera_id"]] = json.loads(row["tags"] or "[]")
            except json.JSONDecodeError:
                pending[row["camera_id"]] = []
    now = time.monotonic()
    return [{"id": monitor.camera.id, "name": monitor.camera.name, "last_seen": monitor.last_seen, "online": monitor.last_frame is not None, "ptz_enabled": monitor.camera.ptz_enabled,
             "event_active": monitor.event_active_until > now or monitor.camera.id in pending,
             "active_tags": monitor.active_tags if monitor.event_active_until > now else pending.get(monitor.camera.id, [])} for monitor in monitors.values()]


@app.get("/api/storage")
def get_storage():
    return storage_status()


@app.put("/api/storage")
def put_storage(settings: StorageSettings):
    try:
        proof_timezone = ZoneInfo(settings.proof_timezone)
    except ZoneInfoNotFoundError:
        raise HTTPException(422, "Use an IANA timezone name such as Africa/Johannesburg")
    STORAGE_CONFIG.write_text(settings.model_dump_json(indent=2), encoding="utf-8")
    global PROOF_TIMEZONE
    PROOF_TIMEZONE = proof_timezone
    deleted = cleanup_storage()
    return {**storage_status(), "deleted_events": deleted}


@app.get("/api/settings/detection")
def get_detection_settings():
    return {**DETECTION_SETTINGS.model_dump(), "classes": list(TRACKED_CLASSES.values())}


@app.put("/api/settings/detection")
def put_detection_settings(settings: DetectionSettings):
    global DETECTION_SETTINGS
    DETECTION_SETTINGS = settings
    DETECTION_CONFIG.write_text(settings.model_dump_json(indent=2), encoding="utf-8")
    return {**settings.model_dump(), "classes": list(TRACKED_CLASSES.values())}


@app.get("/api/stats")
def get_stats():
    now = time.monotonic()
    with db() as connection:
        total_events = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        today_events = connection.execute("SELECT COUNT(*) FROM events WHERE created_at >= ?", (datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),)).fetchone()[0]
    frames_per_minute = detection_frames = people_detected = 0
    inference_total = 0.0
    online = 0
    for monitor in monitors.values():
        with monitor.frame_ready:
            while monitor.frame_times and monitor.frame_times[0] < now - 60:
                monitor.frame_times.popleft()
            frames_per_minute += len(monitor.frame_times)
            detection_frames += monitor.detection_frames
            people_detected += monitor.people_detected
            inference_total += monitor.inference_ms_total
            online += int(monitor.last_frame is not None)
    return {"online_cameras": online, "camera_count": len(monitors), "frames_per_minute": frames_per_minute,
            "detection_frames": detection_frames, "people_detected": people_detected,
            "average_inference_ms": round(inference_total / detection_frames, 1) if detection_frames else 0,
            "events_today": today_events, "events_total": total_events,
            "uptime_seconds": int(now - STARTED_AT)}


class PtzMove(BaseModel):
    direction: str = Field(pattern=r"^(up|down|left|right)$")

@app.post("/api/cameras/{camera_id}/ptz")
def move_ptz(camera_id: str, move: PtzMove):
    monitor = monitors.get(camera_id)
    if not monitor or not monitor.camera.ptz_enabled: raise HTTPException(404, "PTZ is not enabled for this camera")
    direction = {"up": (0, 0.45), "down": (0, -0.45), "left": (-0.45, 0), "right": (0.45, 0)}[move.direction]
    try:
        from onvif import ONVIFCamera
        parsed = urlparse(monitor.camera.rtsp_url)
        camera = ONVIFCamera(parsed.hostname, monitor.camera.onvif_port, unquote(parsed.username or ""), unquote(parsed.password or ""))
        profile = camera.create_media_service().GetProfiles()[0]
        ptz = camera.create_ptz_service()
        request = ptz.create_type("ContinuousMove")
        request.ProfileToken = profile.token
        request.Velocity = {"PanTilt": {"x": direction[0], "y": direction[1]}}
        ptz.ContinuousMove(request)
        time.sleep(0.35)
        ptz.Stop({"ProfileToken": profile.token, "PanTilt": True})
    except Exception as error:
        LOG.exception("PTZ command failed for %s", monitor.camera.name)
        raise HTTPException(502, f"ONVIF PTZ command failed: {error}")
    return {"ok": True}

@app.get("/api/settings/cameras")
def get_camera_settings():
    return [settings_from_camera(monitor.camera) for monitor in monitors.values()]

@app.put("/api/settings/cameras")
def save_camera_settings(items: list[CameraSettings]):
    if len(items) > 16: raise HTTPException(422, "A maximum of 16 cameras is supported")
    if len({item.id for item in items}) != len(items): raise HTTPException(422, "Camera IDs must be unique")
    camera_list = [camera_from_settings(item) for item in sorted(items, key=lambda item: item.order)]
    CAMERA_CONFIG.write_text(json.dumps([camera.__dict__ for camera in camera_list], indent=2), encoding="utf-8")
    apply_cameras(camera_list)
    return {"ok": True, "count": len(camera_list)}

@app.get("/api/cameras/{camera_id}/preview.jpg")
def preview(camera_id: str):
    monitor = monitors.get(camera_id)
    if not monitor or not monitor.last_frame: raise HTTPException(404, "Camera preview unavailable")
    path = BASE / f"preview-{camera_id}.jpg"; path.write_bytes(monitor.last_frame)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

def mjpeg_frames(monitor: Monitor):
    sequence = -1
    while monitor.running:
        with monitor.frame_ready:
            monitor.frame_ready.wait_for(lambda: monitor.frame_sequence != sequence or not monitor.running, timeout=15)
            sequence, frame = monitor.frame_sequence, monitor.last_frame
        if frame:
            yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n"

@app.get("/api/cameras/{camera_id}/live.mjpg")
def live_preview(camera_id: str):
    monitor = monitors.get(camera_id)
    if not monitor: raise HTTPException(404, "Camera not found")
    return StreamingResponse(mjpeg_frames(monitor), media_type="multipart/x-mixed-replace; boundary=frame", headers={"Cache-Control": "no-store, no-cache"})


@app.websocket("/api/live")
async def live_websocket(websocket: WebSocket):
    """One shared live-feed connection per dashboard tab, rather than one HTTP stream per camera."""
    await websocket.accept()
    sequences: dict[str, int] = {}
    try:
        while True:
            frames: dict[str, str] = {}
            for camera_id, monitor in monitors.items():
                with monitor.frame_ready:
                    sequence, frame = monitor.frame_sequence, monitor.last_frame
                if frame and sequence != sequences.get(camera_id):
                    sequences[camera_id] = sequence
                    frames[camera_id] = base64.b64encode(frame).decode("ascii")
            if frames:
                await websocket.send_json({"frames": frames})
            await asyncio.sleep(0.12)
    except WebSocketDisconnect:
        pass

@app.get("/api/events")
def events():
    with db() as connection:
        result = [dict(row) for row in connection.execute("SELECT * FROM events ORDER BY id DESC LIMIT 200")]
    for event in result:
        try:
            event["tags"] = json.loads(event.get("tags") or "[]")
        except json.JSONDecodeError:
            event["tags"] = []
        try:
            event["object_counts"] = json.loads(event.get("object_counts") or "{}")
        except json.JSONDecodeError:
            event["object_counts"] = {}
        event["clip_ready"] = Path(event["clip_path"]).exists() and Path(event["clip_path"]).stat().st_size > 1024
        try:
            age = datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(event["created_at"]).timestamp()
        except ValueError:
            age = 999999
        event["recording"] = not event["clip_ready"] and age < max(float(os.getenv("CLIP_SECONDS", "20")) * 3, float(os.getenv("MAX_CLIP_SECONDS", "120")) * 1.5, 90)
    return result

@app.get("/api/events/{event_id}/snapshot")
def event_snapshot(event_id: int):
    with db() as connection: row = connection.execute("SELECT snapshot_path FROM events WHERE id=?", (event_id,)).fetchone()
    if not row: raise HTTPException(404, "Event not found")
    return FileResponse(row["snapshot_path"], media_type="image/jpeg")

@app.get("/api/events/{event_id}/thumbnail")
def event_thumbnail(event_id: int):
    with db() as connection: row = connection.execute("SELECT snapshot_path FROM events WHERE id=?", (event_id,)).fetchone()
    if not row or not Path(row["snapshot_path"]).exists(): raise HTTPException(404, "Snapshot unavailable")
    thumb = BASE / "thumbnails" / f"{event_id}.jpg"
    thumb.parent.mkdir(parents=True, exist_ok=True)
    source = Path(row["snapshot_path"])
    if not thumb.exists() or thumb.stat().st_mtime < source.stat().st_mtime:
        image = cv2.imread(str(source))
        if image is None: raise HTTPException(500, "Snapshot could not be read")
        height, width = image.shape[:2]
        scale = min(480 / width, 270 / height)
        resized = cv2.resize(image, (round(width * scale), round(height * scale)))
        cv2.imwrite(str(thumb), resized, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return FileResponse(thumb, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=3600"})

@app.get("/api/events/{event_id}/clip")
def event_clip(event_id: int):
    with db() as connection: row = connection.execute("SELECT clip_path FROM events WHERE id=?", (event_id,)).fetchone()
    if not row or not Path(row["clip_path"]).exists(): raise HTTPException(404, "Clip is still recording or unavailable")
    return FileResponse(row["clip_path"], media_type="video/mp4")

@app.get("/assets/camera-offline.png")
def camera_offline_asset():
    return FileResponse(RESOURCE_DIR / "app" / "static" / "camera-offline.png", media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

@app.get("/")
def dashboard():
    page = (RESOURCE_DIR / "app" / "static" / "index.html").read_text(encoding="utf-8")
    stats_script = """<script>showStats=s=>{let a=[['Model','YOLO11 Nano'],['Confidence','55%'],['Clip tracking','4 / second'],['Cameras online',`${s.online_cameras} / ${s.camera_count}`],['Frames / minute',s.frames_per_minute.toLocaleString()],['AI inference',`${s.average_inference_ms} ms`],['People seen',s.people_detected.toLocaleString()],['Alarms today',s.events_today],['Uptime',`${Math.floor(s.uptime_seconds/60)}m`]];stats.innerHTML=a.map(([k,v])=>`<article class=stat><span>${k}</span><b>${v}</b></article>`).join('')}</script>"""
    confidence_script = """<script>card=e=>{let tags=(e.tags||[]).map(t=>`<span class="tag ${t}">${t}</span>`).join(''),counts=Object.entries(e.object_counts||{}).map(([t,n])=>`${n} ${n===1?t:t==='human'?'people':t+'s'}`).join(' · '),confidence=Number.isFinite(e.confidence)?`<div class=counts>${Math.round(e.confidence*100)}% confidence</div>`:'';return `<article class=event><img src="/api/events/${e.id}/thumbnail" onerror="this.onerror=null;this.src='/assets/camera-offline.png'"><div class=eventTitle>${esc(e.camera_name)}</div><div class=eventTime>${new Date(e.created_at).toLocaleString()}</div><div>${tags||'<span class=tag>legacy</span>'}</div>${confidence}${counts?`<div class=counts>${counts}</div>`:''}${e.clip_ready?`<button onclick="playClip(${e.id})">Play video</button>`:'<div class="counts" style="color:#ffca58">Recording video…</div>'}</article>`}</script>"""
    active_event_script = """<style>.cam.activeEvent{border-color:#fb5870!important;box-shadow:0 0 0 1px #fb5870,0 0 22px #fb587066;animation:eventPulse 1.15s ease-in-out infinite}.eventLive{margin-left:auto;background:#b52f42;color:white;padding:4px 7px;border-radius:999px;font-size:10px;letter-spacing:.04em;font-weight:800}@keyframes eventPulse{50%{box-shadow:0 0 0 1px #ff8191,0 0 30px #fb5870aa}}</style><script>wall=cs=>{let host=location.hostname==='127.0.0.1'?'localhost':'127.0.0.1';cameras.innerHTML=cs.map(c=>`<article class="cam ${c.event_active?'activeEvent':''}"><img src="http://${host}:10101/api/cameras/${c.id}/live.mjpg" onerror="this.onerror=null;this.src='/assets/camera-offline.png'"><div class=camFooter>${esc(c.name)}<i class="${c.online?'online':'online offline'}"></i>${c.event_active?`<span class=eventLive>EVENT · ${(c.active_tags||[]).join(', ')}</span>`:''}${c.ptz_enabled?`<div class=ptz><button onclick="ptz('${c.id}','up')">↑</button><button onclick="ptz('${c.id}','left')">←</button><button onclick="ptz('${c.id}','right')">→</button><button onclick="ptz('${c.id}','down')">↓</button></div>`:''}</div></article>`).join('')}</script>"""
    active_refresh_script = """<script>setInterval(async()=>{let cs=await fetch('/api/cameras').then(r=>r.json());cs.forEach(c=>{let card=[...document.querySelectorAll('#cameras .cam')].find(x=>x.querySelector('img')?.src.includes(`/cameras/${c.id}/`));if(!card)return;card.classList.toggle('activeEvent',c.event_active);let badge=card.querySelector('.eventLive');if(c.event_active&&!badge){badge=document.createElement('span');badge.className='eventLive';card.querySelector('.camFooter').insertBefore(badge,card.querySelector('.ptz'))}if(badge){badge.textContent=`EVENT · ${(c.active_tags||[]).join(', ')}`;if(!c.event_active)badge.remove()}})},1000)</script>"""
    websocket_wall_script = """<script>(()=>{let socket;const connect=()=>{if(socket&&(socket.readyState===WebSocket.OPEN||socket.readyState===WebSocket.CONNECTING))return;socket=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/api/live`);socket.onmessage=e=>{for(const[id,jpeg]of Object.entries(JSON.parse(e.data).frames||{})){let image=document.querySelector(`#cameras img[data-camera="${id}"]`);if(image)image.src=`data:image/jpeg;base64,${jpeg}`}};socket.onclose=()=>setTimeout(connect,1000)};wall=cs=>{cameras.innerHTML=cs.map(c=>`<article class="cam ${c.event_active?'activeEvent':''}"><img data-camera="${c.id}" src="/assets/camera-offline.png" alt="${esc(c.name)}"><div class=camFooter>${esc(c.name)}<i class="${c.online?'online':'online offline'}"></i>${c.event_active?`<span class=eventLive>EVENT · ${(c.active_tags||[]).join(', ')}</span>`:''}${c.ptz_enabled?`<div class=ptz><button onclick="ptz('${c.id}','up')">↑</button><button onclick="ptz('${c.id}','left')">←</button><button onclick="ptz('${c.id}','right')">→</button><button onclick="ptz('${c.id}','down')">↓</button></div>`:''}</div></article>`).join('');connect()};setInterval(async()=>{try{for(const c of await fetch('/api/cameras').then(r=>r.json())){let image=document.querySelector(`#cameras img[data-camera="${c.id}"]`),card=image?.closest('.cam');if(!card)continue;card.classList.toggle('activeEvent',!!c.event_active);let badge=card.querySelector('.eventLive');if(c.event_active&&!badge){badge=document.createElement('span');badge.className='eventLive';card.querySelector('.camFooter').append(badge)}if(badge){if(c.event_active)badge.textContent=`EVENT · ${(c.active_tags||[]).join(', ')}`;else badge.remove()}}}catch{}} ,1000);connect()})()</script>"""
    controls_script = """<style>#cameras{grid-template-columns:repeat(var(--grid-cols,4),minmax(0,1fr))}.wallConfig{margin-top:12px;padding-top:12px;border-top:1px solid #293241}.wallConfig select{margin-left:7px;background:#0e1219;color:#fff;border:1px solid #384356;border-radius:5px;padding:7px}</style><script>card=e=>{let tags=(e.tags||[]).map(t=>`<span class="tag ${t}">${t}</span>`).join(''),counts=Object.entries(e.object_counts||{}).map(([t,n])=>`${n} ${n===1?t:t==='human'?'people':t+'s'}`).join(' · '),confidence=Number.isFinite(e.confidence)?`<div class=counts>${Math.round(e.confidence*100)}% confidence</div>`:'';let status=e.clip_ready?`<button onclick="playClip(${e.id})">Play video</button>`:e.recording?'<div class="counts" style="color:#ffca58">Recording video…</div>':'<div class=counts>Clip unavailable</div>';return `<article class=event><img src="/api/events/${e.id}/thumbnail" onerror="this.onerror=null;this.src='/assets/camera-offline.png'"><div class=eventTitle>${esc(e.camera_name)}</div><div class=eventTime>${new Date(e.created_at).toLocaleString()}</div><div>${tags||'<span class=tag>legacy</span>'}</div>${confidence}${counts?`<div class=counts>${counts}</div>`:''}${status}</article>`};(()=>{let setGrid=v=>{v=Math.max(1,Math.min(6,+v||4));localStorage.setItem('cctvGridColumns',v);cameras.style.setProperty('--grid-cols',v)};setGrid(localStorage.getItem('cctvGridColumns')||4);let base=openSettings;openSettings=async()=>{await base();if(document.getElementById('wallColumns'))return;let config=document.createElement('div');config.className='wallConfig';config.innerHTML='<b>Live wall layout</b><p class=sub>Choose how many camera cards appear per row. Rows fill automatically.</p><label>Cards per row <select id=wallColumns><option>1</option><option>2</option><option>3</option><option>4</option><option>5</option><option>6</option></select></label>';config.querySelector('select').value=localStorage.getItem('cctvGridColumns')||4;config.querySelector('select').onchange=e=>setGrid(e.target.value);document.querySelector('#settingsBox').append(config)}})()</script>"""
    compact_layout_script = """<style>header{height:50px;padding:0 20px}.tabs button{padding:0 13px}main{padding-top:12px}#monitorView>.sectionHead{display:none}.stats{grid-template-columns:repeat(9,minmax(0,1fr));gap:6px;margin-top:10px}.stat{min-height:40px;padding:7px 9px;display:flex;align-items:center;justify-content:space-between;gap:6px}.stat span{font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.stat b{font-size:14px;margin:0;white-space:nowrap}#eventsView{height:calc(100dvh - 74px);display:flex;min-height:0;flex-direction:column}#eventsView>.sectionHead,.filterBar{flex:none}#events{min-height:0;overflow-y:auto;padding:0 8px 8px 0}@media(max-width:1200px){.stats{grid-template-columns:repeat(6,minmax(0,1fr))}}@media(max-width:700px){header{height:48px;padding:0 12px}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.stat{min-height:38px}#eventsView{height:calc(100dvh - 70px)}}</style>"""
    settings_actions_script = """<style>#settingsActions{display:flex;justify-content:flex-end;gap:9px;border-top:1px solid #293241;margin-top:18px;padding-top:14px}#settingsActions .cancel{background:#303949}</style><script>(()=>{let openBase=openSettings;openSettings=async()=>{await openBase();let box=document.querySelector('#settingsBox');box.querySelector('.sectionHead button')?.remove();[...box.querySelectorAll('button')].filter(b=>['Save and reconnect','Save storage'].includes(b.textContent.trim())).forEach(b=>b.remove());let wall=document.querySelector('#wallColumns');if(wall)wall.onchange=()=>{};let actions=document.querySelector('#settingsActions');if(!actions){actions=document.createElement('div');actions.id='settingsActions';actions.innerHTML='<button class="cancel" onclick="cancelSettings()">Cancel</button><button id="applySettingsButton" onclick="applySettings()">Apply changes</button>';box.append(actions)}};window.cancelSettings=()=>settingsPanel.classList.add('hidden');window.applySettings=async()=>{let button=document.querySelector('#applySettingsButton');button.disabled=true;try{let cameraItems=[...rows.children].map((row,index)=>{let item={id:row.dataset.id||`camera-${index+1}`,detect_every_seconds:1};row.querySelectorAll('input').forEach(input=>item[input.dataset.k]=input.type==='checkbox'?input.checked:input.value.trim());return item});let cameraResponse=await fetch('/api/settings/cameras',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(cameraItems)});if(!cameraResponse.ok)throw new Error(await cameraResponse.text());let storageResponse=await fetch('/api/storage',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({max_gb:+maxGb.value,reserve_free_gb:+reserveGb.value,proof_timezone:document.querySelector('#proofTimezone')?.value||'Africa/Johannesburg'})});if(!storageResponse.ok)throw new Error(await storageResponse.text());let layout=+document.querySelector('#wallColumns')?.value||4;localStorage.setItem('cctvGridColumns',layout);cameras.style.setProperty('--grid-cols',layout);settingsPanel.classList.add('hidden');refresh()}catch(error){alert(error.message||'Could not apply settings')}finally{button.disabled=false}}})()</script>"""
    modal_filter_script = """<style>#modalFilters{display:flex;gap:6px;align-items:center;margin-right:auto}#modalFilters select{max-width:145px;background:#0e1219;color:#fff;border:1px solid #384356;border-radius:5px;padding:6px;font:12px inherit}</style><script>(()=>{let baseOpenClip=openClip;let showModalFilters=()=>{let holder=document.querySelector('#modalFilters');if(!holder){holder=document.createElement('div');holder.id='modalFilters';document.querySelector('.playerTop').insertBefore(holder,playerStatus)}let cameraOptions=['all',...new Set(eventsData.map(event=>event.camera_name))].sort();let tagOptions=['all',...new Set(eventsData.flatMap(event=>event.tags||[]))].sort();holder.innerHTML=`<select id="modalCameraFilter" aria-label="Video camera filter">${cameraOptions.map(camera=>`<option value="${esc(camera)}" ${selectedCamera===camera?'selected':''}>${camera==='all'?'All cameras':esc(camera)}</option>`).join('')}</select><select id="modalTagFilter" aria-label="Video type filter">${tagOptions.map(tag=>`<option value="${esc(tag)}" ${selectedTag===tag?'selected':''}>${tag==='all'?'All types':esc(tag)}</option>`).join('')}</select>`;holder.querySelector('#modalCameraFilter').onchange=applyModalFilters;holder.querySelector('#modalTagFilter').onchange=applyModalFilters};window.applyModalFilters=()=>{selectedCamera=document.querySelector('#modalCameraFilter').value;selectedTag=document.querySelector('#modalTagFilter').value;renderEvents();if(clips.length)openClip(0);else closePlayer()};openClip=index=>{baseOpenClip(index);showModalFilters()}})()</script>"""
    timezone_settings_script = """<style>.proofTime{border-top:1px solid #293241;margin-top:18px;padding-top:14px}.proofTime input{width:230px;padding:7px;background:#0e1219;border:1px solid #384356;border-radius:5px;color:#fff;margin-left:8px}</style><script>(()=>{let openBase=openSettings;openSettings=async()=>{await openBase();let box=document.querySelector('#settingsBox'),actions=document.querySelector('#settingsActions'),section=document.querySelector('#proofTime');if(!section){section=document.createElement('div');section.id='proofTime';section.className='proofTime';section.innerHTML='<b>Proof timestamp</b><p class=sub>Used on live previews, snapshots and recorded clips. Use an IANA timezone name.</p><label>Timezone <input id="proofTimezone" placeholder="Africa/Johannesburg"></label>';box.insertBefore(section,actions)}let settings=await fetch('/api/storage').then(response=>response.json());document.querySelector('#proofTimezone').value=settings.proof_timezone||'Africa/Johannesburg'}})()</script>"""
    detection_settings_script = """<style>.detectionRules{border-top:1px solid #293241;margin-top:18px;padding-top:14px}.ruleGrid{display:grid;grid-template-columns:minmax(160px,1fr) 74px 74px;max-height:360px;overflow:auto;border:1px solid #293241;border-radius:6px}.ruleGrid>div{padding:6px 9px;border-bottom:1px solid #293241}.ruleGrid .ruleHead{position:sticky;top:0;background:#202735;font-weight:700}.ruleGrid input{accent-color:#2777e9}</style><script>(()=>{let openBase=openSettings;openSettings=async()=>{await openBase();let box=document.querySelector('#settingsBox'),actions=document.querySelector('#settingsActions'),rules=await fetch('/api/settings/detection').then(response=>response.json()),section=document.querySelector('#detectionRules');if(!section){section=document.createElement('div');section.id='detectionRules';section.className='detectionRules';box.insertBefore(section,actions)}let record=new Set(rules.record_tags),alarm=new Set(rules.alarm_tags);section.innerHTML=`<b>Detection rules</b><p class=sub>Select classes to record and to alert. Alerted classes are always recorded as evidence. Cars are disabled by default.</p><div class="ruleGrid"><div class="ruleHead">Model class</div><div class="ruleHead">Record</div><div class="ruleHead">Alarm</div>${rules.classes.map(tag=>`<div>${esc(tag)}</div><div><input data-record value="${esc(tag)}" type=checkbox ${record.has(tag)?'checked':''}></div><div><input data-alarm value="${esc(tag)}" type=checkbox ${alarm.has(tag)?'checked':''}></div>`).join('')}</div>`};let applyBase=window.applySettings;window.applySettings=async()=>{let payload={record_tags:[...document.querySelectorAll('[data-record]:checked')].map(input=>input.value),alarm_tags:[...document.querySelectorAll('[data-alarm]:checked')].map(input=>input.value)};let response=await fetch('/api/settings/detection',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});if(!response.ok)return alert(await response.text());await applyBase()}})()</script>"""
    theme_script = """<style>
      :root{--black:#06070e;--pine-teal:#29524a;--muted-teal:#94a187;--khaki-beige:#c5afa0;--cotton-rose:#e9bcb7;--text-primary:#f5eee6;--text-soft:color-mix(in srgb,var(--text-primary) 78%,var(--muted-teal));--surface:color-mix(in srgb,var(--pine-teal) 56%,var(--black));--surface-raised:color-mix(in srgb,var(--pine-teal) 72%,var(--black));--surface-active:color-mix(in srgb,var(--pine-teal) 83%,var(--black));--border:color-mix(in srgb,var(--muted-teal) 72%,var(--pine-teal));--radius:6px;--radius-small:4px}
      body{background:var(--black);color:var(--text-primary);font-family:"Trebuchet MS","Segoe UI",sans-serif}header{background:color-mix(in srgb,var(--black) 86%,var(--pine-teal));border-color:var(--border)}.brand{color:var(--khaki-beige)}.tabs button,.storage,.sub,.eventTime,.counts{color:var(--text-soft)}.tabs button.active{color:var(--text-primary);border-color:var(--khaki-beige)}button{background:var(--khaki-beige);color:var(--black);border-radius:var(--radius);font-weight:650}.cam,.event,.filterBar{background:var(--surface);border-color:var(--border);border-radius:var(--radius)}.cam img,.event img,#clipPlayer{background:var(--black)}.stat{background:var(--surface-raised);border-color:var(--border);border-radius:var(--radius-small)}.stat span,.filterBar label,.rowHead{color:var(--khaki-beige);font-size:11px;letter-spacing:.07em;text-transform:uppercase}.online{background:var(--muted-teal)}.offline{background:var(--cotton-rose)}.pill,.tag,#settingsActions .cancel{background:var(--surface-raised);color:var(--text-primary);border:1px solid var(--border)}.pill.active{background:var(--surface-active);box-shadow:inset 0 0 0 1px var(--muted-teal)}.tag.human{background:color-mix(in srgb,var(--cotton-rose) 38%,var(--surface-raised))}.tag.dog,.tag.cat,.tag.bird{background:color-mix(in srgb,var(--muted-teal) 38%,var(--surface-raised))}.tag.car{background:color-mix(in srgb,var(--khaki-beige) 38%,var(--surface-raised))}#playerPanel,#settingsPanel,#aiAbout{background:color-mix(in srgb,var(--black) 86%,transparent)}#playerBox,#settingsBox,#aiAboutBox{background:var(--surface);border-color:var(--border);border-radius:var(--radius)}.row input,.retention input,.filterBar select,.wallConfig select,#modalFilters select,.proofTime input{background:var(--black);color:var(--text-primary);border-color:var(--border);border-radius:var(--radius-small)}.wallConfig,.retention,.proofTime,.detectionRules,#settingsActions{border-color:var(--border)}.ruleGrid,.ruleGrid>div{border-color:var(--border)}.ruleGrid .ruleHead{background:var(--surface-raised)}.ruleGrid input{accent-color:var(--muted-teal)}.remove{background:var(--cotton-rose);color:var(--black)}.cam.activeEvent{border-color:var(--cotton-rose)!important;box-shadow:0 0 0 1px var(--cotton-rose),0 0 22px color-mix(in srgb,var(--cotton-rose) 38%,transparent)}.eventLive{background:var(--cotton-rose);color:var(--black)}
    </style>"""
    return HTMLResponse(page.replace("</body>", stats_script + confidence_script + active_event_script + active_refresh_script + controls_script + websocket_wall_script + compact_layout_script + settings_actions_script + modal_filter_script + timezone_settings_script + detection_settings_script + theme_script + "</body>"))
