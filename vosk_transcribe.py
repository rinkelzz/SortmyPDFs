#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tempfile

MODEL_DIR = os.environ.get(
    "VOSK_MODEL_DIR",
    "/home/tim/.openclaw/workspace/vosk-models/vosk-model-de-0.21",
)


def to_wav_16k_mono(input_path: str, output_wav: str) -> None:
    # Convert anything ffmpeg understands (ogg/opus) to 16kHz mono s16le WAV.
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        input_path,
        "-ar",
        "16000",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        output_wav,
    ]
    subprocess.run(cmd, check=True)


def transcribe_wav(model_dir: str, wav_path: str) -> str:
    from vosk import KaldiRecognizer, Model

    model = Model(model_dir)
    rec = KaldiRecognizer(model, 16000)

    with open(wav_path, "rb") as f:
        f.read(44)  # WAV header
        while True:
            data = f.read(4000)
            if not data:
                break
            rec.AcceptWaveform(data)

    return json.loads(rec.FinalResult()).get("text", "").strip()


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: vosk_transcribe.py <audio-file>", file=sys.stderr)
        return 2

    audio_path = sys.argv[1]

    if not os.path.isdir(MODEL_DIR):
        print(
            f"Missing Vosk model dir: {MODEL_DIR}. Set VOSK_MODEL_DIR or download vosk-model-de-0.21.",
            file=sys.stderr,
        )
        return 3

    with tempfile.TemporaryDirectory(prefix="openclaw-vosk-") as td:
        wav_path = os.path.join(td, "audio.wav")
        to_wav_16k_mono(audio_path, wav_path)
        text = transcribe_wav(MODEL_DIR, wav_path)

    # IMPORTANT: Write transcript to stdout only.
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
