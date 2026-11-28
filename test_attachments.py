import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from attachments import MAX_CHUNK, RETENTION, UploadStore, register_attachment_tools
import server


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = UploadStore(Path(self.temporary.name)/'uploads')

    def begin(self, data):
        return self.store.begin('тест.pdf',len(data),hashlib.sha256(data).hexdigest(),'application/pdf')['upload_id']

    def test_chunk_reassembly_and_identical_retry(self):
        data = b'0123456789'*30000
        uid = self.begin(data)
        first = base64.b64encode(data[:MAX_CHUNK]).decode()
        self.assertFalse(self.store.append(uid,0,first)['ready_to_send'])
        self.assertEqual(self.store.append(uid,0,first)['received_bytes'],MAX_CHUNK)
        self.assertTrue(self.store.append(uid,MAX_CHUNK,base64.b64encode(data[MAX_CHUNK:]).decode())['sha256_verified'])
        path,meta = self.store.verified(uid)
        self.assertEqual(path.read_bytes(),data)
        self.assertEqual(meta['filename'],'тест.pdf')

    def test_missing_bad_hash_and_conflicting_retry(self):
        uid = self.begin(b'hello')
        with self.assertRaises(ValueError): self.store.verified(uid)
        self.store.append(uid,0,base64.b64encode(b'he').decode())
        with self.assertRaises(ValueError): self.store.append(uid,0,base64.b64encode(b'xx').decode())
        with self.assertRaises(ValueError): self.store.append(uid,2,base64.b64encode(b'xxx').decode())

    def test_offsets_limits_and_paths(self):
        uid = self.begin(b'hello')
        for offset,data in [(1,b'h'),(-1,b'h'),(0,b'123456'),(0,b'x'*(MAX_CHUNK+1))]:
            with self.assertRaises(ValueError): self.store.append(uid,offset,base64.b64encode(data).decode())
        for filename in ['../secret','/secret','..','x\\y','x\n.txt']:
            with self.assertRaises(ValueError): self.store.begin(filename,1,'a'*64,'text/plain')
        with self.assertRaises(ValueError): self.store.metadata('../secret')
        with self.assertRaises(ValueError): self.store.append(uid,0,'not valid base64')

    def test_expiration(self):
        uid = self.begin(b'hello')
        with patch('attachments.time.time',return_value=__import__('time').time()+RETENTION+1):
            with self.assertRaises(ValueError): self.store.verified(uid)


