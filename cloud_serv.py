"""Cloud layer for Safe Thresher: Tiger Data + Deepgram STT + Cartesia TTS + optional Gemini.

Everything here runs on background threads. Nothing touches Tk widgets.
Results go back to the app through emit(*args), which the app wires to
its existing event queue (self.events.put(args)).

Events emitted:
    ('cloud_log', category, message)   -> app.log(category, message)
    ('cloud_status', text)             -> status line in the UI
    ('ai', dict)                       -> Gemini analysis (or {'error': ...})
    ('heard', question, answer)        -> voice Q&A result
"""
import hashlib
import io
import json
import os
import queue
import subprocess
import threading
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    psycopg2 = None

try:
    from google import genai
except ImportError:
    genai = None

try:
    import numpy as np
    import sounddevice as sd
except ImportError:
    np = None
    sd = None

# ----------------------------------------------------------------------
# Config (set these in a .env file next to the app, see .env.example)
# ----------------------------------------------------------------------
TIGER_URL = os.getenv('TIGER_DATABASE_URL', '')
GEMINI_KEY = os.getenv('GEMINI_API_KEY', '')
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')
DEEPGRAM_KEY = os.getenv('DEEPGRAM_API_KEY', '')
DEEPGRAM_MODEL = os.getenv('DEEPGRAM_MODEL', 'nova-3')
CARTESIA_KEY = os.getenv('CARTESIA_API_KEY', '')
CARTESIA_MODEL = os.getenv('CARTESIA_MODEL', 'sonic-3')
CARTESIA_VOICE_ID = os.getenv('CARTESIA_VOICE_ID', '12e85709-099c-480a-ba3e-875c41a9611a')
CARTESIA_VOICE_NAME = os.getenv('CARTESIA_VOICE_NAME', 'Selected Cartesia Voice')

# Fixed alert phrases are saved here once and reused on every launch (no re-generation).
CACHE_DIR = Path(os.getenv('VOICE_CACHE_DIR', Path.home() / 'Documents' / 'Safe Thresher Data' / 'voice_cache'))

PCM_RATE = 22050            # matches output_format=pcm_22050
ANALYSIS_PERIOD = int(os.getenv("GEMINI_ANALYSIS_PERIOD", "180"))  # reduce quota use
AUTO_ANALYSIS = os.getenv("GEMINI_AUTO_ANALYSIS", "0").strip().lower() in ("1", "true", "yes")
ANALYSIS_MIN_GAP = 10       # minimum seconds between analyses (zone-change pokes)
ZONE_CONFIRM_SECONDS = 1.5  # a zone must hold this long before it counts
SPEECH_COOLDOWN = 8         # seconds between non-escalating announcements

