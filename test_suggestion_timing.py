import datetime as dt
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('timing_assistant',Path(__file__).with_name('assistant.py'))
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
BXL=a.ZoneInfo('Europe/Amsterdam')
def at(y,m,d,h,mi=0):return dt.datetime(y,m,d,h,mi,tzinfo=BXL)

class Timing(unittest.TestCase):
    def test_weekend_nonurgent_work_waits_for_monday_but_personal_and_urgent_do_not(self):
        monday=at(2026,10,12,8,30).astimezone(dt.timezone.utc).isoformat()
        saturday=at(2026,10,10,16)
        self.assertEqual(a.delivery_time('now',current=saturday,work=True),monday)
        self.assertEqual(a.delivery_time('digest',current=saturday,work=True),monday)
        self.assertEqual(a.delivery_time('2026-10-11T12:00',current=saturday,work=True),monday)
        self.assertIsNone(a.delivery_time('now',current=saturday,work=True,urgent=True))
        self.assertIsNone(a.delivery_time('now',current=saturday,work=False))
        self.assertEqual(a.delivery_time('digest',current=saturday,work=False),at(2026,10,11,8,30).astimezone(dt.timezone.utc).isoformat())
        self.assertEqual(a.delivery_time('2026-10-14',current=saturday,work=True),at(2026,10,14,8,30).astimezone(dt.timezone.utc).isoformat())
        self.assertIsNone(a.delivery_time('now',current=at(2026,10,12,9),work=True))
    def test_delivery_time(self):
        evening=at(2026,10,11,21,0)
        self.assertIsNone(a.delivery_time('now',current=evening))
        self.assertEqual(a.delivery_time('digest',current=evening),at(2026,10,12,8,30).astimezone(dt.timezone.utc).isoformat())
        self.assertEqual(a.delivery_time('digest',current=at(2026,10,12,7,0)),at(2026,10,12,8,30).astimezone(dt.timezone.utc).isoformat())
        self.assertEqual(a.delivery_time('2026-10-14',current=evening),at(2026,10,14,8,30).astimezone(dt.timezone.utc).isoformat())
        self.assertEqual(a.delivery_time('2026-10-14T13:00',current=evening),at(2026,10,14,13,0).astimezone(dt.timezone.utc).isoformat())
        self.assertIsNone(a.delivery_time('2026-10-01',current=evening))  # a past date means now
        with self.assertRaises(ValueError):a.delivery_time('soonish',current=evening)

