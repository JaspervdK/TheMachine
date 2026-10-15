import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('assistant_fairness',Path(__file__).with_name('assistant.py'))
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)


class ReopenAndSlack(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=a.Store(Path(self.temp.name)/'events.sqlite')

    def test_reopen_preserves_draft_and_history_and_edits_one_card_idempotently(self):
        event={'account_id':'work','source_id':'one','kind':'gmail.message','occurred_at':a.now(),
               'title':'Evidence','body':'Evidence','metadata':{},'deleted':False}
        self.store.commit_page('work',{},[event]);eid=self.store.events(['work'])[0]['id']
        sid=self.store.suggest('topic','Prepare reply','A short suggestion',[eid])['id']
        self.store.decide(sid,'accept','','accept','dashboard');self.store.prepare(sid,'The prepared reply')
        with self.store.connect() as db:
            db.execute("UPDATE deliveries SET status='sent',message_id='123' WHERE suggestion_id=?",(sid,))
        self.store.decide(sid,'complete','Actually finished','done','dashboard')
        with self.assertRaises(ValueError):self.store.decide(sid,'reopen','','agent','agent')
        self.assertEqual(self.store.decide(sid,'reopen','','owner','dashboard')['status'],'prepared')
        self.assertTrue(self.store.decide(sid,'reopen','','owner','dashboard')['duplicate'])
        self.assertTrue(self.store.decide(sid,'reopen','','double-click','dashboard')['duplicate'])
        state=self.store.snapshot([])
        self.assertEqual(state['suggestions'][0]['body'],'The prepared reply')
        self.assertEqual(len(state['decisions']),3)
        self.assertEqual(state['deliveries'][0]['message_id'],'123')
        self.assertEqual(state['deliveries'][0]['status'],'sent')
        with patch.object(a,'telegram',return_value={'messageId':'123'}) as telegram:
            a.deliver(self.store,{})
        self.assertEqual(telegram.call_args.args[2],'edit')
        self.assertEqual(telegram.call_count,1)

    def test_legacy_completion_restores_accepted_and_pending_cannot_reopen(self):
        self.store.commit_page('work',{},[{'account_id':'work','source_id':'one','kind':'gmail.message',
            'occurred_at':a.now(),'title':'Real','body':'Real','metadata':{},'deleted':False}])
        sid=self.store.suggest('topic','Reply','Reply',[self.store.events(['work'])[0]['id']])['id']
        with self.assertRaises(ValueError):self.store.decide(sid,'reopen','','bad','dashboard')
        with self.store.connect() as db:db.execute("UPDATE suggestions SET status='completed' WHERE id=?",(sid,))
        self.assertEqual(self.store.decide(sid,'reopen','','owner','dashboard')['status'],'accepted')

    def test_agent_cannot_spoof_dashboard_source_for_reopen(self):
        app=object.__new__(a.App);app.config={'accounts':[]};app.store=self.store
        with self.assertRaisesRegex(ValueError,'owner dashboard'):
            app.action('decide',{'suggestion_id':'id','choice':'reopen','source':'dashboard','decision_id':'agent'})

    def test_incremental_rotates_one_old_thread_and_keeps_new_channel_messages(self):
        account={'id':'slack','fair_incremental':True,'slack_host':'example.slack.com'}
        channels=[{'id':'C1','name':'team','type':'channel','threads':['1001.000001','1002.000001','1003.000001'],
                   'last_scan':'2000.000000','next_full':a.time.time()+86400},
                  {'id':'C2','name':'other','type':'channel','threads':[], 'last_scan':'2000.000000',
                   'next_full':a.time.time()+86400}]
        state={'mode':'incremental','phase':'scan','after':'1000.000000','channels':channels,'index':0,
               'next_discovery':a.time.time()+86400}
        self.store.commit_page('slack',state,[])
        calls=[]
        class Source:
            def profile(self):pass
            def get(self,method,params):
                calls.append((method,params.copy()))
                if method=='conversations.history' and params['channel']=='C2':
                    return {'messages':[{'ts':'3000.000001','text':'A newly arrived message'}]}
                return {'messages':[]}
        source=Source();config={'slack_thread_retention_days':10**6}  # epoch-era fixture timestamps
        a.sync_slack(self.store,config,account,source)
        scan=self.store.checkpoint('slack')['scan'];self.assertEqual(scan['threads'],['1001.000001'])
        a.sync_slack(self.store,config,account,source)
        cp=self.store.checkpoint('slack');self.assertEqual(cp['index'],1)
        self.assertEqual(cp['channels'][0]['threads'],channels[0]['threads'])
        a.sync_slack(self.store,config,account,source)
        self.assertEqual(self.store.events(['slack'])[0]['body'],'A newly arrived message')
        a.sync_slack(self.store,config,account,source)
        self.assertEqual(self.store.checkpoint('slack')['scan']['threads'],['1002.000001'])

    def test_failed_thread_page_is_preserved_without_starving_other_channels(self):
        account={'id':'slack','fair_incremental':True,'slack_host':'example.slack.com'}
        channels=[{'id':'C1','name':'one','type':'channel','last_scan':'2000.000000','threads':['1001.000001']},
                  {'id':'C2','name':'two','type':'channel','last_scan':'2000.000000','threads':[]}]
        scan={'phase':'threads','oldest':'1000.000000','latest':'3000.000000','full':False,
              'threads':['1001.000001'],'known_threads':['1001.000001'],'thread_index':0,'thread_start':0,'cursor':'saved-page'}
        self.store.commit_page('slack',{'mode':'incremental','phase':'scan','after':'1000.000000','index':0,
            'channels':channels,'scan':scan,'next_discovery':a.time.time()+86400},[])
        class Broken:
            def profile(self):pass
            def get(self,*args):raise a.SourceError('network:TimeoutError:conversations.replies')
        with self.assertRaises(a.SourceError):a.sync_slack(self.store,{},account,Broken())
        state=self.store.checkpoint('slack')
        self.assertEqual(state['index'],1)
        self.assertNotIn('scan',state)
        saved=state['channels'][0]['deferred_thread']
        self.assertEqual(saved['cursor'],'saved-page')
        self.assertEqual(saved['latest'],'3000.000000')
        self.assertEqual(saved['ts'],'1001.000001')
        state['index']=0
        saved['retry_at']=0
        self.store.commit_page('slack',state,[],record_sync=False)
        calls=[]
        class Healthy:
            def profile(self):pass
            def get(self,method,params):calls.append((method,params.copy()));return {'messages':[]}
        a.sync_slack(self.store,{},account,Healthy())
        a.sync_slack(self.store,{},account,Healthy())
        self.assertEqual(calls[-1][1]['cursor'],'saved-page')
        self.assertEqual(calls[-1][1]['oldest'],'1000.000000')
        self.assertEqual(calls[-1][1]['latest'],'3000.000000')
        self.assertNotIn('deferred_thread',self.store.checkpoint('slack')['channels'][0])


if __name__=='__main__':unittest.main()