SYSTEM_PREAMBLE = (
    "You assist with Safe Thresher, a prototype water-intrusion monitor. "
    "A sensor produces ADC readings 0-1023; higher means more water contact. "
    "The thresholds are sensor-signal bands, NOT validated water depths, and this "
    "is NOT a certified flood alarm. Use only the data provided; never invent "
    "numbers. Be brief and plain-spoken."
)

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS readings (
        ts          timestamptz NOT NULL,
        adc         integer     NOT NULL,
        calibrating boolean     NOT NULL DEFAULT false)""",
    """CREATE TABLE IF NOT EXISTS calibrations (
        ts         timestamptz NOT NULL DEFAULT now(),
        trial      integer,
        max_adc    integer,
        median_adc double precision,
        flutter    integer,
        thresholds integer[])""",
    """CREATE TABLE IF NOT EXISTS alert_events (
        ts         timestamptz NOT NULL DEFAULT now(),
        zone       text        NOT NULL,
        median_adc integer)""",
]
HYPERTABLE_SQL = "SELECT create_hypertable('readings', 'ts', if_not_exists => TRUE)"


# ----------------------------------------------------------------------
# Tiger Data (TimescaleDB on Postgres)
# ----------------------------------------------------------------------
class TigerStore:
    """Buffered writer plus summary queries over one shared connection."""

    def __init__(self, log):
        self.log = log
        self.conn = None
        self.lock = threading.Lock()
        self.readings = []
        self.events = []          # (sql, params) for small tables
        self.buf_lock = threading.Lock()
        self.stop_evt = threading.Event()
        self.thread = None
        self.connected = False

    def start(self):
        if psycopg2 is None:
            self.log('WARNING', 'Tiger Data disabled: pip install psycopg2-binary')
            return
        if not TIGER_URL:
            self.log('WARNING', 'Tiger Data disabled: TIGER_DATABASE_URL not set')
            return
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_evt.set()
        if self.thread:
            self.thread.join(timeout=3)
        try:
            self._flush()
        except Exception:
            pass
        with self.lock:
            if self.conn:
                try:
                    self.conn.close()
                except Exception:
                    pass

    # -- connection ------------------------------------------------------
    def _connect(self):
        conn = psycopg2.connect(TIGER_URL, connect_timeout=8)
        conn.autocommit = True
        with conn.cursor() as cur:
            for stmt in SCHEMA:
                cur.execute(stmt)
            try:
                cur.execute(HYPERTABLE_SQL)
            except Exception as exc:
                self.log('WARNING', f'create_hypertable skipped: {exc}')
        with self.lock:
            self.conn = conn
        self.connected = True
        self.log('SUCCESS', 'Tiger Data connected; schema ready.')

    def _drop_connection(self):
        self.connected = False
        with self.lock:
            if self.conn:
                try:
                    self.conn.close()
                except Exception:
                    pass
            self.conn = None

    def _loop(self):
        backoff = 2
        while not self.stop_evt.is_set():
            if self.conn is None:
                try:
                    self._connect()
                    backoff = 2
                except Exception as exc:
                    self.log('ERROR', f'Tiger Data connect failed: {exc}')
                    self.stop_evt.wait(backoff)
                    backoff = min(30, backoff * 2)
                    continue
            try:
                self._flush()
            except Exception as exc:
                self.log('ERROR', f'Tiger Data write failed: {exc}')
                self._drop_connection()
            self.stop_evt.wait(1.0)

    # -- writes ----------------------------------------------------------
    def add_reading(self, ts, adc, calibrating):
        with self.buf_lock:
            self.readings.append((ts, adc, calibrating))
            if len(self.readings) > 20000:      # offline cap; CSV still has the data
                del self.readings[:5000]

    def add_event(self, sql, params):
        with self.buf_lock:
            self.events.append((sql, params))

    def _flush(self):
        with self.buf_lock:
            rows, self.readings = self.readings, []
            events, self.events = self.events, []
        if not rows and not events:
            return
        try:
            with self.lock:
                if self.conn is None:
                    raise ConnectionError('no connection')
                with self.conn.cursor() as cur:
                    if rows:
                        execute_values(
                            cur, 'INSERT INTO readings (ts, adc, calibrating) VALUES %s', rows)
                    for sql, params in events:
                        cur.execute(sql, params)
        except Exception:
            with self.buf_lock:                 # put it back and retry after reconnect
                self.readings = rows + self.readings
                self.events = events + self.events
            raise

    # -- reads -----------------------------------------------------------
    def summary(self, window_s=60):
        """Compact windowed stats for Gemini. Returns None if unavailable."""
        with self.lock:
            if self.conn is None:
                return None
            with self.conn.cursor() as cur:
                cur.execute(
                    """SELECT time_bucket('10 seconds', ts) AS b,
                              avg(adc)::float, min(adc), max(adc), count(*)
                       FROM readings
                       WHERE ts > now() - make_interval(secs => %s) AND NOT calibrating
                       GROUP BY b ORDER BY b""", (window_s,))
                buckets = cur.fetchall()
                cur.execute(
                    """SELECT extract(epoch FROM now() - ts)::int, zone, median_adc
                       FROM alert_events ORDER BY ts DESC LIMIT 5""")
                events = cur.fetchall()
        if not buckets:
            return None
        first, last = buckets[0], buckets[-1]
        span = (last[0] - first[0]).total_seconds()
        rate = ((last[1] - first[1]) / span * 60) if span > 0 else 0.0
        return {
            'window_seconds': window_s,
            'avg_per_10s': [round(b[1], 1) for b in buckets],
            'window_min': min(b[2] for b in buckets),
            'window_max': max(b[3] for b in buckets),
            'samples': sum(b[4] for b in buckets),
            'change_adc_per_minute': round(rate, 1),
            'recent_zone_changes': [
                {'seconds_ago': e[0], 'zone': e[1], 'median_adc': e[2]} for e in events],
        }


# ----------------------------------------------------------------------
# Gemini
# ----------------------------------------------------------------------
class GeminiClient:
    def __init__(self):
        self.client = None
        if genai is not None and GEMINI_KEY:
            self.client = genai.Client(api_key=GEMINI_KEY)

    @property
    def ready(self):
        return self.client is not None

    def ask_json(self, prompt):
        resp = self.client.models.generate_content(
            model=GEMINI_MODEL, contents=prompt,
            config={'response_mime_type': 'application/json'})
        text = (resp.text or '').strip()
        text = text.removeprefix('```json').removeprefix('```').removesuffix('```').strip()
        return json.loads(text)

    def ask_text(self, prompt):
        resp = self.client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        return (resp.text or '').strip()


def transcribe(wav_bytes):
    """Transcribe a recorded WAV through Deepgram's prerecorded-audio API."""
    if not DEEPGRAM_KEY:
        raise RuntimeError('DEEPGRAM_API_KEY missing. Add it to your .env file.')
    if not wav_bytes:
        return ''
    r = requests.post(
        'https://api.deepgram.com/v1/listen',
        params={'model': DEEPGRAM_MODEL, 'smart_format': 'true', 'language': 'en'},
        headers={
            'Authorization': f'Token {DEEPGRAM_KEY}',
            'Content-Type': 'audio/wav',
        },
        data=wav_bytes,
        timeout=45,
    )
    try:
        r.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(
            f'Deepgram HTTP {r.status_code}: {r.text[:250]}'
        ) from exc
    data = r.json()
    channels = data.get('results', {}).get('channels', [])
    if not channels:
        return ''
    alternatives = channels[0].get('alternatives', [])
    return (alternatives[0].get('transcript', '') if alternatives else '').strip()