class HeldCards(unittest.TestCase):
    def test_agent_default_is_valid_and_work_boundary_enforces_weekend(self):
        app=object.__new__(a.App);app.store=self.store
        app.config={'accounts':[{'id':'work','boundary':'work:Acme'}]}
        with patch.object(a,'now',return_value=at(2026,10,10,16).astimezone(dt.timezone.utc).isoformat()):
            default=app.action('suggest',{'dedupe_key':'default','title':'Task','body':'Body','event_ids':[1]})
            immediate=app.action('suggest',{'dedupe_key':'explicit-now','title':'Task','body':'Body','event_ids':[1],'when':'now'})
            urgent=app.action('suggest',{'dedupe_key':'urgent','title':'Task','body':'Body','event_ids':[1],'when':'now','urgent':True})
        monday=at(2026,10,12,8,30).astimezone(dt.timezone.utc).isoformat()
        self.assertEqual(default['deliver_after'],monday);self.assertEqual(immediate['deliver_after'],monday)
        self.assertIsNone(urgent['deliver_after'])
    def test_deadline_within_three_days_goes_out_now(self):
        app=object.__new__(a.App);app.store=self.store
        app.config={'accounts':[{'id':'work','boundary':'work:Acme'}]}
        base={'title':'Task','body':'Body','event_ids':[1]}
        with patch.object(a,'now',return_value=at(2026,10,14,10,26).astimezone(dt.timezone.utc).isoformat()):  # Wednesday
            soon=app.action('suggest',{**base,'dedupe_key':'soon','due':'2026-10-17'})
            later=app.action('suggest',{**base,'dedupe_key':'later','due':'2026-10-18'})
        self.assertIsNone(soon['deliver_after'])
        self.assertEqual(later['deliver_after'],at(2026,10,15,8,30).astimezone(dt.timezone.utc).isoformat())
        with patch.object(a,'now',return_value=at(2026,10,10,16).astimezone(dt.timezone.utc).isoformat()):  # Saturday
            sunday=app.action('suggest',{**base,'dedupe_key':'sunday','due':'2026-10-11'})
            tuesday=app.action('suggest',{**base,'dedupe_key':'tuesday','due':'2026-10-13'})
        self.assertIsNone(sunday['deliver_after'])  # Monday would be too late: urgent
        self.assertEqual(tuesday['deliver_after'],at(2026,10,12,8,30).astimezone(dt.timezone.utc).isoformat())
        with self.store.connect() as db:
            self.assertEqual(dict(db.execute("SELECT dedupe_key,urgent FROM suggestions WHERE dedupe_key IN ('sunday','tuesday')").fetchall()),{'sunday':1,'tuesday':0})
    def test_friday_work_delivery_catchup_waits_but_owner_reminder_does_not(self):
        friday=at(2026,10,9,16);saturday=at(2026,10,10,16)
        with patch.object(a,'now',return_value=friday.astimezone(dt.timezone.utc).isoformat()):
            sid=self.store.suggest('friday','Task','Body',[1],'now')['id']
        config={'accounts':[{'id':'work','boundary':'work:Acme'}]}
        class Weekend(dt.datetime):
            @classmethod
            def now(cls,tz=None):return saturday.astimezone(tz or dt.timezone.utc)
        with patch.object(a.dt,'datetime',Weekend),patch.object(a,'now',return_value=saturday.astimezone(dt.timezone.utc).isoformat()),patch.object(a,'telegram',return_value={'messageId':'41'}) as send:
            a.deliver_one(self.store,config,edit_cards=False);send.assert_not_called()
            with self.store.connect() as db:
                self.assertEqual(db.execute('SELECT deliver_after FROM suggestions').fetchone()[0],at(2026,10,12,8,30).astimezone(dt.timezone.utc).isoformat())
                db.execute('UPDATE deliveries SET reminder=1')
            a.deliver_one(self.store,config,edit_cards=False);send.assert_called_once()
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=a.Store(Path(self.temp.name)/'e.sqlite')
        self.store.commit_page('work',{},[{'account_id':'work','source_id':'m1','kind':'gmail.message','occurred_at':'2026-10-09T10:12:00+00:00',
            'title':'Re: training','body':'Hi','metadata':{'from':'Dana Example <dana@example.com>'},'deleted':False}])

    def deliverable(self,when):
        with self.store.connect() as db:
            return db.execute("SELECT count(*) FROM suggestions s JOIN deliveries d ON s.id=d.suggestion_id WHERE d.status='queued' AND (s.deliver_after IS NULL OR s.deliver_after<=?)",(when,)).fetchone()[0]

    def test_digest_card_waits_and_shows_its_source(self):
        with patch.object(a,'now',return_value=at(2026,10,11,21,0).astimezone(dt.timezone.utc).isoformat()):
            result=self.store.suggest('k','Prepare the Iter brief','Body text.',[1],'digest')
        self.assertEqual(self.deliverable(at(2026,10,11,22,0).astimezone(dt.timezone.utc).isoformat()),0)
        self.assertEqual(self.deliverable(at(2026,10,12,8,31).astimezone(dt.timezone.utc).isoformat()),1)
        with self.store.connect() as db:row=dict(db.execute('SELECT * FROM suggestions WHERE id=?',(result['id'],)).fetchone())
        self.assertEqual(row['source_line'],'Source: email from Dana Example · Fri 9 Oct')
        self.assertIn('Source: email from Dana Example',a.card({**row,'status':'pending'}))

