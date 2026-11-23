"""Isolated HTTP gateway tests; no real tokens, network or Telegram session."""
import os
import unittest

os.environ.setdefault('MCP_PUBLIC_HOST', 'tg.example.com')

import httpx
from starlette.testclient import TestClient
from claude_gateway import create_app, MAX_BODY

PUBLIC = 'p' * 64
PRIVATE = 'b' * 64
BASE = 'https://tg.example.com'


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        def backend(request):
            self.requests.append(request)
            return httpx.Response(200, content=b'{"ok":true}', headers={
                'content-type': 'application/json', 'mcp-session-id': 'test-session',
                'Set-Cookie': 'must-not-leak=1'})
        self.client = TestClient(create_app(PUBLIC, PRIVATE, transport=httpx.MockTransport(backend)), base_url=BASE)

    def test_authentication_rejects_missing_wrong_and_backend_credentials(self):
        for value in ['', 'Bearer wrong', 'Bearer ' + PRIVATE]:
            response = self.client.post('/mcp', headers={'Authorization': value}, json={})
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.requests, [])

    def test_private_key_replaces_public_auth_and_only_protocol_headers_forwarded(self):
        response = self.client.post('/mcp', headers={'Authorization': 'Bearer ' + PUBLIC,
            'MCP-Protocol-Version': '2025-11-25', 'Cookie': 'private-cookie', 'Origin': 'https://claude.ai',
            'X-Forwarded-Host': 'evil.example'}, json={'jsonrpc': '2.0'})
        self.assertEqual(response.json(), {'ok': True})
        sent = self.requests[0]
        self.assertEqual(sent.headers['Authorization'], 'Bearer ' + PRIVATE)
        self.assertEqual(str(sent.url), 'http://127.0.0.1:18765/mcp')
        self.assertNotIn('cookie', sent.headers)
        self.assertNotIn('origin', sent.headers)
        self.assertNotIn('x-forwarded-host', sent.headers)
        self.assertEqual(response.headers['mcp-session-id'], 'test-session')
        self.assertNotIn('set-cookie', response.headers)

    def test_host_unknown_paths_and_body_size_are_bounded(self):
        headers = {'Authorization': 'Bearer ' + PUBLIC}
        self.assertEqual(self.client.post('/mcp', headers={**headers, 'Host': 'evil.example'}).status_code, 421)
        self.assertEqual(self.client.post('/mcp', headers=headers, content=b'x' * (MAX_BODY + 1)).status_code, 413)
        self.assertEqual(self.client.get('/private/mcp-token', headers=headers).status_code, 404)
        self.assertEqual(self.requests, [])

    def test_get_post_delete_and_stream_content_passthrough(self):
        for method in ['GET', 'POST', 'DELETE']:
            response = self.client.request(method, '/mcp', headers={'Authorization': 'Bearer ' + PUBLIC})
            self.assertEqual(response.content, b'{"ok":true}')
        self.assertEqual([r.method for r in self.requests], ['GET', 'POST', 'DELETE'])

    def test_unreachable_backend_is_honest_and_does_not_reveal_keys(self):
        def fail(request): raise httpx.ConnectError('withheld', request=request)
        client = TestClient(create_app(PUBLIC, PRIVATE, transport=httpx.MockTransport(fail)), base_url=BASE)
        response = client.post('/mcp', headers={'Authorization': 'Bearer ' + PUBLIC})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(PRIVATE, response.text)
        self.assertNotIn(PUBLIC, response.text)

    def test_credentials_must_be_distinct_and_long(self):
        for public, private in [('short', PRIVATE), (PUBLIC, 'short'), (PUBLIC, PUBLIC)]:
            with self.assertRaises(ValueError): create_app(public, private)


if __name__ == '__main__': unittest.main()