class Recorder:
    RATE = 16000

    def __init__(self):
        self.frames = []
        self.stream = None
        self.last_peak = 0.0
        self.last_rms = 0.0
        self.last_duration = 0.0

    def start(self):
        self.frames = []

        def cb(indata, frames, time_info, status):
            self.frames.append(indata.copy())
        device = os.getenv('MIC_DEVICE', '').strip()
        if device:
            try:
                device = int(device)
            except ValueError:
                pass
        else:
            device = None
        self.stream = sd.InputStream(device=device, samplerate=self.RATE, channels=1,
                                     dtype='int16', callback=cb)
        self.stream.start()

    def stop(self):
        """Stop and return WAV bytes, or None if nothing was captured."""
        if self.stream is None:
            return None
        self.stream.stop()
        self.stream.close()
        self.stream = None
        if not self.frames:
            return None
        audio = np.concatenate(self.frames)
        samples = audio.astype(np.float64).reshape(-1)
        self.last_duration = len(samples) / self.RATE
        self.last_peak = float(np.max(np.abs(samples))) / 32768.0
        self.last_rms = float(np.sqrt(np.mean(samples * samples))) / 32768.0
        if len(audio) < self.RATE * 0.4:        # ignore accidental taps
            return None
        buf = io.BytesIO()
        with wave.open(buf, 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.RATE)
            w.writeframes(audio.tobytes())
        return buf.getvalue()


# Cartesia automatic spoken flood alarms
SEVERITY = {'GREEN': 0, 'BLUE': 1, 'BLUE_BLINK': 2, 'RED': 3, 'RED_BLINK': 4}
MESSAGES = {
    'BLUE': 'Water intrusion detected.',
    'BLUE_BLINK': 'Caution. Elevated water reading.',
    'RED': 'Warning. High water reading.',
    'RED_BLINK': 'Critical flooding alert. Act immediately.',
}