class RecapAndPreferences(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=a.Store(Path(self.temp.name)/'e.sqlite')
        self.store.commit_page('work',{},[{'account_id':'work','source_id':str(i),'kind':'slack.message','occurred_at':a.now(),'title':'t','body':'b',
            'metadata':{'channel_name':'leads','bot_id':'B'},'deleted':False} for i in range(25)])
        self.sid=self.store.suggest('k','Prepare a retention brief','Body.',[1])['id']
        self.store.decide(self.sid,'reject','not useful for me','d1','telegram')
        self.app=object.__new__(a.App);self.app.store=self.store
        self.app.config={'telegram_owner':'1','weekly_recap':{'enabled':True}}

    def test_preferences_include_dismissals_with_notes(self):
        text=self.store.preference_summary()
        self.assertIn('Dismissed:',text);self.assertIn('Prepare a retention brief (owner wrote: "not useful for me")',text)

    def test_behaviour_feeds_reviews_preparations_and_recap(self):
        snoozy=self.store.suggest('s','Review the Q4 budget','Body.',[2])['id']
        quick=self.store.suggest('q','Read the newsletter digest','Body.',[3])['id']
        self.store.decide(snoozy,'snooze','','d2','telegram');self.store.decide(snoozy,'accept','','d3','telegram');self.store.decide(snoozy,'snooze','','d4','dashboard')
        self.store.decide(quick,'reject','','d5','telegram')
        with self.store.connect() as db:
            db.execute("INSERT INTO draft_edits VALUES('e1',?,'aaa','bbb','email','Re: x','Dear Sir, please find attached the long version.','Re: x','Hi Tom, here it is.','dashboard',?)",(self.sid,a.now()))
            db.execute("INSERT INTO action_jobs(id,suggestion_id,revision,payload,status,created_at,updated_at,approval_id,source,rfc_message_id) VALUES('j1',?,'bbb','{}','sent',?,?,'x','dashboard','')",(self.sid,a.now(),a.now()))
        text=self.store.preference_summary()
        self.assertIn('Drafts: 0 sent as written, 1 edited by the owner first.',text)
        self.assertIn('- Review the Q4 budget (2x)',text);self.assertIn('without a note (not worth their time):\n- Read the newsletter digest',text)
        self.assertNotIn('Prepare a retention brief',text.split('How the owner acted')[1])  # a written note is not a quick dismissal
        edits=self.store.draft_edits_text()
        self.assertIn('Your draft: Dear Sir',edits);self.assertIn('The owner changed it to: Hi Tom, here it is.',edits)
        self.assertIn('The owner changed it to: Hi Tom',a.preparation_prompt({'job_id':'j'*16,'suggestion_id':self.sid},'',None,edits))
        recap=a.weekly_recap(self.store,{},None,None)
        self.assertIn('**What I noticed:** 0 drafts sent as written, 1 edited by you first; snoozed more than once: Review the Q4 budget; 1 dismissed within minutes.',recap)

    def test_recap_content(self):
        text=a.weekly_recap(self.store,{},None,{'input':1e6,'cacheRead':9e6,'output':4e4,'runs':3,'usd':3.3})
        self.assertIn('1 new',text);self.assertIn('dismissed 1',text);self.assertIn('$3.30',text)
        self.assertIn('#leads (work, 24 this week)',text)

    def test_recap_goes_out_once_on_friday_afternoon(self):
        sent=[]
        sender=lambda m:(sent.append(m),{'messageId':'7'})[1]
        thursday=at(2026,10,15,17,0).astimezone(dt.timezone.utc);friday=at(2026,10,16,16,5).astimezone(dt.timezone.utc)
        with patch.object(a,'assistant_token_usage',return_value=None):
            self.assertIsNone(self.app.recap_step(sender,thursday))
            self.assertIsNone(self.app.recap_step(sender,at(2026,10,16,15,0).astimezone(dt.timezone.utc)))
            self.assertEqual(self.app.recap_step(sender,friday),'sent')
            self.assertIsNone(self.app.recap_step(sender,friday+dt.timedelta(hours=1)))
        self.assertEqual(len(sent),1)

    def test_unknown_outcome_is_not_resent(self):
        def sender(m):raise ValueError('unclear')
        friday=at(2026,10,16,16,5).astimezone(dt.timezone.utc)
        with patch.object(a,'assistant_token_usage',return_value=None):
            self.assertEqual(self.app.recap_step(sender,friday),'uncertain')
            self.assertIsNone(self.app.recap_step(lambda m:{'messageId':'8'},friday+dt.timedelta(minutes=5)))

if __name__=='__main__':unittest.main()
