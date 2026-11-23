"""Single-owner OAuth provider. SDK supplies PKCE/redirect/token endpoint validation."""
from __future__ import annotations
import hashlib
import json
import re
import secrets
import sqlite3
import time
from urllib.parse import urlencode, urlparse

from mcp.server.auth.provider import (AuthorizationCode, RefreshToken, AccessToken,
    TokenError, RegistrationError)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken


def digest(value): return hashlib.sha256(value.encode()).hexdigest()

class Provider:
    def __init__(self, private, base, subject=''):
        self.private, self.base, self.subject = private, base.rstrip('/'), str(subject)
        self.resource = self.base + '/mcp'
        private.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS oauth (kind TEXT, key TEXT, data TEXT, expiry REAL, PRIMARY KEY(kind,key))')

    def db(self): return sqlite3.connect(self.private/'oauth.sqlite3')
    def put(self, kind, key, data, expiry):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO oauth VALUES (?,?,?,?)', (kind,key,json.dumps(data),expiry))
            db.execute('DELETE FROM oauth WHERE expiry<?', (time.time(),))
    def get(self, kind, key, consume=False):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT data,expiry FROM oauth WHERE kind=? AND key=?',(kind,key)).fetchone()
            if not row or row[1] < time.time(): return None
            if consume: db.execute('DELETE FROM oauth WHERE kind=? AND key=?',(kind,key))
            return json.loads(row[0])

    async def get_client(self, client_id):
        value=self.get('client',client_id)
        return OAuthClientInformationFull.model_validate(value) if value else None

    async def register_client(self, client_info):
        for uri in client_info.redirect_uris:
            u=urlparse(str(uri))
            allowed=(u.scheme=='https' and u.netloc=='chatgpt.com' and not u.query and not u.fragment
                and (u.path in ['/connector_platform_oauth_redirect','/connector_platform/oauth/callback']
                     or re.fullmatch(r'/connector/oauth/[A-Za-z0-9_-]+',u.path)))
            if not allowed: raise RegistrationError('invalid_redirect_uri','Only ChatGPT callback URLs are allowed')
        with self.db() as db:
            if db.execute("SELECT COUNT(*) FROM oauth WHERE kind='client'").fetchone()[0]>=100:
                raise RegistrationError('invalid_client_metadata','Registration capacity reached')
        self.put('client',client_info.client_id,client_info.model_dump(mode='json'),time.time()+10*365*86400)

    async def authorize(self, client, params):
        if params.resource and params.resource.rstrip('/') != self.resource:
            from mcp.server.auth.provider import AuthorizeError
            raise AuthorizeError('invalid_request','Wrong resource')
        flow=secrets.token_urlsafe(32)
        self.put('flow',flow,{'client_id':client.client_id,'params':params.model_dump(mode='json'),
                            'csrf':secrets.token_urlsafe(32)},time.time()+600)
        return self.base+'/connect?'+urlencode({'flow':flow})

    def approve(self, flow):
        value=self.get('flow',flow,consume=True)
        if not value: raise ValueError('Expired authorization request')
        p=value['params'];code=secrets.token_urlsafe(32)
        record=AuthorizationCode(code=code,scopes=p.get('scopes') or ['telegram'],
            expires_at=time.time()+120,client_id=value['client_id'],code_challenge=p['code_challenge'],
            redirect_uri=p['redirect_uri'],redirect_uri_provided_explicitly=p['redirect_uri_provided_explicitly'],
            resource=self.resource,subject=self.subject)
        self.put('code',digest(code),record.model_dump(mode='json',exclude={'code'}),record.expires_at)
        args={'code':code}
        if p.get('state') is not None: args['state']=p['state']
        return p['redirect_uri']+('?' if '?' not in p['redirect_uri'] else '&')+urlencode(args)

    async def load_authorization_code(self, client, authorization_code):
        data=self.get('code',digest(authorization_code))
        if not data or data['client_id']!=client.client_id: return None
        return AuthorizationCode(code=authorization_code,**data)

    def mint(self, client_id, scopes, family=None):
        family=family or secrets.token_urlsafe(24)
        access,refresh=secrets.token_urlsafe(48),secrets.token_urlsafe(48)
        for kind,token,lifetime in [('access',access,3600),('refresh',refresh,30*86400)]:
            self.put(kind,digest(token),{'client_id':client_id,'scopes':scopes,
                'expires_at':int(time.time())+lifetime,'resource':self.resource,
                'subject':self.subject,'family':family},time.time()+lifetime)
        return OAuthToken(access_token=access,expires_in=3600,refresh_token=refresh,scope=' '.join(scopes))

    async def exchange_authorization_code(self, client, authorization_code):
        data=self.get('code',digest(authorization_code.code),consume=True)
        if not data or data['client_id']!=client.client_id: raise TokenError('invalid_grant','Code expired or already used')
        return self.mint(client.client_id,data['scopes'])

    async def load_refresh_token(self, client, refresh_token):
        data=self.get('refresh',digest(refresh_token))
        if not data or data['client_id']!=client.client_id: return None
        return RefreshToken(token=refresh_token,**{k:v for k,v in data.items() if k!='family'})

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        data=self.get('refresh',digest(refresh_token.token),consume=True)
        if not data or data['client_id']!=client.client_id: raise TokenError('invalid_grant','Refresh token expired or already used')
        if not set(scopes).issubset(data['scopes']): raise TokenError('invalid_scope')
        self.revoke_family(data['family'])
        return self.mint(client.client_id,scopes or data['scopes'],data['family'])

    async def load_access_token(self, token):
        data=self.get('access',digest(token))
        if not data: return None
        return AccessToken(token=token,**{k:v for k,v in data.items() if k!='family'})

    def revoke_family(self, family):
        with self.db() as db:
            rows=db.execute("SELECT kind,key,data FROM oauth WHERE kind IN ('access','refresh')").fetchall()
            for kind,key,data in rows:
                if json.loads(data).get('family')==family:
                    db.execute('DELETE FROM oauth WHERE kind=? AND key=?',(kind,key))

    async def revoke_token(self, token):
        for kind in ['access','refresh']:
            data=self.get(kind,digest(token.token))
            if data: self.revoke_family(data['family'])
