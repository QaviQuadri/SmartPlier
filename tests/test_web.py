import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from smartplier import discovery_mail
from smartplier.discovery_sources import SourceBatch, make_posting
from smartplier.dashboard import DashboardServer, FORM_TYPE
from tests.test_scoring_cli import PROFILE, MOCK_RESULT
from smartplier.web_app import WebHandler
from smartplier.web_service import Workspace, prepare

class WebTests(unittest.TestCase):
    def setUp(self):
        settings_patch = patch("smartplier.web_service.gmail_credentials", return_value=("", ""))
        settings_patch.start()
        self.addCleanup(settings_patch.stop)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "web.sqlite3"
        self.profile = Path(folder.name) / "profile.json"
        self.profile.write_text(json.dumps(PROFILE), encoding="utf-8")
        prepare(self.db, self.profile)
        provider = type("Provider", (), {"method":"gemini","score":lambda self,_data:MOCK_RESULT})()
        self.server = DashboardServer(0,self.db,self.profile,provider,handler=WebHandler)
        self.server.workspace = Workspace(self.db,self.profile,provider)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.workspace.close)
        thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.csrf = self.call("/overview")[1]["csrf"]

    def call(self, path, data=None, headers=None):
        client = http.client.HTTPConnection(self.server.host, timeout=7)
        try:
            args = {"method":"GET","url":"/api"+path,"headers":headers or {}}
            if data is not None:
                args.update(method="POST", body=urlencode({"payload":json.dumps(data),"csrf":self.csrf}),
                            headers={"Origin":self.server.origin,"Content-Type":FORM_TYPE,**(headers or {})})
            client.request(**args)
            response=client.getresponse()
            return response.status,json.loads(response.read())
        finally:
            client.close()

    def task(self):
        for _ in range(150):
            tasks=self.server.workspace.snapshot()
            if tasks and tasks[-1]["status"] not in {"queued","running"}:
                return tasks[-1]
            time.sleep(.02)
        self.fail("Worker did not finish.")

    def test_jobs_security_scoring_and_events(self):
        payload={"title":"Python Developer","company":"Example","location":"Remote","description":"Python"}
        self.assertEqual(self.call("/jobs",payload,{"Origin":"https://example.org"})[0],403)
        self.assertEqual(self.call("/jobs",payload,{"Content-Type":"application/json"})[0],415)
        self.assertEqual(self.call("/overview",headers={"Host":"untrusted.example"})[0],403)
        _,job=self.call("/jobs",payload)
        self.assertEqual(self.call("/score",{})[0],202)
        self.assertEqual(self.task()["successes"],1)
        self.assertEqual(self.call("/jobs/"+str(job["id"]))[1]["job"]["score"],83)
        client=http.client.HTTPConnection(self.server.host,timeout=5)
        try:
            client.request("GET","/api/events")
            response=client.getresponse()
            self.assertEqual(response.getheader("Content-Type"),"text/event-stream")
            self.assertIn(b"completed",response.readline())
        finally:
            client.close()
    def test_sync_history_scheduler_and_failure_isolation(self):
        config=self.call("/settings")[1]["settings"]
        config["discovery"]["companies"]=[
            {"provider":"lever","board":"broken","company":"Broken"},
            {"provider":"lever","board":"ok","company":"Working"},
        ]
        config["discovery"]["rss"]=[]
        config["interval"]=240
        self.assertEqual(self.call("/settings",config)[0],200)
        source=SourceBatch("lever:ok","Working",[make_posting("Python Developer","Example","Remote","Python","https://example.com/job","1")])
        from datetime import timedelta
        from smartplier.web_service import now
        self.server.workspace.next_run=now()-timedelta(seconds=1)
        with patch("smartplier.web_service.ats_batch",side_effect=[ValueError("Offline"),source,source]):
            status=self.task()
            self.assertEqual((status["status"],status["completed"]),("partial",2))
            self.assertEqual(self.call("/discover",{"source":"lever:ok"})[0],202)
            self.assertEqual(self.task()["results"][0]["result"]["duplicates"],1)
        rows=self.call("/sync-history")[1]
        self.assertEqual([x["trigger"] for x in rows],["manual","scheduled","scheduled"])
        self.assertEqual(rows[-1]["error"],"Offline")

class GmailTests(unittest.TestCase):
    def test_incremental_uids_are_skipped(self):
        from unittest.mock import MagicMock
        inbox=MagicMock()
        inbox.select.return_value=("OK",[])
        inbox.response.return_value=("UIDVALIDITY",[b"100"])
        inbox.uid.side_effect=[("OK",[b"12"]),("OK",[(b"body",b"Subject: Alert\r\n\r\nText")])]
        options={"mailbox":"INBOX","days":14,"limit":50}
        with patch("smartplier.discovery_mail.imaplib.IMAP4_SSL") as transport:
            transport.return_value.__enter__.return_value=inbox
            first=discovery_mail.gmail_batch(options,"test@example.org","abcdefghijklmnop",set())
            self.assertEqual(len(first.message_ids),1)
            inbox.uid.reset_mock()
            inbox.uid.side_effect=[("OK",[b"12"])]
            second=discovery_mail.gmail_batch(options,"test@example.org","abcdefghijklmnop",set(first.message_ids))
            self.assertEqual(second.checked,1)
            self.assertEqual(second.message_ids,[])
            self.assertEqual(inbox.uid.call_count,1)
