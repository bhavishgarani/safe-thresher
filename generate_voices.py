
# generate_voices.py
# Run once to create ElevenLabs WAV announcements.
# No Deepgram or Gemini required.

import json
import os
import wave
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from damage_control import MESSAGES, AUDIO_DIR

from dotenv import load_dotenv
load_dotenv()

# Enter your ElevenLabs credentials here.
# You can also set ELEVENLABS_API_KEY and
# ELEVENLABS_VOICE_ID as environment variables.
API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "").strip()

MODEL_ID = "eleven_multilingual_v2"
SAMPLE_RATE = 16000

def generate_clip(name, message):
    url = (
        f"https://api.elevenlabs.io/v1/text-to-speech/"
        f"{VOICE_ID}?output_format=mp3_44100_128"
    )

    payload = {
        "text": message,
        "model_id": MODEL_ID,
        "voice_settings": {
            "stability": 0.70,
            "similarity_boost": 0.80,
            "style": 0.12,
            "use_speaker_boost": True
        }
    }

    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "xi-api-key": API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/octet-stream"
        },
        method="POST"
    )

    with urlopen(request, timeout=45) as response:
        pcm_audio = response.read()

    if not pcm_audio:
        raise ValueError("ElevenLabs returned empty audio.")

    output = AUDIO_DIR / f"{name}.wav"

    with wave.open(str(output), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm_audio)

    print(f"Created: {output.name}")


def main():
    if not API_KEY or not VOICE_ID:
        print("Missing ElevenLabs API key or voice ID.")
        print("Set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID.")
        return

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    print("Generating Safe Thresher announcements...")

    for name, message in MESSAGES.items():
        try:
            generate_clip(name, message)
        except HTTPError as exc:
            details = exc.read().decode(
                "utf-8", errors="replace"
            )
            print(f"Failed {name}: HTTP {exc.code}")
            print(details[:400])
        except (URLError, OSError, ValueError) as exc:
            print(f"Failed {name}: {exc}")

    print("Finished.")
    print(f"Audio folder: {AUDIO_DIR}")


if __name__ == "__main__":
    main()
