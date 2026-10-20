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


# Sending: only an owner-approved card sends, once, through the one writable wacli call. No live sends: wacli is always faked.
import json,subprocess
import next_actions as n
import test_ux as ux
WA={**account,'service':'whatsapp','transport':'wacli-local','store':'/tmp/wa-store','boundary':'personal:whatsapp'}
CHAT='32470000000@s.whatsapp.net'
def ok(data):return subprocess.CompletedProcess([],0,json.dumps({'success':True,'data':data,'error':None}),'')
class WhatsAppSend(unittest.TestCase):
 row=ux.UX.row;decide=ux.UX.decide
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.store=a.Store(Path(self.tmp.name)/'events.sqlite')
  raw=message();raw.update(ChatJID=CHAT,ChatName='Sam',SenderJID=CHAT,Text='Are we still on for Friday?')
  self.store.commit_page(WA['id'],{},[a.whatsapp_event(WA,raw)])
  with self.store.connect() as db:ids=[r[0] for r in db.execute('SELECT id FROM events')]
  self.sid=self.store.suggest('wa-task','Answer Sam','Sam asks about Friday.',ids)['id']
  self.accounts=[WA,{**WA,'id':'other-whatsapp'},{'id':'work','email':'owner@example.com','service':'gmail','transport':'gog'}]
 def app(self):
  app=object.__new__(a.App);app.store=self.store;app.config={'accounts':self.accounts};return app
 def save(self,**draft):
  action={k:v for k,v in {'kind':'whatsapp','label':'Send reply','account_id':WA['id'],'chat_id':CHAT,'body':'Yes, Friday works.',**draft}.items() if v is not None}
  return self.app().action('prepare',{'suggestion_id':self.sid,'body':'Reply ready.','next_action':action})
 def ready(self,**draft):
  self.decide('accept');self.save(**draft);return self.row()['next_action']
 def confirm(self,key='owner-approval'):
  return self.app().action('confirm_action',{'suggestion_id':self.sid,'revision':self.row()['next_action']['revision'],'approval_id':key},owner_dashboard=True)
 def job(self):
  with self.store.connect() as db:return dict(db.execute('SELECT * FROM action_jobs ORDER BY created_at DESC LIMIT 1').fetchone())
 def transport(self,send=None,preflight=None,reconcile=None):
  class T:
   def preflight(s,action):return preflight(action) if preflight else None
   def send(s,action,job):return send(action,job) if send else {'message_id':'whatsapp:3EB0AA'}
   def reconcile(s,action,job):return reconcile(action,job) if reconcile else {}
  return T()
 def test_reply_only_in_a_task_chat_plain_text(self):
  action=self.ready()
  self.assertEqual((action['chat_id'],action['to_name'],action['max_length']),(CHAT,'Sam',4000))
  self.assertEqual(n.preview(action),'WhatsApp message to Sam\n\nYes, Friday works.')
  self.assertEqual(a.controls(self.row())['blocks'][0]['buttons'][0]['label'],'Send reply')
  for bad in ({'chat_id':'999@s.whatsapp.net'},{'account_id':'other-whatsapp'},{'account_id':'work'},{'label':'Post it'},
              {'body':'a'*4001},{'body':' '},{'media':['a.png']},{'to':'+3247000'}):
   with self.assertRaises(ValueError,msg=bad):self.save(**bad)
  with self.assertRaises(ValueError):n.revise(action,body='a'*4001)
  self.assertEqual(n.revise(action,body='Friday!')['body'],'Friday!')
 def test_send_now_never_sends_whatsapp(self):
  self.decide('accept')
  with self.assertRaises(ValueError):
   self.app().action('prepare',{'suggestion_id':self.sid,'body':'Reply ready.','send_now':True,
                                'next_action':{'kind':'whatsapp','account_id':WA['id'],'chat_id':CHAT,'body':'Yes'}})
  with self.store.connect() as db:self.assertIsNone(db.execute('SELECT 1 FROM action_jobs').fetchone())
 def test_approved_reply_sends_once_and_the_answer_returns_to_the_card(self):
  self.ready();self.confirm();self.confirm('second-click');calls=[]
  self.app().action_step(self.transport(send=lambda action,job:(calls.append(action),{'message_id':'whatsapp:3EB0AA'})[1]))
  self.app().action_step(self.transport(send=lambda *_:self.fail('Duplicate send')))
  self.assertEqual([c['body'] for c in calls],['Yes, Friday works.'])
  self.assertEqual(self.job()['status'],'sent');self.assertIn('✓ WhatsApp message sent to Sam at ',str(a.card(self.row())))
  raw=message();raw.update(ChatJID=CHAT,ChatName='Sam',SenderJID=CHAT,MsgID='ANSWER',Text='Great!',Timestamp='2099-01-01T00:00:00Z')
  self.store.commit_page(WA['id'],{},[a.whatsapp_event(WA,raw)])
  with self.store.connect() as db:rows=[dict(r) for r in db.execute("SELECT * FROM events WHERE kind='whatsapp.message' ORDER BY occurred_at DESC LIMIT 1")]
  self.assertEqual(list(self.store.card_replies(rows).values())[0]['suggestion_id'],self.sid)
 def test_unknown_outcome_never_resends_and_signed_out_blocks(self):
  self.ready();self.confirm()
  self.app().action_step(self.transport(send=lambda *_:{}))
  self.assertEqual(self.job()['status'],'uncertain')
  with self.store.connect() as db:db.execute('UPDATE action_jobs SET retry_at=NULL')
  self.app().action_step(self.transport(send=lambda *_:self.fail('resent')))
  self.assertEqual(self.job()['error'],'I couldn’t confirm whether this WhatsApp message was sent. I’m checking the chat; I won’t send it again.')
  with self.store.connect() as db:db.execute('UPDATE action_jobs SET retry_at=NULL')
  self.app().action_step(self.transport(reconcile=lambda *_:{'message_id':'whatsapp:3EB0AA'}))
  self.assertEqual(self.job()['status'],'sent')
 def test_signed_out_blocks_cleanly(self):
  self.ready();self.confirm()
  def out(_):raise ValueError('WhatsApp is signed out, or linked to another number. Nothing was sent.')
  self.assertEqual(self.app().action_step(self.transport(preflight=out,send=lambda *_:self.fail('sent'))),'blocked')

