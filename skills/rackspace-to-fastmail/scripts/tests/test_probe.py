import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import fastmail_date_probe as p
from test_fastmail import FakeFastmail


class ProbeServer(FakeFastmail):
    def __init__(self,reuse):
        super().__init__()
        self.folders={}
        self.reuse=reuse
        self.first_date=None
        self.capabilities=('IMAP4REV1','UIDPLUS')
        self.forget_deleted=False
    def select(self,folder,readonly=False):
        self.messages=self.folders.get(folder,[])
        return ('OK',[]) if folder in self.folders else ('NO',[])
    def create(self,folder):
        self.folders[folder]=[]
        return 'OK',[]
    def uid(self,command,*args):
        if command=='STORE':
            return 'OK',[]
        if command=='EXPUNGE':
            assert str(args[0])=='1'
            self.messages.clear()
            if self.forget_deleted and not any(self.folders.values()):
                self.first_date=None
            return 'OK',[]
        return super().uid(command,*args)
    def append(self,folder,flags,date,raw):
        self.appends+=1
        if self.first_date is None:
            self.first_date=date
        self.folders[folder].append((flags,self.first_date if self.reuse else date,raw))
        return 'OK',[]


class ProbeTests(unittest.TestCase):
    def test_post_delete_probe_and_resume(self):
        for forget in (True,False):
            with self.subTest(forget=forget),tempfile.TemporaryDirectory() as tmp,patch.object(p,'STATE',Path(tmp)/'probe.json'),contextlib.redirect_stdout(io.StringIO()):
                state={'token':'test-token','probes':[
                    {'label':'A','folder':'Migration-Probe-test-token-A','requested_internaldate':'13-Oct-2007 02:02:40 +0000'},
                    {'label':'B','folder':'Migration-Probe-test-token-B','requested_internaldate':'13-Oct-2008 02:02:40 +0000'}]}
                client=ProbeServer(True);client.forget_deleted=forget
                p.run(client,state)
                p.after_delete(client,state)
                self.assertEqual(state['after_delete']['requested_date_retained'],forget)
                p.after_delete(client,state)
                self.assertEqual(client.appends,3)
                self.assertEqual(sum(map(len,client.folders.values())),1)

    def test_capability_formats_and_missing_support(self):
        for capabilities in (('IMAP4REV1','UIDPLUS'),(b'IMAP4rev1',b'UIDPLUS'),('IMAP4REV1',)):
            with self.subTest(capabilities=capabilities),tempfile.TemporaryDirectory() as tmp,patch.object(p,'STATE',Path(tmp)/'probe.json'),contextlib.redirect_stdout(io.StringIO()):
                state={'token':'test-token','probes':[
                    {'label':'A','folder':'Migration-Probe-test-token-A','requested_internaldate':'13-Oct-2007 02:02:40 +0000'},
                    {'label':'B','folder':'Migration-Probe-test-token-B','requested_internaldate':'13-Oct-2008 02:02:40 +0000'}]}
                client=ProbeServer(True)
                client.capabilities=capabilities
                p.run(client,state)
                if len(capabilities)==1:
                    with self.assertRaisesRegex(RuntimeError,'UIDPLUS'):
                        p.after_delete(client,state)
                    self.assertEqual(sum(map(len,client.folders.values())),2)
                else:
                    p.after_delete(client,state)
                    self.assertEqual(sum(map(len,client.folders.values())),1)

    def test_diagnosis_and_resume(self):
        for reuse in (True,False):
            with self.subTest(reuse=reuse),tempfile.TemporaryDirectory() as tmp,patch.object(p,'STATE',Path(tmp)/'probe.json'),contextlib.redirect_stdout(io.StringIO()):
                state={'token':'test-token','probes':[
                    {'label':'A','folder':'Probe-A','requested_internaldate':'13-Oct-2007 02:02:40 +0000'},
                    {'label':'B','folder':'Probe-B','requested_internaldate':'13-Oct-2008 02:02:40 +0000'}]}
                client=ProbeServer(reuse)
                p.run(client,state)
                self.assertEqual(state['probes'][1]['requested_date_retained'],not reuse)
                self.assertEqual('reused the first date' in state['finding'],reuse)
                p.run(client,state)
                self.assertEqual(client.appends,2)


if __name__=='__main__':
    unittest.main()
