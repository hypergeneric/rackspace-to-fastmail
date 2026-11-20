import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import catch_up as c
from test_upload import UploadServer,cache,RAW


class CatchupServer(UploadServer):
    def __init__(self):
        super().__init__();self.next_uid=1
    def select(self,*args,readonly=False):
        return 'OK',[str(len(self.messages)).encode()]
    def subscribe(self,*args):return 'OK',[]
    def append(self,folder,flags,date,raw):
        self.appends+=1
        uid=str(self.next_uid);self.next_uid+=1
        self.messages[uid]=(flags[1:-1],date[1:-1],raw)
        self.last_uid=uid
        if self.interrupt:
            self.interrupt=False
            raise OSError('Accepted APPEND; response lost')
        return 'OK',[f'[APPENDUID 42 {uid}] completed'.encode()]
    def uid(self,command,*args):
        if command=='STORE':
            for uid in str(args[0]).split(','):
                flags,date,raw=self.messages[uid]
                values=set(flags.split());change=set(args[2][1:-1].split())
                values=values|change if args[1].startswith('+') else values-change
                self.messages[uid]=(' '.join(sorted(values)),date,raw)
            return 'OK',[]
        if command=='EXPUNGE':
            for uid in str(args[0]).split(','):
                self.messages.pop(uid,None)
            return 'OK',[]
        return super().uid(command,*args)


def row(root,n,raw=RAW,flags=None):
    path=f'{n}.eml';(root/path).write_bytes(raw)
    return {'folder':'f','validity':'10','uid':str(n),'path':path,'sha256':c.d.sha(raw),'size':len(raw),
            'target_date':'13-Oct-2006 02:02:40 +0000','target_flags':['\\Seen'] if flags is None else flags}


class CatchupTests(unittest.TestCase):
    def test_sync_saves_updated_mapping_for_both_modes(self):
        for late in (False,True):
            with self.subTest(late=late),tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
                root=Path(tmp);client=CatchupServer();db=cache()
                original=row(root,1);new=row(root,2,b'Subject: later\r\n\r\nLater message\r\n')
                with patch.object(c.d,'ROOT',root),patch.object(c,'STATE',root/'catch-up.json'),patch.object(c.u,'STATE',root/'upload.json'):
                    prior=c.u.upload_folder(client,'INBOX',[original],db)
                    state={'baseline':{'folders':{'INBOX':prior}},'mode':'late-arrivals' if late else 'pre-cutover',
                           'old_dates':{original['sha256']:original['target_date']},'old_flags':{original['sha256']:original['target_flags']}}
                    report={'folders':[{'id':'f','name':'INBOX','delimiter':'.'}],'total_messages':2,'finished_utc':'test-time'}
                    with patch.object(c,'personal_folders',return_value=[{'name':'INBOX','flags':'','selectable':True}]):
                        c.sync(client,state,report,{'f':[original,new]},db)
                    import json
                    saved=json.loads((root/'upload.json').read_text())
                    self.assertEqual(saved['total_messages'],2)
                    self.assertEqual(len(saved['folders']['INBOX']['messages']),2)
                    self.assertEqual(state['phase'],'complete')
                    self.assertEqual(state['new_source_messages'],1)
                db.close()

    def test_source_delta_and_fastmail_only_preserved(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
            root=Path(tmp);client=CatchupServer();db=cache()
            rows=[row(root,1),row(root,2)]
            with patch.object(c.d,'ROOT',root):
                prior=c.u.upload_folder(client,'INBOX',rows,db)
                extra=b'Subject: Fastmail only\r\n\r\nKeep me\r\n'
                client.append(b'INBOX','(\\Seen)','"13-Oct-2006 02:02:40 +0000"',extra)
                changed=[row(root,1,flags=[]),row(root,3,b'Subject: new\r\n\r\nNew Rackspace arrival\r\n')]
                report=c.reconcile_folder(client,'INBOX',changed,prior,{c.d.sha(RAW):['\\Seen']},db,False)
                self.assertEqual(report['message_count'],2)
                self.assertEqual(report['removed_stale_managed_copies'],1)
                self.assertEqual(client.messages['3'][2],extra)
                self.assertNotIn('2',client.messages)
                self.assertNotIn('\\Seen',client.messages['1'][0])
                self.assertEqual(len(client.messages),3)
            db.close()
    def test_late_arrivals_preserve_existing_flags_and_copies(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
            root=Path(tmp);client=CatchupServer();db=cache()
            original=row(root,1)
            with patch.object(c.d,'ROOT',root):
                prior=c.u.upload_folder(client,'INBOX',[original],db)
                flags,date,raw=client.messages['1'];client.messages['1']=('\\Flagged',date,raw)
                new=row(root,2,b'Subject: late arrival\r\n\r\nNew\r\n')
                result=c.reconcile_folder(client,'INBOX',[new],prior,{},db,True)
                self.assertEqual(client.messages['1'][0],'\\Flagged')
                self.assertEqual(result['removed_stale_managed_copies'],0)
                self.assertEqual(result['message_count'],1)
                self.assertEqual(len(client.messages),2)
            db.close()
    def test_resume_uncertain_append(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
            root=Path(tmp);client=CatchupServer();db=cache();new=row(root,1)
            with patch.object(c.d,'ROOT',root):
                client.interrupt=True
                with self.assertRaises(OSError):c.reconcile_folder(client,'INBOX',[new],{}, {},db,False)
                result=c.reconcile_folder(client,'INBOX',[new],{}, {},db,False)
                self.assertEqual(client.appends,1)
                self.assertEqual(result['message_count'],1)
            db.close()
    def test_changed_mapped_hash_refused(self):
        records=[{'uid':'1','sha256':'changed'}]
        prior={'messages':[{'destination_uid':'1','sha256':'original'}]}
        with self.assertRaisesRegex(RuntimeError,'content changed'):
            c.classify(records,[],prior,False)


if __name__=='__main__':unittest.main()
