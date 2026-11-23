import base64
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from urllib.parse import urlparse,parse_qs,urlencode

import httpx
from mcp.server.auth.routes import create_auth_routes
from mcp.server.auth.settings import ClientRegistrationOptions,RevocationOptions
from mcp.server.auth.provider import RegistrationError,TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from oauth import Provider,digest

BASE='https://tg.example.com'
CALLBACK='https://chatgpt.com/connector/oauth/unit-test'

class OAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.provider=Provider(Path(self.tmp.name),BASE,'1753453708')
        self.client=OAuthClientInformationFull(client_id='unit-client',redirect_uris=[CALLBACK],
            token_endpoint_auth_method='none',grant_types=['authorization_code','refresh_token'],
            response_types=['code'],scope='telegram offline_access')
        await self.provider.register_client(self.client)
        app=Starlette(routes=create_auth_routes(provider=self.provider,issuer_url=AnyHttpUrl(BASE),
            client_registration_options=ClientRegistrationOptions(enabled=True,valid_scopes=['telegram','offline_access'],default_scopes=['telegram']),
            revocation_options=RevocationOptions(enabled=True)))
        self.http=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=BASE)
    async def asyncTearDown(self):await self.http.aclose();self.tmp.cleanup()

    async def grant(self):
        verifier='correct-verifier-'+'a'*48
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        r=await self.http.get('/authorize',params={'response_type':'code','client_id':'unit-client',
            'redirect_uri':CALLBACK,'code_challenge':challenge,'code_challenge_method':'S256',
            'scope':'telegram offline_access','state':'unit-state','resource':BASE+'/mcp'})
        self.assertEqual(r.status_code,302,r.text)
        flow=parse_qs(urlparse(r.headers['location']).query)['flow'][0]
        redirect=self.provider.approve(flow)
        values=parse_qs(urlparse(redirect).query)
        self.assertEqual(values['state'],['unit-state'])
        return values['code'][0],verifier

    async def test_pkce_code_single_use_and_refresh_rotation_survive_provider_reload(self):
        code,verifier=await self.grant()
        data={'grant_type':'authorization_code','client_id':'unit-client','code':code,
            'redirect_uri':CALLBACK,'code_verifier':'wrong-'+'b'*48,'resource':BASE+'/mcp'}
        wrong=await self.http.post('/token',data=data);self.assertEqual(wrong.status_code,400)
        data['code_verifier']=verifier
        good=await self.http.post('/token',data=data);self.assertEqual(good.status_code,200,good.text)
        token=good.json()
        replay=await self.http.post('/token',data=data);self.assertEqual(replay.status_code,400)
        reloaded=Provider(Path(self.tmp.name),BASE)
        access=await reloaded.load_access_token(token['access_token'])
        self.assertEqual(access.resource,BASE+'/mcp');self.assertEqual(access.subject,'1753453708')
        refresh=await self.http.post('/token',data={'grant_type':'refresh_token','client_id':'unit-client',
            'refresh_token':token['refresh_token'],'resource':BASE+'/mcp'})
        self.assertEqual(refresh.status_code,200,refresh.text)
        self.assertIsNone(await reloaded.load_access_token(token['access_token']))
        self.assertIsNone(await reloaded.load_refresh_token(self.client,token['refresh_token']))
        new=refresh.json()
        await reloaded.revoke_token(await reloaded.load_access_token(new['access_token']))
        self.assertIsNone(await reloaded.load_refresh_token(self.client,new['refresh_token']))

    async def test_registration_and_redirect_are_bound_to_chatgpt(self):
        for callback in ['https://evil.example/callback','https://chatgpt.com.evil.example/connector/oauth/x',
                         'https://chatgpt.com/redirect?url=https://evil.example','http://chatgpt.com/connector/oauth/x']:
            candidate=self.client.model_copy(update={'redirect_uris':[AnyHttpUrl(callback)]})
            with self.assertRaises(RegistrationError):await self.provider.register_client(candidate)
        r=await self.http.get('/authorize',params={'response_type':'code','client_id':'unit-client',
            'redirect_uri':'https://evil.example/callback','code_challenge':'a'*43,'code_challenge_method':'S256'})
        self.assertNotIn('evil.example',r.headers.get('location',''))
        self.assertGreaterEqual(r.status_code,400)

    async def test_wrong_resource_and_expired_tokens_are_rejected(self):
        r=await self.http.get('/authorize',params={'response_type':'code','client_id':'unit-client',
            'redirect_uri':CALLBACK,'code_challenge':'a'*43,'code_challenge_method':'S256','resource':'https://evil.example/mcp'})
        if r.status_code==302:self.assertIn('error=',r.headers['location'])
        else:self.assertGreaterEqual(r.status_code,400)
        self.provider.put('access',digest('expired'),{'client_id':'unit-client'},time.time()-1)
        self.assertIsNone(await self.provider.load_access_token('expired'))

if __name__=='__main__':unittest.main()
