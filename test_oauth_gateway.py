import atexit
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
import httpx
from telethon import errors

_directory=tempfile.TemporaryDirectory();atexit.register(_directory.cleanup)
os.environ['TG_MCP_PRIVATE']=_directory.name
Path(_directory.name,'instance.json').write_text(json.dumps({'name':'Test_TG','owner_label':'владельца','public_url':'https://tg.example.com','expected_account_id':1753453708}))
import oauth_gateway as gateway
gateway.EXPECTED=1753453708  # CONFIG may already be loaded by another test module
from oauth import Provider,digest

class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.private=Path(self.tmp.name)
        self.key='unit-bootstrap-key-'+('a'*40)
        self.record={'digest':digest(self.key),'expires':time.time()+300,'used':False}
        (self.private/'bootstrap.json').write_text(json.dumps(self.record))
        self.patches=[patch.object(gateway,'PRIVATE',self.private),patch.object(gateway,'provider',Provider(self.private,gateway.PUBLIC)),
            patch.object(gateway,'STATE',{'stage':'credentials'}),patch.object(gateway,'CLIENT',None),patch.object(gateway,'ATTEMPTS',{}),
            patch.object(gateway,'TASK',None),patch.object(gateway,'LOGIN_PHONE',None),patch.object(gateway,'PHONE_CODE_HASH',None)]
        for p in self.patches:p.start()
        self.http=httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),base_url=gateway.PUBLIC)
    async def asyncTearDown(self):
        await self.http.aclose()
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    async def authorize(self):
        return await self.http.post('/setup/access',headers={'Origin':gateway.PUBLIC},json={'key':self.key})

    async def test_personal_link_is_single_use_and_only_cookie_allows_state(self):
        self.assertEqual((await self.http.get('/setup/state')).status_code,403)
        good=await self.authorize();self.assertEqual(good.status_code,200)
        self.assertIn('Secure',good.headers['set-cookie']);self.assertIn('HttpOnly',good.headers['set-cookie'])
        state=await self.http.get('/setup/state');self.assertEqual(state.json()['stage'],'credentials')
        again=await self.authorize();self.assertEqual(again.status_code,200)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),base_url=gateway.PUBLIC) as outsider:
            reused=await outsider.post('/setup/access',headers={'Origin':gateway.PUBLIC},json={'key':self.key})
            self.assertEqual(reused.status_code,403)
        self.assertEqual(state.headers['cache-control'],'no-store')

    async def test_wrong_expired_and_cross_origin_bootstrap_are_rejected(self):
        wrong=await self.http.post('/setup/access',headers={'Origin':gateway.PUBLIC},json={'key':'wrong'})
        self.assertEqual(wrong.status_code,403)
        cross=await self.http.post('/setup/access',headers={'Origin':'https://evil.example'},json={'key':self.key})
        self.assertEqual(cross.status_code,403)
        self.record['expires']=0;(self.private/'bootstrap.json').write_text(json.dumps(self.record))
        self.assertEqual((await self.authorize()).status_code,403)

    async def test_invalid_credentials_do_not_start_telegram_and_wrong_account_is_logged_out(self):
        await self.authorize()
        with patch.object(gateway,'TelegramClient') as factory:
            r=await self.http.post('/setup/credentials',headers={'Origin':gateway.PUBLIC},json={'api_id':'123','api_hash':'12345678'})
            self.assertEqual(r.status_code,400);factory.assert_not_called()
        client=SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=123)),log_out=AsyncMock(),disconnect=AsyncMock())
        with patch.object(gateway,'CLIENT',client):await gateway.finish_login()
        client.log_out.assert_awaited_once();client.disconnect.assert_awaited_once()
        self.assertFalse((self.private/'ready.json').exists())
        self.assertEqual(gateway.STATE['stage'],'error')

    async def test_mcp_remains_protected_and_connect_rejects_bad_csrf(self):
        for value in ['', 'Bearer wrong']:
            r=await self.http.post('/mcp',headers={'Authorization':value},json={})
            self.assertEqual(r.status_code,401)
            self.assertIn('resource_metadata=',r.headers['www-authenticate'])
        r=await self.http.post('/connect',headers={'Origin':gateway.PUBLIC},data={'flow':'bad','csrf':'bad','password':'bad'})
        self.assertEqual(r.status_code,400)

    async def test_consent_browser_policy_preserves_origin_and_approved_form_redirects(self):
        gateway.provider.put('flow','test-flow',{'csrf':'test-csrf'},time.time()+60)
        salt='a1'*16
        hashed=hashlib.pbkdf2_hmac('sha256',b'test-only-code',bytes.fromhex(salt),300000).hex()
        (self.private/'connection-password.json').write_text(json.dumps({'salt':salt,'hash':hashed}))
        response=await self.http.get('/connect?flow=test-flow')
        self.assertEqual(response.headers['referrer-policy'],'same-origin')
        form_action=next(x.strip() for x in response.headers['content-security-policy'].split(';') if x.strip().startswith('form-action '))
        self.assertIn('https://chatgpt.com/connector/oauth/',form_action)
        self.assertIn('https://chatgpt.com/connector_platform_oauth_redirect',form_action)
        self.assertNotIn('*',form_action)
        self.assertEqual(gateway.page('non OAuth page').headers['content-security-policy'].split('form-action ')[1].split(';')[0],"'self'")
        self.assertIn('name="csrf" value="test-csrf"',response.text)
        data={'flow':'test-flow','csrf':'test-csrf','password':'test-only-code'}
        with patch.object(gateway.provider,'approve',return_value='https://chatgpt.com/connector_platform_oauth_redirect?code=test') as approve:
            for origin in ['null','https://evil.example','']:
                denied=await self.http.post('/connect',headers={'Origin':origin},data=data)
                self.assertEqual(denied.status_code,403)
            bad_csrf=await self.http.post('/connect',headers={'Origin':gateway.PUBLIC},data={**data,'csrf':'wrong'})
            self.assertEqual(bad_csrf.status_code,400)
            wrong=await self.http.post('/connect',headers={'Origin':gateway.PUBLIC},data={**data,'password':'wrong'})
            self.assertIn('Код подключения не подошёл',wrong.text)
            approve.assert_not_called()
            good=await self.http.post('/connect',headers={'Origin':gateway.PUBLIC},data=data)
            self.assertEqual(good.status_code,303)
            approve.assert_called_once_with('test-flow')

    async def test_expired_consent_explains_how_to_start_fresh_without_granting_access(self):
        with patch.object(gateway.provider,'approve') as approve:
            expired=await self.http.post('/connect',headers={'Origin':gateway.PUBLIC},data={'flow':'missing','csrf':'x','password':'x'})
            self.assertEqual(expired.status_code,400)
            self.assertIn('Запрос истёк или уже использован',expired.text)
            self.assertIn('Пересоздавать плагин',expired.text)
            approve.assert_not_called()

    async def test_failed_entry_can_retry_but_completed_entry_cannot_be_reset(self):
        await self.authorize();gateway.STATE.update(stage='error')
        result=await self.http.post('/setup/retry',headers={'Origin':gateway.PUBLIC},json={})
        self.assertEqual(result.status_code,200);self.assertEqual(gateway.STATE['stage'],'credentials')
        (self.private/'ready.json').write_text('{}')
        result=await self.http.post('/setup/retry',headers={'Origin':gateway.PUBLIC},json={})
        self.assertEqual(result.status_code,409)

    async def prepare_phone(self):
        await self.authorize();gateway.STATE.update(stage='phone')
        (self.private/'owner-phone').write_text('+15555550100')
        return SimpleNamespace(is_user_authorized=AsyncMock(return_value=False),
            send_code_request=AsyncMock(return_value=SimpleNamespace(phone_code_hash='private-test-hash',type=SimpleNamespace())),
            sign_in=AsyncMock())

    async def test_code_is_requested_only_for_verified_owner_number_and_hash_stays_private(self):
        client=await self.prepare_phone()
        with patch.object(gateway,'CLIENT',client):
            wrong=await self.http.post('/setup/phone',headers={'Origin':gateway.PUBLIC},json={'phone':'+15555550999'})
            self.assertEqual(wrong.status_code,400);client.send_code_request.assert_not_awaited()
            good=await self.http.post('/setup/phone',headers={'Origin':gateway.PUBLIC},json={'phone':'+1 (555) 555-0100'})
            self.assertEqual(good.status_code,200)
            client.send_code_request.assert_awaited_once_with('+15555550100')
            state=await self.http.get('/setup/state');self.assertEqual(state.json()['stage'],'code')
            self.assertNotIn('private-test-hash',state.text);self.assertNotIn('15555550100',state.text)

    async def test_invalid_code_can_retry_and_two_factor_transition_does_not_repeat_code_request(self):
        client=await self.prepare_phone()
        with patch.object(gateway,'CLIENT',client):
            await self.http.post('/setup/phone',headers={'Origin':gateway.PUBLIC},json={'phone':'+15555550100'})
            client.sign_in.side_effect=[errors.PhoneCodeInvalidError(request=None),errors.SessionPasswordNeededError(request=None)]
            wrong=await self.http.post('/setup/code',headers={'Origin':gateway.PUBLIC},json={'code':'12345'})
            self.assertEqual(wrong.status_code,400);self.assertEqual(gateway.STATE['stage'],'code')
            password=await self.http.post('/setup/code',headers={'Origin':gateway.PUBLIC},json={'code':'54321'})
            self.assertEqual(password.status_code,200);self.assertEqual(gateway.STATE['stage'],'password')
            self.assertIsNone(gateway.PHONE_CODE_HASH);client.send_code_request.assert_awaited_once()

    async def test_successful_code_finishes_login_and_expired_code_returns_to_phone(self):
        client=await self.prepare_phone()
        with patch.object(gateway,'CLIENT',client):
            await self.http.post('/setup/phone',headers={'Origin':gateway.PUBLIC},json={'phone':'+15555550100'})
            client.sign_in.side_effect=errors.PhoneCodeExpiredError(request=None)
            expired=await self.http.post('/setup/code',headers={'Origin':gateway.PUBLIC},json={'code':'12345'})
            self.assertEqual(expired.status_code,400);self.assertEqual(gateway.STATE['stage'],'phone')
            client.sign_in.side_effect=None
            await self.http.post('/setup/phone',headers={'Origin':gateway.PUBLIC},json={'phone':'+15555550100'})
            with patch.object(gateway,'finish_login',new_callable=AsyncMock) as finish:
                good=await self.http.post('/setup/code',headers={'Origin':gateway.PUBLIC},json={'code':'54 321'})
                self.assertEqual(good.status_code,200);finish.assert_awaited_once()
                self.assertIsNone(gateway.PHONE_CODE_HASH)
                client.sign_in.assert_awaited_with(phone='+15555550100',code='54321',phone_code_hash='private-test-hash')

    async def test_delivery_type_is_exact_and_resend_is_blocked_until_telegram_allows_it(self):
        client=await self.prepare_phone()
        email_type=type('SentCodeTypeEmailCode',(),{'email_pattern':'b***@example.test'})()
        sent=SimpleNamespace(phone_code_hash='private-hash',type=email_type,next_type=SimpleNamespace(),timeout=90)
        client.send_code_request.return_value=sent
        with patch.object(gateway,'CLIENT',client):
            await self.http.post('/setup/phone',headers={'Origin':gateway.PUBLIC},json={'phone':'+15555550100'})
            self.assertEqual(gateway.STATE['delivery_type'],'SentCodeTypeEmailCode')
            self.assertIn('b***@example.test',gateway.STATE['delivery'])
            state=await self.http.get('/setup/state');self.assertNotIn('private-hash',state.text)
            self.assertTrue((self.private/'pending-code.json').exists())
            blocked=await self.http.post('/setup/resend',headers={'Origin':gateway.PUBLIC},json={})
            self.assertEqual(blocked.status_code,429)
            gateway.STATE['resend_after']=0;gateway.STATE['next_type']=None
            absent=await self.http.post('/setup/resend',headers={'Origin':gateway.PUBLIC},json={})
            self.assertEqual(absent.status_code,409)

if __name__=='__main__':unittest.main()