class DamageControlVoice:
    """Automatic Cartesia Sonic WAV announcements with local disk caching."""
    def __init__(self, emit=None):
        self.emit = emit or (lambda *args: None)
        self.muted = False
        self.stopped = False
        self.last_zone = None
        self.normal_since = None
        self.last_critical_repeat = time.monotonic()
        self.last_spoken = {}
        self.q = queue.Queue()
        self.api_key = CARTESIA_KEY
        self.model = CARTESIA_MODEL
        self.voice_id = CARTESIA_VOICE_ID
        self.cache_dir = Path(__file__).resolve().parent / 'voice_alerts'
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.player = None
        self.play_lock = threading.RLock()
        try:
            import pygame
            pygame.mixer.init()
            self.player = pygame
        except Exception as exc:
            self.emit('cloud_log', 'LINK', f'pygame unavailable ({exc}); using Windows WAV playback when available.')
        if not self.api_key:
            self.emit('cloud_log', 'ERROR', 'Missing CARTESIA_API_KEY in .env; voice alerts unavailable')
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()

    def _say(self, message, priority=False, cooldown=12):
        if self.muted or self.stopped:
            return
        now = time.monotonic()
        if now - self.last_spoken.get(message, -1e10) < cooldown:
            return
        self.last_spoken[message] = now
        if priority:
            # Interrupt an ongoing low-priority announcement.
            if self.player:
                with self.play_lock:
                    self.player.mixer.music.stop()
            try:
                while True:
                    self.q.get_nowait()
            except queue.Empty:
                pass
        self.q.put(message)

    def _resolve_voice(self):
        if self.voice_id:
            return self.voice_id
        if not self.api_key:
            raise RuntimeError('CARTESIA_API_KEY missing from .env')
        r = requests.get('https://api.cartesia.ai/voices', headers={
            'X-API-Key': self.api_key, 'Cartesia-Version': '2025-04-16'}, timeout=20)
        if not r.ok:
            raise RuntimeError(f'Cartesia voices HTTP {r.status_code}: {r.text[:240]}')
        data = r.json()
        voices = data if isinstance(data, list) else data.get('voices', data.get('data', []))
        # Daniel: crisp, male American digital-assistant voice; best fit for concise naval alerts.
        matches = [v for v in voices if v.get('name', '').strip().casefold() == CARTESIA_VOICE_NAME.casefold()]
        if not matches:
            raise RuntimeError(f'Voice {CARTESIA_VOICE_NAME!r} not in accessible Cartesia voices. '
                               'Copy its ID from the Cartesia voice library and set CARTESIA_VOICE_ID in .env.')
        self.voice_id = matches[0].get('id') or matches[0].get('voice_id')
        if not self.voice_id:
            raise RuntimeError('Cartesia voice listing did not contain an ID')
        self.emit('cloud_log', 'SUCCESS', f'Cartesia voice selected: {CARTESIA_VOICE_NAME}')
        return self.voice_id

    def _audio_path(self, message):
        voice = self._resolve_voice()
        token = hashlib.sha256(f'cartesia|{self.model}|{voice}|{message}'.encode()).hexdigest()
        return self.cache_dir / (token + '.wav')

    def _get_audio(self, message):
        path = self._audio_path(message)
        if path.exists() and path.stat().st_size > 100:
            try:
                self._validate_wav(path)
                return path
            except Exception:
                path.unlink(missing_ok=True)
        response = requests.post('https://api.cartesia.ai/tts/bytes', headers={
            'X-API-Key': self.api_key, 'Cartesia-Version': '2025-04-16',
            'Content-Type': 'application/json'}, json={
            'model_id': self.model,
            'transcript': message,
            'voice': {'mode': 'id', 'id': self.voice_id},
            'language': 'en',
            'output_format': {'container': 'wav', 'encoding': 'pcm_s16le', 'sample_rate': 44100},
        }, timeout=40)
        if not response.ok:
            raise RuntimeError(f'Cartesia TTS HTTP {response.status_code}: {response.text[:260]}')
        if not response.content.startswith(b'RIFF'):
            raise RuntimeError('Cartesia did not return a WAV file')
        temp = path.with_suffix('.part')
        temp.write_bytes(response.content)
        self._validate_wav(temp)
        temp.replace(path)
        return path

    @staticmethod
    def _validate_wav(path):
        """Reject corrupt/unsupported cached audio rather than playing Windows default beep."""
        with wave.open(str(path), 'rb') as wav:
            if wav.getnframes() < 1000 or wav.getnchannels() not in (1, 2) or wav.getsampwidth() != 2:
                raise RuntimeError('Cartesia returned an invalid or unsupported PCM WAV file')
            if wav.getframerate() not in (16000, 22050, 24000, 32000, 44100, 48000):
                raise RuntimeError('Unexpected Cartesia WAV sample rate')

    def _play_windows_wav(self, path):
        """Play through Windows with canonical PCM conversion and a MediaPlayer fallback."""
        import winsound
        import tempfile
        import subprocess
        self._validate_wav(path)
        # Rewrite to a minimal PCM WAV header; Windows PlaySound rejects some
        # valid Cartesia RIFF variants that Python's wave reader accepts.
        with wave.open(str(path), 'rb') as source:
            channels = source.getnchannels()
            width = source.getsampwidth()
            rate = source.getframerate()
            samples = source.readframes(source.getnframes())
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp:
            clean = Path(tmp.name)
        try:
            with wave.open(str(clean), 'wb') as target:
                target.setnchannels(channels)
                target.setsampwidth(width)
                target.setframerate(rate)
                target.writeframes(samples)
            try:
                winsound.PlaySound(str(clean), winsound.SND_FILENAME | winsound.SND_NODEFAULT)
                return
            except RuntimeError as first_error:
                # MediaPlayer uses Windows Media Foundation rather than PlaySound.
                # A PowerShell child process keeps playback synchronous.
                script = ("Add-Type -AssemblyName PresentationCore; "
                          "$p=New-Object System.Windows.Media.MediaPlayer; "
                          "$p.Open([Uri]::new($args[0])); "
                          "Start-Sleep -Milliseconds 350; "
                          "$p.Play(); "
                          "$duration=$p.NaturalDuration; "
                          "if($duration.HasTimeSpan){Start-Sleep -Milliseconds ([int]$duration.TimeSpan.TotalMilliseconds + 250)} "
                          "else{Start-Sleep -Seconds 5}; $p.Close()")
                result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                                         '-Command', script, str(clean)],
                                        capture_output=True, text=True, timeout=35)
                if result.returncode:
                    raise RuntimeError('Windows playback failed via both PlaySound and MediaPlayer. '
                                       f'PlaySound: {first_error}; MediaPlayer: {result.stderr[-300:]}')
        finally:
            clean.unlink(missing_ok=True)

    def _worker(self):
        while not self.stopped:
            try:
                message = self.q.get(timeout=0.25)
            except queue.Empty:
                continue
            if self.stopped or self.muted:
                continue
            try:
                path = self._get_audio(message)
                if self.player is not None:
                    self._validate_wav(path)
                    with self.play_lock:
                        self.player.mixer.music.load(str(path))
                        self.player.mixer.music.play()
                    self.emit('cloud_log', 'SUCCESS', f'Spoken alert started: {message}')
                    while not self.stopped and not self.muted and self.player.mixer.music.get_busy():
                        time.sleep(0.05)
                elif os.name == 'nt':
                    self._play_windows_wav(path)
                    self.emit('cloud_log', 'SUCCESS', f'Spoken alert played: {message}')
                else:
                    raise RuntimeError('No audio player; install pygame')
            except Exception as exc:
                self.emit('cloud_log', 'ERROR', f'Cartesia voice alert failed: {exc}')

    def observe(self, zone, calibrated, prediction=None):
        if not calibrated or zone is None:
            self.last_zone = None
            self.normal_since = None
            return
        now = time.monotonic()
        prev = self.last_zone
        if zone != prev:
            self.last_zone = zone
            if prev is None:
                if zone != 'GREEN':
                    self._say(MESSAGES[zone], priority=SEVERITY[zone] >= 3)
            elif SEVERITY[zone] > SEVERITY[prev]:
                self._say(MESSAGES[zone], priority=SEVERITY[zone] >= 3)
            elif zone == 'GREEN':
                self.normal_since = now
            else:
                self._say('Water reading decreasing. Warning remains active.', cooldown=8)
        if zone != 'GREEN':
            self.normal_since = None
        elif self.normal_since is not None and now - self.normal_since >= 5:
            self._say('Water readings normal.', cooldown=20)
            self.normal_since = None
        if zone == 'RED_BLINK':
            if now - self.last_critical_repeat >= 45:
                self.last_critical_repeat = now
                if prev == zone:
                    self._say('Critical alert remains active.', priority=True, cooldown=40)
        else:
            self.last_critical_repeat = now

    def signal_lost(self):
        self._say('Warning. Sensor signal lost.', priority=True, cooldown=30)

    def disconnected(self):
        self._say('Warning. Sensor connection lost.', priority=True, cooldown=30)
        self.last_zone = None

    def toggle_mute(self):
        self.muted = not self.muted
        if self.muted and self.player:
            with self.play_lock:
                self.player.mixer.music.stop()
        return self.muted

    def stop(self):
        self.stopped = True
        if self.player:
            with self.play_lock:
                self.player.mixer.music.stop()


