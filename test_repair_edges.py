import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import test_preparation as p
import next_actions as n
import rollout_health



class SenderEvidence(unittest.TestCase):
    def test_reload_health_check_is_read_only_and_omits_payload(self):
        import sqlite3
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE cron_jobs(job_id TEXT,state_json TEXT)')
            db.execute('INSERT INTO cron_jobs VALUES(?,?)',('job',json.dumps({'lastRunStatus':'error','lastError':'Tool call blocked because before_tool_call hook failed','consecutiveErrors':3})))
            self.assertEqual(rollout_health.cached_failures(db)[0]['job_id'],'job')

    def test_attachment_staging_is_shared_not_private_tmp(self):
        self.assertTrue(any('/workspace/data/assistant-attachments' in p for p in n.ATTACHMENT_SOURCES))
        self.assertFalse(any(p.startswith('/tmp/') for p in n.ATTACHMENT_SOURCES))


class SnoozeOrdering(unittest.TestCase):
    def test_new_reply_prevents_archive(self):
        g=n.ThreadSnooze({'accounts':[{'id':'work'}]},None,None)
        g.allowed=lambda _:True
        g.thread=lambda *_:{'messages':[{'id':'sent','internalDate':'100','labelIds':['SENT']},
                                       {'id':'reply','internalDate':'101','labelIds':['INBOX','UNREAD']}]}
        with patch.object(g,'write') as write:
            self.assertEqual(g.snooze('work','thread','sent'),'returned');write.assert_not_called()
        with self.assertRaises(RuntimeError):g.snooze('work','thread','wrong')


class Overlap(unittest.TestCase):
    setUp=p.Preparation.setUp
    def test_older_snooze_does_not_wake_newer_snooze(self):
        with self.store.connect() as db:
            for job,until in [('a','2000-01-01T00:00:00+00:00'),('b','2099-01-01T00:00:00+00:00')]:
                db.execute("INSERT INTO thread_snoozes(job_id,account_id,thread_id,until,status,created_at,updated_at) VALUES(?,'work','thread',?,'snoozed',?,?)",(job,until,p.a.now(),p.a.now()))
        class Gmail:
            def wake(*_):raise AssertionError('woke newer snooze early')
        self.app.thread_snooze_step(Gmail())
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT until FROM thread_snoozes WHERE job_id='a'").fetchone()[0],'2099-01-01T00:00:00+00:00')
