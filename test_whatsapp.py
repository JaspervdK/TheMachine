import importlib.util
from pathlib import Path
import tempfile,unittest
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('assistant_wa',Path(__file__).with_name('assistant.py'));a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
account={'id':'personal-whatsapp','identity_sha256':a.hashlib.sha256(b'123@s.whatsapp.net').hexdigest()}
def message():return {'ChatJID':'12345@g.us','MsgID':'FULL-NATIVE-ID','Timestamp':'2026-10-09T12:00:00Z','FromMe':False,'SenderJID':'12345@g.us','Text':'Hello','ChatName':'Family'}
class WhatsAppTests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.store=a.Store(Path(self.tmp.name)/'a.sqlite')
 def tearDown(self):self.tmp.cleanup()
 def test_account_mismatch_and_mutation_blocked(self):
  source=a.WhatsApp({},account)
  with patch.object(source,'read',return_value={'authenticated':True,'linked_jid':'wrong'}),self.assertRaises(a.SourceError):source.profile()
  with self.assertRaises(a.SourceError):source.read(['send','text'])
 def test_group_sender_not_invented_and_native_identity_preserved(self):
  raw=message();raw['ChatJID']='123-456@g.us';event=a.whatsapp_event(account,raw);self.assertEqual(event['metadata']['sender_identity'],'unknown');self.assertEqual(event['metadata']['message_id'],'FULL-NATIVE-ID');self.assertEqual(event['title'],'Family')
 def test_outgoing_sender_is_owner_even_when_provider_labels_recipient(self):
  raw=message();raw.update(ChatJID='234@s.whatsapp.net',ChatName='Xavier',SenderName='Xavier',SenderJID='234@s.whatsapp.net',FromMe=True)
  event=a.whatsapp_event(account,raw)
  self.assertEqual(event['title'],'Xavier');self.assertEqual(event['metadata']['from'],'You')
  self.assertEqual(event['metadata']['sender_display'],'You');self.assertIsNone(event['metadata']['sender_id'])
  self.assertEqual(event['metadata']['sender_identity'],'authenticated_account_owner')
  raw['FromMe']=False;event=a.whatsapp_event(account,raw);self.assertEqual(event['metadata']['from'],'Xavier')

 def test_full_window_replay_edit_and_revocation(self):
  src=a.WhatsApp({},account);raw=message()
  with patch.object(src,'profile',return_value={}),patch.object(src,'read',side_effect=lambda _: {'messages':[raw]}):
   a.sync_whatsapp(self.store,{},account,src);a.sync_whatsapp(self.store,{},account,src)
   raw['Text']='Updated';raw['Edited']=True;a.sync_whatsapp(self.store,{},account,src)
   raw['Revoked']=True;a.sync_whatsapp(self.store,{},account,src)
  with self.store.connect() as db:self.assertEqual(tuple(db.execute('SELECT count(*),version,deleted FROM events').fetchone()),(1,3,1))
 def test_saturated_or_invalid_window_cannot_advance_checkpoint(self):
  src=a.WhatsApp({},account)
  with patch.object(src,'profile',return_value={}),patch.object(src,'read',return_value={'messages':[message()]*1000}),self.assertRaises(a.SourceError):a.sync_whatsapp(self.store,{},account,src)
  self.assertIsNone(self.store.checkpoint(account['id']))
  raw=message();raw['Timestamp']='2026-10-09T12:00:00'
  with self.assertRaises(a.SourceError):a.whatsapp_event(account,raw)
  raw=message();raw['MsgID']='imported_synthetic'
  with self.assertRaises(a.SourceError):a.whatsapp_event(account,raw)
if __name__=='__main__':unittest.main()
