
# Safe Thresher - ElevenLabs API Voice Alerts
# Generates audio once, caches it, then replays instantly.
# No Deepgram or Gemini.

import os
import time
import queue
import threading
from pathlib import Path

import requests
import pygame
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "").strip()

AUDIO_DIR = Path(__file__).resolve().parent / "voice_alerts"
AUDIO_DIR.mkdir(exist_ok=True)

MESSAGES = {
    "water_detected": "Water detected.",
    "water_elevated": "Caution. Water level elevated.",
    "water_high": "Warning. High water level.",
    "water_critical": "Critical flooding alert.",
    "water_rapid": "Warning. Rapid water rise.",
    "water_decreasing": "Water level decreasing.",
    "water_normal": "Water levels normal.",
    "critical_approaching": "Critical threshold approaching.",
    "critical_active": "Critical alert remains active.",
    "signal_lost": "Sensor signal lost.",
    "connection_lost": "Sensor connection lost.",
    "calibration_complete": "Calibration complete.",
}

ZONES = {
    "GREEN": 0,
    "BLUE": 1,
    "BLUE_BLINK": 2,
    "RED": 3,
    "RED_BLINK": 4,
}

ZONE_MESSAGES = {
    "BLUE": "water_detected",
    "BLUE_BLINK": "water_elevated",
    "RED": "water_high",
    "RED_BLINK": "water_critical",
}


class DamageControlVoice:

    def __init__(self, emit=None):
        self.emit = emit or (lambda *args: None)
        self.muted = False
        self.stopped = False
        self.last_zone = None
        self.last_prediction = False
        self.normal_since = None
        self.last_spoken = {}
        self.last_critical_repeat = time.monotonic()

        self.jobs = queue.PriorityQueue()
        self.counter = 0
        self.lock = threading.Lock()
        self.audio_lock = threading.Lock()
        self.audio_ready = False

        try:
            pygame.mixer.init()
            self.audio_ready = True
        except pygame.error as exc:
            self._log("ERROR", f"Audio unavailable: {exc}")

        self.worker = threading.Thread(
            target=self._worker,
            daemon=True
        )
        self.worker.start()

        if not API_KEY or not VOICE_ID:
            self._log(
                "WARNING",
                "ElevenLabs credentials missing from .env"
            )
        else:
            self._log("SUCCESS", "ElevenLabs API voice ready.")

        # Prepare all phrases in the background.
        # Existing cached clips are reused.
        threading.Thread(
            target=self._prepare_all,
            daemon=True
        ).start()

    def _log(self, level, message):
        self.emit("cloud_log", level, message)

    def _generate(self, name):
        path = AUDIO_DIR / f"{name}.mp3"

        if path.exists() and path.stat().st_size > 0:
            return path

        if not API_KEY or not VOICE_ID:
            return None

        url = (
            "https://api.elevenlabs.io/v1/text-to-speech/"
            f"{VOICE_ID}"
        )

        payload = {
            "text": MESSAGES[name],
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": 0.70,
                "similarity_boost": 0.80,
                "style": 0.10,
                "use_speaker_boost": True
            }
        }

        try:
            response = requests.post(
                url,
                params={"output_format": "mp3_44100_128"},
                headers={
                    "xi-api-key": API_KEY,
                    "Content-Type": "application/json"
                },
                json=payload,
                timeout=25
            )

            response.raise_for_status()

            temp = path.with_suffix(".tmp")
            temp.write_bytes(response.content)
            temp.replace(path)

            self._log("SUCCESS", f"Voice cached: {name}")
            return path

        except requests.RequestException as exc:
            detail = ""
            if getattr(exc, "response", None) is not None:
                detail = exc.response.text[:250]
            self._log(
                "ERROR",
                f"ElevenLabs {name}: {detail or exc}"
            )
            return None

    def _prepare_all(self):
        for name in MESSAGES:
            if self.stopped:
                return
            self._generate(name)

    def _say(self, name, priority=False, cooldown=12):
        if self.muted or self.stopped:
            return

        now = time.monotonic()
        if now - self.last_spoken.get(name, -1e10) < cooldown:
            return

        self.last_spoken[name] = now

        with self.lock:
            self.counter += 1
            number = self.counter

        self.jobs.put((0 if priority else 1, number, name))

        if priority and self.audio_ready:
            with self.audio_lock:
                pygame.mixer.music.stop()

    def _worker(self):
        while not self.stopped:
            try:
                _, _, name = self.jobs.get(timeout=0.25)
            except queue.Empty:
                continue

            try:
                if self.muted or self.stopped:
                    continue

                path = self._generate(name)
                if path is None or not self.audio_ready:
                    continue

                with self.audio_lock:
                    pygame.mixer.music.load(str(path))
                    pygame.mixer.music.play()

                self.emit("voice_event", MESSAGES[name])

                # Wait without blocking the dashboard.
                while (
                    not self.stopped
                    and not self.muted
                    and pygame.mixer.music.get_busy()
                ):
                    time.sleep(0.05)

            except Exception as exc:
                self._log("ERROR", f"Voice playback: {exc}")
            finally:
                self.jobs.task_done()

    def observe(self, zone, calibrated, prediction=None):
        if not calibrated or zone not in ZONES:
            self.last_zone = None
            self.normal_since = None
            self.last_prediction = False
            return

        now = time.monotonic()
        previous = self.last_zone

        if zone != previous:
            self.last_zone = zone

            if previous is None:
                if zone != "GREEN":
                    self._say(
                        ZONE_MESSAGES[zone],
                        priority=ZONES[zone] >= 3
                    )

            elif ZONES[zone] > ZONES[previous]:
                self._say(
                    ZONE_MESSAGES[zone],
                    priority=ZONES[zone] >= 3
                )

            elif zone == "GREEN":
                self.normal_since = now

            else:
                self._say("water_decreasing", cooldown=15)

        if zone != "GREEN":
            self.normal_since = None

        elif self.normal_since is not None:
            if now - self.normal_since >= 5:
                self._say("water_normal", cooldown=20)
                self.normal_since = None

        if zone == "RED_BLINK":
            if now - self.last_critical_repeat >= 45:
                self._say(
                    "critical_active",
                    priority=True,
                    cooldown=40
                )
                self.last_critical_repeat = now
        else:
            self.last_critical_repeat = now

        ready = (
            prediction is not None
            and getattr(prediction, "ready", False)
        )

        rapid = (
            ready
            and getattr(prediction, "trend", "") == "RAPID RISE"
        )

        predictive = (
            ready
            and str(getattr(prediction, "warning", "")).startswith(
                "PREDICTIVE WARNING"
            )
        )

        active = bool(rapid or predictive)

        if active and not self.last_prediction:
            self._say(
                "critical_approaching"
                if predictive else "water_rapid",
                priority=predictive,
                cooldown=20
            )

        self.last_prediction = active

    def signal_lost(self):
        self._say("signal_lost", priority=True, cooldown=30)

    def disconnected(self):
        self._say("connection_lost", priority=True, cooldown=30)
        self.last_zone = None

    def calibration_complete(self):
        self._say("calibration_complete", cooldown=5)

    def toggle_mute(self):
        self.muted = not self.muted

        if self.muted and self.audio_ready:
            with self.audio_lock:
                pygame.mixer.music.stop()

        return self.muted

    def stop(self):
        self.stopped = True

        if self.audio_ready:
            with self.audio_lock:
                pygame.mixer.music.stop()
