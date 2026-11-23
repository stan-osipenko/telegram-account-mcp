"""Whisper для transcribe_voice: на VPS лежит как ~/services/whisper/transcribe.py и запускается
из ~/services/whisper/.venv. Печатает JSON {text, language, duration}. Модель small, int8, CPU, ru."""
import json
import os
import sys

from faster_whisper import WhisperModel

MODEL = os.environ.get('WHISPER_MODEL', 'small')

model = WhisperModel(MODEL, device='cpu', compute_type='int8', cpu_threads=1,
                     download_root=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'models'))
segments, info = model.transcribe(sys.argv[1], language='ru', beam_size=1, vad_filter=True)
text = ' '.join(s.text.strip() for s in segments).strip()
print(json.dumps({'text': text, 'language': info.language, 'duration': round(info.duration, 1)}, ensure_ascii=False))
