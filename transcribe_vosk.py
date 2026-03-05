import json, sys
from vosk import Model, KaldiRecognizer

model_path = sys.argv[1]
wav_path = sys.argv[2]

model = Model(model_path)
rec = KaldiRecognizer(model, 16000)

with open(wav_path, 'rb') as f:
    f.read(44)  # WAV header
    while True:
        data = f.read(4000)
        if not data:
            break
        rec.AcceptWaveform(data)

print(json.loads(rec.FinalResult()).get('text', ''))
