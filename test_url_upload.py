import asyncio
import hashlib
from pathlib import Path
import shutil
import socket
import tempfile
import unittest
from unittest.mock import patch

from attachments import UploadStore
import url_upload
from url_upload import check_url, host_allowed, stage_file

ALLOWED = url_upload.DEFAULT_HOSTS


def resolve_to(address):
    return lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))]


class UrlCheckTests(unittest.TestCase):
    def test_host_allowlist(self):
        self.assertTrue(host_allowed('elevenlabsusercontent.com', ALLOWED))
        self.assertTrue(host_allowed('a.b.elevenlabsusercontent.com', ALLOWED))
        self.assertFalse(host_allowed('evilelevenlabsusercontent.com', ALLOWED))
        self.assertFalse(host_allowed('elevenlabsusercontent.com.evil.io', ALLOWED))

    def test_rejects_scheme_port_credentials_and_unlisted_hosts(self):
        with patch('url_upload.socket.getaddrinfo', resolve_to('34.1.2.3')):
            for url in ['http://elevenlabsusercontent.com/a.mp3',
                        'https://elevenlabsusercontent.com:8443/a.mp3',
                        'https://user:pw@elevenlabsusercontent.com/a.mp3',
                        'https://example.com/a.mp3', 'file:///etc/passwd']:
                with self.assertRaises(ValueError, msg=url):
                    check_url(url, ALLOWED)
            self.assertEqual(check_url('https://elevenlabsusercontent.com/a.mp3', ALLOWED),
                             'elevenlabsusercontent.com')

    def test_rejects_private_addresses(self):
        for address in ['127.0.0.1', '10.0.0.5', '169.254.169.254', '192.168.1.1', '::1']:
            with patch('url_upload.socket.getaddrinfo', resolve_to(address)):
                with self.assertRaises(ValueError, msg=address):
                    check_url('https://elevenlabsusercontent.com/a.mp3', ALLOWED)


class StageTests(unittest.TestCase):
    def test_stage_file_is_verified_and_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            store = UploadStore(Path(directory) / 'uploads')
            source = Path(directory) / 'x.ogg'
            source.write_bytes(b'OggS' + b'\x00' * 100)
            result = stage_file(store, source, 'x.ogg', 'audio/ogg')
            self.assertTrue(result['ready_to_send'])
            self.assertEqual(result['sha256'], hashlib.sha256(source.read_bytes()).hexdigest())
            path, meta = store.verified(result['upload_id'])
            self.assertEqual(path.read_bytes(), source.read_bytes())
            self.assertEqual(meta['mime_type'], 'audio/ogg')


class VoiceConversionTests(unittest.TestCase):
    def test_converts_wav_to_ogg_opus_with_pyav(self):
        import math, struct, wave
        with tempfile.TemporaryDirectory() as directory, patch('url_upload.shutil.which', return_value=None):
            source, target = Path(directory) / 'in.wav', Path(directory) / 'out.ogg'
            with wave.open(str(source), 'wb') as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
                w.writeframes(b''.join(struct.pack('<h', int(8000*math.sin(i/10))) for i in range(22050)))
            asyncio.run(url_upload.to_voice(source, target))
            self.assertEqual(target.read_bytes()[:4], b'OggS')
            self.assertIn(b'OpusHead', target.read_bytes()[:200])

    def test_non_audio_is_refused(self):
        with tempfile.TemporaryDirectory() as directory, patch('url_upload.shutil.which', return_value=None):
            source = Path(directory) / 'in.mp3'
            source.write_bytes(b'not audio at all' * 50)
            with self.assertRaises(ValueError):
                asyncio.run(url_upload.to_voice(source, Path(directory) / 'out.ogg'))


if __name__ == '__main__':
    unittest.main()