class WhatsAppTransportTest(unittest.TestCase):
 config={'wacli':'/opt/wacli','accounts':[WA]}
 action={'kind':'whatsapp','account_id':WA['id'],'chat_id':CHAT,'to_name':'Sam','body':'Yes, Friday works.'}
 def test_checks_read_only_then_one_writable_send(self):
  seen=[]
  def run(args,**kw):
   seen.append((args,kw['env'].get('WACLI_READONLY')))
   if 'auth' in args:return ok({'authenticated':True,'linked_jid':'123@s.whatsapp.net'})
   if 'messages' in args:return ok({'messages':[{'MsgID':'1'}]})
   return ok({'sent':True,'to':CHAT,'id':'3EB0AA'})
  t=n.WhatsAppTransport(self.config)
  with patch.dict(a.os.environ,{'WACLI_READONLY':'1'}),patch.object(n.subprocess,'run',side_effect=run):
   t.preflight(self.action);self.assertEqual(t.send(self.action,{'id':'J1'}),{'message_id':'whatsapp:3EB0AA'})
  reads,(send,send_env)=seen[:-1],seen[-1]
  self.assertTrue(all('--read-only' in args and env=='1' for args,env in reads))
  self.assertEqual(send[:3],['/opt/wacli','--store','/tmp/wa-store']);self.assertNotIn('--read-only',send);self.assertIsNone(send_env)
  self.assertEqual(send[send.index('send'):],['send','text','--to',CHAT,'--message','Yes, Friday works.'])
 def test_preflight_refuses_another_number_or_unknown_chat(self):
  t=n.WhatsAppTransport(self.config)
  for status,rows in (({'authenticated':True,'linked_jid':'999@s.whatsapp.net'},[{}]),({'authenticated':False},[{}]),({'authenticated':True,'linked_jid':'123@s.whatsapp.net'},[])):
   with patch.object(n.subprocess,'run',side_effect=lambda args,**kw:ok(status) if 'auth' in args else ok({'messages':rows})),self.assertRaisesRegex(ValueError,'Nothing was sent'):
    t.preflight(self.action)
 def test_send_outcomes_never_resend_on_doubt(self):
  t=n.WhatsAppTransport(self.config)
  for result,expected in ((subprocess.CompletedProcess([],1,'','no reply from the running sync process before the timeout; the send may still have gone through'),{}),
                          (subprocess.CompletedProcess([],1,'','store is locked by another wacli process'),{'not_sent':True}),
                          (subprocess.CompletedProcess([],1,'','not authenticated; run wacli auth'),{'rejected':'refused'}),
                          (subprocess.CompletedProcess([],1,'','connection reset'),{}),
                          (subprocess.CompletedProcess([],0,'garbage',''),{})):
   with patch.object(n.subprocess,'run',return_value=result):self.assertEqual(t.send(self.action,{'id':'J'}),expected,result.stderr)
  with patch.object(n.subprocess,'run',side_effect=subprocess.TimeoutExpired('wacli',150)):self.assertEqual(t.send(self.action,{'id':'J'}),{})
 def test_reconcile_finds_exactly_one_matching_own_message(self):
  t=n.WhatsAppTransport(self.config);job={'id':'J','created_at':'2026-10-19T10:00:00+00:00'}
  self.action['proof']={'whatsapp_dispatched_at':'2026-10-19T10:00:00+00:00','whatsapp_before_ids':['OLD']}
  mine={'MsgID':'3EB0AA','FromMe':True,'Text':'Yes, Friday works.','Timestamp':'2026-10-19T10:00:02Z'}
  for rows,expected in (([mine],{'message_id':'whatsapp:3EB0AA'}),([mine,{**mine,'MsgID':'B'}],{}),([{**mine,'Text':'Other'}],{}),([{**mine,'FromMe':False}],{})):
   with patch.object(n.subprocess,'run',return_value=ok({'messages':rows})) as run:self.assertEqual(t.reconcile(self.action,job),expected)
   self.assertIn('--read-only',run.call_args[0][0]);self.assertIn('--from-me',run.call_args[0][0])
if __name__=='__main__':unittest.main()