class MutationTests(unittest.IsolatedAsyncioTestCase):
    async def test_idempotency_payload_collision_and_uncertain_send(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(server,'PRIVATE',Path(directory)):
            calls = []
            async def send():
                calls.append(1)
                return {'message_id':123}
            first = await server.mutate_once('forward-test-01',['a'],send)
            second = await server.mutate_once('forward-test-01',['a'],send)
            self.assertEqual(first['message_id'],second['message_id'])
            self.assertTrue(second['duplicate_prevented'])
            self.assertEqual(len(calls),1)
            with self.assertRaises(ValueError): await server.mutate_once('forward-test-01',['b'],send)
            async def fail(): raise RuntimeError('simulated uncertain outcome')
            with self.assertRaises(RuntimeError): await server.mutate_once('uncertain-test-01',['x'],fail)
            with self.assertRaises(ValueError): await server.mutate_once('uncertain-test-01',['x'],send)
            self.assertEqual(len(calls),1)

    async def test_forward_validates_source_destination_and_protection(self):
        tools = {}
        class Registry:
            def tool(self,**kwargs):
                def register(function):
                    tools[function.__name__]=function
                    return function
                return register
        client = SimpleNamespace(protected=False,forwarded=0)
        async def get_messages(entity,ids):
            return [SimpleNamespace(id=i,chat_id=1,noforwards=client.protected) for i in ids]
        async def forward_messages(entity,messages,from_peer):
            client.forwarded += 1
            return [SimpleNamespace(id=100+i,file=SimpleNamespace()) for i,_ in enumerate(messages)]
        client.get_messages=get_messages
        client.forward_messages=forward_messages
        @asynccontextmanager
        async def telegram(): yield client
        async def checked(client,chat_id,expected):
            if expected != str(chat_id): raise ValueError('identity mismatch')
            return SimpleNamespace(noforwards=False),{'chat_id':chat_id}
        async def mutate(key,payload,operation): return await operation()
        with tempfile.TemporaryDirectory() as directory:
            register_attachment_tools(Registry(),Path(directory),telegram,checked,mutate,server.READ,server.WRITE)
            forward=tools['forward_messages']
            result=await forward(1,'1',[10,11],2,'2','forward-safe-test')
            self.assertEqual(result['attachment_count'],2)
            for args in [(1,'wrong',[10],2,'2','forward-wrong-source'),(1,'1',[10],2,'wrong','forward-wrong-target')]:
                with self.assertRaises(ValueError): await forward(*args)
            client.protected=True
            with self.assertRaises(ValueError): await forward(1,'1',[10],2,'2','forward-protected')
            self.assertEqual(client.forwarded,1)

    async def test_send_file_as_voice(self):
        import base64, hashlib
        tools = {}
        class Registry:
            def tool(self,**kwargs):
                def register(function):
                    tools[function.__name__]=function
                    return function
                return register
        calls = []
        async def send_file(entity, path, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(id=7, date=__import__('datetime').datetime(2026,11,23),
                                   voice=kwargs.get('voice_note'))
        client = SimpleNamespace(send_file=send_file)
        @asynccontextmanager
        async def telegram(): yield client
        async def checked(client,chat_id,expected):
            return SimpleNamespace(),{'chat_id':chat_id}
        async def mutate(key,payload,operation):
            calls.append(payload)
            return await operation()
        data = b'OggS-fake-opus'
        digest = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            register_attachment_tools(Registry(),Path(directory),telegram,checked,mutate,server.READ,server.WRITE)
            for name, mime in (('v.ogg','audio/ogg'), ('v.mp3','audio/mpeg')):
                up = await tools['begin_attachment_upload'](name, len(data), digest, mime)
                await tools['append_attachment_upload'](up['upload_id'], 0, base64.b64encode(data).decode())
                if mime == 'audio/mpeg':
                    with self.assertRaisesRegex(ValueError, 'OGG'):
                        await tools['send_file'](5, 'x', up['upload_id'], 'voice-test-mp3', as_voice=True)
                    continue
                result = await tools['send_file'](5, 'x', up['upload_id'], 'voice-test-ogg', as_voice=True)
                self.assertTrue(result['voice'])
                self.assertTrue(calls[-1]['voice_note'])
                self.assertNotIn('force_document', calls[-1])
                self.assertEqual(calls[-2][-1], 'voice')  # payload marks voice; legacy payloads unchanged
                plain = await tools['send_file'](5, 'x', up['upload_id'], 'voice-test-doc')
                self.assertNotIn('voice', plain)
                self.assertTrue(calls[-1]['force_document'])

    async def test_send_file_as_photo_uses_real_name(self):
        import base64, hashlib
        tools = {}
        class Registry:
            def tool(self,**kwargs):
                def register(function):
                    tools[function.__name__]=function
                    return function
                return register
        calls = []
        async def upload_file(path, file_name=None):
            calls.append(('upload', file_name))
            return SimpleNamespace(name=file_name)
        async def send_file(entity, file, **kwargs):
            calls.append(('send', file, kwargs))
            return SimpleNamespace(id=8, date=__import__('datetime').datetime(2026,11,28),
                                   photo=object() if getattr(file, 'name', '').endswith('.png') else None)
        client = SimpleNamespace(send_file=send_file, upload_file=upload_file)
        @asynccontextmanager
        async def telegram(): yield client
        async def checked(client,chat_id,expected):
            return SimpleNamespace(),{'chat_id':chat_id}
        async def mutate(key,payload,operation):
            return await operation()
        data = b'\x89PNG-fake'
        with tempfile.TemporaryDirectory() as directory:
            register_attachment_tools(Registry(),Path(directory),telegram,checked,mutate,server.READ,server.WRITE)
            up = await tools['begin_attachment_upload']('scheme.png', len(data), hashlib.sha256(data).hexdigest(), 'image/png')
            await tools['append_attachment_upload'](up['upload_id'], 0, base64.b64encode(data).decode())
            result = await tools['send_file'](5, 'x', up['upload_id'], 'photo-test-png', send_as='photo')
            self.assertTrue(result['photo'])
            self.assertEqual(calls[0], ('upload', 'photo.png'))
            self.assertFalse(calls[1][2]['force_document'])


if __name__ == '__main__':
    unittest.main()
