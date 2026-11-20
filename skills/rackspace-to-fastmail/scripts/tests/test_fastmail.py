from copy import deepcopy
from datetime import datetime,timezone
import contextlib
import io
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import fastmail_test as t

RAW=b'Date: Thu, 12 Oct 2006 19:02:40 -0700\r\nSubject: Historical fixture\r\n\r\nOriginal bytes\r\n'


class FakeFastmail:
    def __init__(self):
        self.exists=False
        self.messages=[]
        self.appends=0
        self.interrupt=False
        self.wrong_date=False
    def select(self,*args,readonly=False):
        assert readonly
        return ('OK',[b'1']) if self.exists else ('NO',[b'missing'])
    def create(self,*args):
        self.exists=True
        return 'OK',[]
    def response(self,*args):
        return 'UIDVALIDITY',[b'42']
    def subscribe(self,*args):
        return 'OK',[]
    def append(self,folder,flags,date,raw):
        self.appends+=1
        self.messages.append((flags,date,raw))
        if self.interrupt:
            self.interrupt=False
            raise OSError('connection lost after accepted APPEND')
        return 'OK',[b'[APPENDUID 42 1]']
    def uid(self,command,*args):
        if command=='SEARCH':
            return 'OK',[b' '.join(str(i+1).encode() for i in range(len(self.messages)))]
        uid=int(args[0]);flags,date,raw=self.messages[uid-1]
        if self.wrong_date:
            date='"03-Dec-2012 14:18:44 -0500"'
        meta=f'{uid} (UID {uid} FLAGS {flags} INTERNALDATE {date} RFC822.SIZE {len(raw)} BODY[] {{{len(raw)}}}'.encode()
        return 'OK',[(meta,raw),b')']


class FastmailTests(unittest.TestCase):
    def state(self):
        return {'folder':'Migration-Date-Test-fixture','created':False,'messages':[
            {'sha256':t.d.sha(RAW),'size':len(RAW),'uid':'234','source_folder_name':'INBOX.Archives.2006',
             'target_flags':['\\Seen','\\Flagged'],'target_internaldate':'13-Oct-2006 02:02:40 +0000'}]}
    @patch.dict(t.d.cfg.CONFIG, {"repair_received_days": ["2012-12-03"]})
    def test_date_policy(self):
        row={'internaldate':'03-Dec-2012 14:18:44 -0500','sent_utc':'2006-10-13T02:02:40+00:00'}
        self.assertEqual(t.planned_date(row)[0],'13-Oct-2006 02:02:40 +0000')
        row['sent_utc']=None
        self.assertEqual(t.planned_date(row)[0],row['internaldate'])
        row.update(internaldate='05-May-2020 10:00:00 +0000',sent_utc='2020-05-05T09:59:00+00:00')
        self.assertEqual(t.planned_date(row)[0],row['internaldate'])
    def test_interrupted_append_reconciles_without_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(t,'STATE',Path(tmp)/'state.json'),contextlib.redirect_stdout(io.StringIO()):
            server=FakeFastmail();server.interrupt=True;state=self.state();bodies={t.d.sha(RAW):RAW}
            with self.assertRaises(OSError):
                t.run(server,state,bodies)
            t.run(server,state,bodies)
            self.assertEqual(server.appends,1)
            self.assertEqual(state['status'],'verified_imap_awaiting_visual_check')
            self.assertEqual(state['messages'][0]['destination']['sha256'],t.d.sha(RAW))
    def test_wrong_date_or_changed_content_stops(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(t,'STATE',Path(tmp)/'state.json'),contextlib.redirect_stdout(io.StringIO()):
            server=FakeFastmail();server.wrong_date=True;state=self.state()
            with self.assertRaisesRegex(RuntimeError,'requested date'):
                t.run(server,state,{t.d.sha(RAW):RAW})
            self.assertEqual(state['status'],'test_incomplete')
            server.wrong_date=False
            flags,date,raw=server.messages[0]
            server.messages[0]=(flags,date,raw+b'changed')
            with self.assertRaisesRegex(RuntimeError,'changed message bytes'):
                t.run(server,state,{t.d.sha(RAW):RAW})
            self.assertEqual(server.appends,1)


if __name__=='__main__':
    unittest.main()
