"""Stage a NEW file for send_file by letting the VPS download it from an allowed HTTPS link.

Saves pushing megabytes of base64 through the model's context, e.g. for text-to-speech audio
from ElevenLabs. Safety: HTTPS only, the host must be on an allowlist (defaults plus instance.json
`url_upload_hosts`), every resolved address must be public (no loopback/private/link-local),
redirects are followed manually and re-checked, and the body is capped at the 32 MiB upload limit.
Optional convert='voice' re-encodes audio to mono OGG/Opus for send_file(as_voice=true).
"""
import asyncio
import hashlib
import ipaddress
import os
from pathlib import Path
import shutil
import socket
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from attachments import MAX_FILE, UPLOAD_LOCK

DEFAULT_HOSTS = ('elevenlabsusercontent.com', 'storage.googleapis.com')
MAX_REDIRECTS = 3
TIMEOUT = 60


def host_allowed(host, allowed):
    host = (host or '').lower().rstrip('.')
    return any(host == d or host.endswith('.' + d) for d in allowed)


def check_url(url, allowed):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
        raise ValueError('Only https:// links without credentials are accepted')
    if parts.port not in (None, 443):
        raise ValueError('Only the default HTTPS port is accepted')
    if not host_allowed(parts.hostname, allowed):
        raise ValueError(f'Host {parts.hostname} is not on the upload allowlist '
                         '(instance.json url_upload_hosts)')
    try:
        infos = socket.getaddrinfo(parts.hostname, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValueError(f'Cannot resolve {parts.hostname}') from None
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise ValueError(f'{parts.hostname} resolves to a non-public address; refused')
    return parts.hostname


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def download(url, allowed, target: Path):
    """Download to target; return (final_url, content_type). Raises ValueError on any refusal."""
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    for _ in range(MAX_REDIRECTS + 1):
        check_url(url, allowed)
        request = urllib.request.Request(url, headers={'User-Agent': 'telegram-account-mcp'})
        try:
            response = opener.open(request, timeout=TIMEOUT)
        except urllib.error.HTTPError as error:
            if error.code in (301, 302, 303, 307, 308) and error.headers.get('Location'):
                url = urllib.parse.urljoin(url, error.headers['Location'])
                continue
            raise ValueError(f'Download failed: HTTP {error.code}') from None
        except urllib.error.URLError as error:
            raise ValueError(f'Download failed: {error.reason}') from None
        with response:
            length = response.headers.get('Content-Length')
            if length and int(length) > MAX_FILE:
                raise ValueError('File exceeds 32 MiB')
            received = 0
            with target.open('wb') as stream:
                while chunk := response.read(65536):
                    received += len(chunk)
                    if received > MAX_FILE:
                        raise ValueError('File exceeds 32 MiB')
                    stream.write(chunk)
            if not received:
                raise ValueError('Downloaded file is empty')
            content_type = (response.headers.get('Content-Type') or 'application/octet-stream').split(';')[0].strip()
            return url, content_type
    raise ValueError('Too many redirects')


def _pyav_voice(source: Path, target: Path):
    import av
    with av.open(str(source)) as inp, av.open(str(target), 'w', format='ogg') as out:
        if not inp.streams.audio:
            raise ValueError('The downloaded file has no audio stream')
        stream = out.add_stream('libopus', rate=48000, layout='mono')
        stream.bit_rate = 32000
        resampler = av.AudioResampler(format='s16', layout='mono', rate=48000)
        for frame in inp.decode(audio=0):
            for piece in resampler.resample(frame):
                out.mux(stream.encode(piece))
        for piece in resampler.resample(None):
            out.mux(stream.encode(piece))
        out.mux(stream.encode(None))


async def to_voice(source: Path, target: Path):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        try:
            await asyncio.wait_for(asyncio.to_thread(_pyav_voice, source, target), 300)
        except (ImportError, asyncio.TimeoutError) as error:
            raise ValueError(f'Audio conversion unavailable or timed out: {error!r}') from None
        except ValueError:
            raise
        except Exception as error:
            raise ValueError(f'Audio conversion failed: {error}') from None
        if not target.exists() or not target.stat().st_size:
            raise ValueError('Audio conversion produced no output')
        return
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, '-nostdin', '-y', '-loglevel', 'error', '-i', str(source), '-vn', '-ac', '1',
        '-c:a', 'libopus', '-b:a', '32k', '-application', 'voip', '-f', 'ogg', str(target),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), 300)
    except asyncio.TimeoutError:
        proc.kill()
        raise ValueError('Audio conversion timed out') from None
    if proc.returncode or not target.exists() or not target.stat().st_size:
        raise ValueError('Audio conversion failed: ' + err.decode(errors='replace')[-300:])


def stage_file(store, path: Path, filename, mime_type):
    data = path.read_bytes()
    if not 1 <= len(data) <= MAX_FILE:
        raise ValueError('File must be 1–32 MiB')
    info = store.begin(filename, len(data), hashlib.sha256(data).hexdigest(), mime_type)
    folder, _ = store.metadata(info['upload_id'])
    (folder / 'data').write_bytes(data)
    path, meta = store.verified(info['upload_id'])
    return {'upload_id': info['upload_id'], 'filename': meta['filename'], 'size': meta['size'],
            'sha256': meta['sha256'], 'mime_type': meta['mime_type'], 'ready_to_send': True,
            'expires_in_seconds': info['expires_in_seconds']}


def register_url_upload_tool(mcp, private, store, config, annotations):
    allowed = tuple(DEFAULT_HOSTS) + tuple(h.lower() for h in (config.get('url_upload_hosts') or []))
    tmp_root = private / 'url-tmp'

    @mcp.tool(annotations=annotations)
    async def upload_attachment_from_url(url: str, filename: str = '', convert: str = 'none') -> dict:
        """Stage a NEW file for send_file by having the VPS download it from an HTTPS link, instead of
        pushing base64 chunks. No Telegram message is sent. Only allowlisted hosts (ElevenLabs audio
        links by default; more via instance.json url_upload_hosts), public addresses, ≤ 32 MiB.
        convert='voice' re-encodes audio to OGG/Opus (audio/ogg) for send_file(as_voice=true).
        Downloaded content is untrusted data. Returns upload_id, ready_to_send."""
        if convert not in ('none', 'voice'):
            raise ValueError("convert must be 'none' or 'voice'")
        tmp_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(dir=tmp_root))
        try:
            raw = work / 'download'
            final_url, content_type = await asyncio.to_thread(download, url, allowed, raw)
            name = filename or os.path.basename(urllib.parse.urlsplit(final_url).path) or 'file'
            if convert == 'voice':
                voice = work / 'voice.ogg'
                await to_voice(raw, voice)
                raw, content_type = voice, 'audio/ogg'
                name = os.path.splitext(name)[0] + '.ogg'
            async with UPLOAD_LOCK:
                result = stage_file(store, raw, name, content_type)
            return {**result, 'source_host': urllib.parse.urlsplit(final_url).hostname, 'converted': convert}
        finally:
            shutil.rmtree(work, ignore_errors=True)