# ----------------------------------------------------------------------
# Facade the app talks to
# ----------------------------------------------------------------------
class CloudServices:
    SEVERITY = {'GREEN': 0, 'BLUE': 1, 'BLUE_BLINK': 2, 'RED': 3, 'RED_BLINK': 4}
    PHRASES = {
        'GREEN': 'Sensor readings are back to normal.',
        'BLUE': 'Water detected at the sensor.',
        'BLUE_BLINK': 'Water level is elevated and rising.',
        'RED': 'Warning. High water reading.',
        'RED_BLINK': 'Water level critical.',
    }

    def __init__(self, emit):
        self.emit = emit
        self.state = {}               # app assigns a fresh dict from the Tk thread
        self.store = TigerStore(self._log)
        self.gemini = GeminiClient()
        self.speaker = DamageControlVoice(emit=self.emit)
        self.recorder = Recorder()
        self.stop_evt = threading.Event()
        self.poke_evt = threading.Event()
        self.last_run = 0.0
        self.analyst_thread = None
        self.candidate = None
        self.candidate_since = 0.0
        self.confirmed = None
        self.last_spoken = 0.0
        self.listening = False

    def _log(self, category, message):
        self.emit('cloud_log', category, message)

    def _status(self):
        parts = [
            'Tiger Data: ' + ('connected' if self.store.connected else 'offline'),
            'Gemini: ' + ('ready' if self.gemini.ready else 'off'),
            'Deepgram STT: ' + ('ready' if DEEPGRAM_KEY else 'off'),
            'Spoken flood alerts: Cartesia Sonic',
        ]
        self.emit('cloud_status', '   |   '.join(parts))

    # -- lifecycle -------------------------------------------------------
    def start(self):
        self.store.start()
        # Voice alerts are triggered automatically by observe_zone().
        self.last_run = time.monotonic() - ANALYSIS_PERIOD + 10   # first analysis after ~10 s
        if AUTO_ANALYSIS:
            self.analyst_thread = threading.Thread(target=self._analyst_loop, daemon=True)
            self.analyst_thread.start()
        else:
            self._log('LINK', 'Automatic Gemini analysis disabled.')
        threading.Thread(target=self._status_loop, daemon=True).start()

    def stop(self):
        self.stop_evt.set()
        self.poke_evt.set()
        self.speaker.stop()
        self.store.stop()

    def _status_loop(self):
        while not self.stop_evt.is_set():
            self._status()
            self.stop_evt.wait(3)

    # -- data in (called from the Tk thread; all non-blocking) -----------
    def log_reading(self, adc, calibrating):
        self.store.add_reading(datetime.now(timezone.utc), int(adc), bool(calibrating))

    def log_calibration(self, trial, max_adc, median, flutter, thresholds):
        self.store.add_event(
            'INSERT INTO calibrations (trial, max_adc, median_adc, flutter, thresholds) '
            'VALUES (%s, %s, %s, %s, %s)',
            (trial, int(max_adc), float(median), int(flutter), [int(t) for t in thresholds]))
        self.confirmed = None
        self.candidate = None
        self.speaker._say('Calibration complete.', cooldown=5)

    def observe_zone(self, zone, median):
        """Call on every alert update. Debounces, logs, announces, pokes Gemini."""
        self.speaker.observe(zone, zone is not None)
        if zone is None:
            self.candidate = self.confirmed = None
            return
        now = time.monotonic()
        if zone != self.candidate:
            self.candidate, self.candidate_since = zone, now
            return
        if zone == self.confirmed or now - self.candidate_since < ZONE_CONFIRM_SECONDS:
            return
        prev, self.confirmed = self.confirmed, zone
        self.store.add_event(
            'INSERT INTO alert_events (zone, median_adc) VALUES (%s, %s)', (zone, int(median)))
        self.poke_evt.set()

    def _announce(self, zone, prev):
        if prev is None and zone == 'GREEN':
            return
        escalating = self.SEVERITY[zone] > self.SEVERITY.get(prev, 0)
        now = time.monotonic()
        if not escalating and now - self.last_spoken < SPEECH_COOLDOWN:
            return
        self.last_spoken = now

    def toggle_mute(self):
        return self.speaker.toggle_mute()

    # -- Gemini analyst ----------------------------------------------------
    def _context(self):
        # Snapshot the most recent app state. The database is historical; it must
        # never override the current reading reported by the Arduino.
        snapshot = dict(self.state) if isinstance(self.state, dict) else {}
        try:
            history = self.store.summary(60) if self.store.connected else None
        except Exception as exc:
            self._log('WARNING', f'Tiger Data summary unavailable: {exc}')
            history = None
        return json.dumps({
            'live_state': snapshot,
            'recent_window': history,
        }, default=str)

    def _analyst_loop(self):
        while not self.stop_evt.is_set():
            fired = self.poke_evt.wait(1.0)
            if self.stop_evt.is_set():
                break
            if fired:
                self.poke_evt.clear()
            now = time.monotonic()
            gap = now - self.last_run
            if (fired and gap >= ANALYSIS_MIN_GAP) or gap >= ANALYSIS_PERIOD:
                self.last_run = now
                self._analyze()

    def _analyze(self):
        if not self.gemini.ready or not self.store.connected or not self.state.get('thresholds'):
            return
        try:
            context = self._context()
            prompt = (
                SYSTEM_PREAMBLE + "\n\nData (JSON):\n" + context +
                "\n\nReturn ONLY JSON with keys: status (max 6 words), "
                "trend ('rising'|'falling'|'steady'|'unknown'), "
                "explanation (max 2 sentences), recommended_action (max 1 sentence)."
            )
            self.emit('ai', self.gemini.ask_json(prompt))
        except Exception as exc:
            self.emit('ai', {'error': str(exc)[:160]})

    def ask(self, question):
        """Answer a question using live state and Tiger Data history. Blocking."""
        prompt = (
            SYSTEM_PREAMBLE + "\n\nData (JSON):\n" + self._context() +
            f"\n\nQuestion: {question}\nFor questions about the current water reading, explicitly state the live ADC value and warning zone if present in live_state. If a value is missing, say it is unavailable. Do not equate ADC fraction with physical water depth. Answer in at most 50 words, suitable to be spoken aloud."
        )
        return self.gemini.ask_text(prompt)

    # -- Voice Q&A (hold to talk) ------------------------------------------
    def start_listening(self):
        if sd is None or np is None:
            self._log('ERROR', 'Mic unavailable: pip install sounddevice numpy')
            return
        if self.listening:
            return
        if not DEEPGRAM_KEY:
            self._log('ERROR', 'Deepgram STT unavailable: set DEEPGRAM_API_KEY in .env')
            return
        try:
            self.recorder.start()
            self.listening = True
            self.emit('cloud_status', 'Listening...')
        except Exception as exc:
            self._log('ERROR', f'Mic start failed: {exc}')

    def stop_listening(self):
        if not self.listening:
            return
        self.listening = False
        try:
            wav_bytes = self.recorder.stop()
        except Exception as exc:
            self._log('ERROR', f'Mic stop failed: {exc}')
            return
        if wav_bytes is None:
            self._log('WARNING', 'Recording too short. Hold to Talk for at least 2 seconds.')
            return
        self._log('MIC', f'Recorded {self.recorder.last_duration:.1f}s; '
                  f'peak={self.recorder.last_peak:.3f}; RMS={self.recorder.last_rms:.4f}')
        if self.recorder.last_peak < 0.008:
            self._log('WARNING', 'Microphone audio is nearly silent. Check Windows input device, '
                      'microphone permissions and input level; optionally set MIC_DEVICE in .env.')
            return
        threading.Thread(target=self._voice_pipeline, args=(wav_bytes,), daemon=True).start()

    def ask_text(self, question):
        """Typed fallback (handy for testing without a mic)."""
        threading.Thread(target=self._answer, args=(question,), daemon=True).start()

    def _voice_pipeline(self, wav_bytes):
        try:
            question = transcribe(wav_bytes)
        except Exception as exc:
            self._log('ERROR', f'Speech-to-text failed: {exc}')
            return
        if question:
            self._answer(question)
        else:
            self._log('WARNING', 'Deepgram received audio but found no words. Try speaking for 3 seconds near the selected microphone.')

    def _local_sensor_answer(self, question):
        """Deterministic live readings: no LLM quota or fabricated depth percentages."""
        q = question.lower()
        terms = ('water', 'reading', 'sensor', 'adc', 'level', 'status',
                 'flood', 'critical', 'danger', 'threshold', 'maximum', 'max')
        if not any(term in q for term in terms):
            return None
        state = dict(self.state) if isinstance(self.state, dict) else {}
        adc = state.get('live_adc')
        zone = state.get('zone')
        peak = state.get('calibrated_max')
        if adc is None:
            return 'The Arduino has not provided a current sensor reading yet.'
        if zone is None:
            return f'The latest sensor reading is {adc} ADC. The warning zone is unavailable until calibration.'
        answer = f'The current sensor reading is {adc} ADC, in the {str(zone).replace("_", " ").lower()} warning zone.'
        if peak is not None:
            answer += f' The calibrated maximum is {peak} ADC.'
        answer += ' These values indicate sensor contact, not physical water depth.'
        return answer

    def _answer(self, question):
        # Critical readings must remain available even when Gemini is rate-limited.
        local = self._local_sensor_answer(question)
        if local is not None:
            self.emit('heard', question, local)
            self.speaker._say(local, priority=True)
            return
        if not self.gemini.ready:
            answer = 'Gemini is unavailable. Ask me for the current water sensor reading or warning status.'
            self.emit('heard', question, answer)
            self.speaker._say(answer, priority=True)
            return
        try:
            answer = self.ask(question)
        except Exception as exc:
            self._log('ERROR', f'Gemini answer failed: {str(exc)[:220]}')
            answer = 'Gemini is temporarily unavailable. I can still report live water sensor readings.'
        self.emit('heard', question, answer)
        self.speaker._say(answer, priority=True)


# Optional speaker test: py cloud_serv.py --test-voice
if __name__ == '__main__':
    import sys
    if '--test-voice' in sys.argv:
        speaker = DamageControlVoice(emit=lambda *a: print(*a, flush=True))
        speaker._say('Safe Thresher voice system online.', cooldown=0)
        try:
            time.sleep(8)  # allows API generation and playback; logs any API errors
        finally:
            speaker.stop()
