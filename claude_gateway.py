"""Claude's separately revocable HTTPS entry to the existing private MCP backend.

This process never opens a Telegram session. Keys stay in the existing private directory.
"""
import hmac
import os
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
import uvicorn

# Public HTTPS host name of this gateway: env MCP_PUBLIC_HOST, else private/public-host.
PUBLIC_HOST = (os.environ.get('MCP_PUBLIC_HOST') or
               (Path(__file__).resolve().parent / 'private/public-host').read_text().strip()).lower()
UPSTREAM = 'http://127.0.0.1:18765/mcp'
MAX_BODY = 2 * 1024 * 1024
REQUEST_HEADERS = {'accept', 'content-type', 'mcp-session-id', 'mcp-protocol-version', 'last-event-id'}
RESPONSE_HEADERS = {'content-type', 'mcp-session-id', 'mcp-protocol-version', 'cache-control',
                    'retry-after', 'allow', 'www-authenticate'}


def create_app(public_token, backend_token, *, transport=None):
    if len(public_token) < 48 or len(backend_token) < 32 or hmac.compare_digest(public_token, backend_token):
        raise ValueError('Distinct sufficiently long gateway and backend credentials are required')

    async def proxy(request):
        if request.headers.get('host', '').lower() not in {PUBLIC_HOST, PUBLIC_HOST + ':443',
                                                          '127.0.0.1:18767', 'localhost:18767'}:
            return Response('Misdirected request', 421)
        supplied = request.headers.get('authorization', '')
        if not hmac.compare_digest(supplied.encode(), ('Bearer ' + public_token).encode()):
            return Response('Unauthorized', 401, headers={'WWW-Authenticate': 'Bearer realm="Stan_VPS_tg"',
                                                         'Cache-Control': 'no-store'})
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BODY:
                return Response('Request too large', 413, headers={'Cache-Control': 'no-store'})
            chunks.append(chunk)
        # Only MCP protocol headers are forwarded. Incoming cookies, Origin, Host and
        # credentials cannot replace the loopback authority or the private backend key.
        forwarded = {k: v for k, v in request.headers.items() if k.lower() in REQUEST_HEADERS}
        forwarded.update({'Authorization': 'Bearer ' + backend_token, 'Accept-Encoding': 'identity'})
        client = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=5), trust_env=False,
                                   follow_redirects=False, transport=transport)
        try:
            result = await client.send(client.build_request(request.method, UPSTREAM,
                                       headers=forwarded, content=b''.join(chunks)), stream=True)
        except httpx.HTTPError:
            await client.aclose()
            return Response('Telegram service unavailable', 503, headers={'Cache-Control': 'no-store'})

        async def stream():
            try:
                async for data in result.aiter_bytes():
                    yield data
            finally:
                await result.aclose()
                await client.aclose()
        headers = {k: v for k, v in result.headers.items() if k.lower() in RESPONSE_HEADERS}
        headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
        return StreamingResponse(stream(), status_code=result.status_code, headers=headers)

    async def health(request):
        return JSONResponse({'ready': True}, headers={'Cache-Control': 'no-store'})

    return Starlette(routes=[Route('/mcp', proxy, methods=['GET', 'POST', 'DELETE']),
                             Route('/healthz', health)])


if __name__ == '__main__':
    os.umask(0o077)
    private = Path(__file__).resolve().parent / 'private'
    app = create_app((private / 'claude-mcp-token').read_text().strip(),
                     (private / 'mcp-token').read_text().strip())
    uvicorn.run(app, host='127.0.0.1', port=18767, access_log=False, log_level='critical')
