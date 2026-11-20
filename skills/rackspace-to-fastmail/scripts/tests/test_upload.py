import contextlib
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import upload_fastmail as u

RAW=b'Date: Thu, 12 Oct 2006 19:02:40 -0700\r\nSubject: sample\r\n\r\nHistorical content\r\n'


class UploadServer:
    def __init__(self):
        self.messages={}
        self.last_uid=None
        self.appends=0
        self.interrupt=False
        self.collapse=False
        self.fetch_bodies=0
    def select(self,*args,readonly=False):
        assert readonly
        return 'OK',[str(len(self.messages)).encode()]
    def response(self,name):
        if name=='UIDVALIDITY':return name,[b'42']
        uid=self.last_uid;self.last_uid=None
        return name,[f'42 {uid}'.encode()] if uid else [None]
    def append(self,folder,flags,date,raw):
        self.appends+=1
        uid=str(len(self.messages)+1)
        if self.collapse and self.messages:uid='1'
        self.messages[uid]=(flags[1:-1],date[1:-1],raw)
        self.last_uid=uid
        if self.interrupt:
            self.interrupt=False
            raise OSError('APPEND accepted; connection lost')
        return 'OK',[f'[APPENDUID 42 {uid}] completed'.encode()]
    def uid(self,command,*args):
        if command=='SEARCH':return 'OK',[' '.join(self.messages).encode()]
        assert command=='FETCH'
        output=[]
        for uid in str(args[0]).split(','):
            flags,date,raw=self.messages[uid]
            meta=f'{uid} (UID {uid} FLAGS ({flags}) INTERNALDATE "{date}" RFC822.SIZE {len(raw)}'.encode()
            if 'BODY.PEEK' in args[1]:
                self.fetch_bodies+=1
                output.extend([(meta+b' BODY[] {0}',raw),b')'])
            else:output.append(meta+b')')
        return 'OK',output


def cache():
    c=sqlite3.connect(':memory:')
    c.execute('CREATE TABLE verified (folder TEXT,validity TEXT,uid TEXT,sha256 TEXT,PRIMARY KEY(folder,validity,uid))')
    return c


class UploadTests(unittest.TestCase):
    def rows(self,root,copies=1):
        result=[]
        for n in range(copies):
            path=f'{n+1}.eml';(root/path).write_bytes(RAW)
            result.append({'folder':'source-folder','validity':'10','uid':str(n+1),'path':path,'sha256':u.d.sha(RAW),
                           'size':len(RAW),'target_date':'13-Oct-2006 02:02:40 +0000','target_flags':['\\Seen','NonJunk']})
        return result
    def test_exact_copies_retained_and_resume_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
            root=Path(tmp);rows=self.rows(root,3);client=UploadServer();db=cache()
            with patch.object(u.d,'ROOT',root):
                report=u.upload_folder(client,'Archives/2006',rows,db)
                self.assertEqual(report['message_count'],3)
                self.assertEqual(len({r['destination_uid'] for r in report['messages']}),3)
                self.assertEqual(client.appends,3)
                self.assertEqual(client.fetch_bodies,3)
                u.upload_folder(client,'Archives/2006',rows,db)
                self.assertEqual(client.appends,3)
                self.assertEqual(client.fetch_bodies,3)
            db.close()
    def test_lost_append_response_reconciled(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
            root=Path(tmp);rows=self.rows(root);client=UploadServer();client.interrupt=True;db=cache()
            with patch.object(u.d,'ROOT',root):
                with self.assertRaises(OSError):u.upload_folder(client,'INBOX',rows,db)
                report=u.upload_folder(client,'INBOX',rows,db)
                self.assertEqual(report['message_count'],1)
                self.assertEqual(client.appends,1)
            db.close()
    def test_server_collapse_is_not_reported_as_complete(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
            root=Path(tmp);rows=self.rows(root,2);client=UploadServer();client.collapse=True;db=cache()
            with patch.object(u.d,'ROOT',root),self.assertRaisesRegex(RuntimeError,'collapsed'):
                u.upload_folder(client,'INBOX',rows,db)
            db.close()
    def test_wrong_date_and_changed_bytes_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            row=self.rows(Path(tmp))[0]
            bad={'uid':'1','sha256':row['sha256'],'size':row['size'],'internaldate':'03-Dec-2012 14:18:44 -0500','flags':'\\Seen'}
            with self.assertRaisesRegex(RuntimeError,'date'):u.check_record(bad,row)
            bad.update(internaldate=row['target_date'],sha256='wrong')
            with self.assertRaisesRegex(RuntimeError,'bytes'):u.check_record(bad,row)
    def test_folder_mapping(self):
        source=[{'id':'1','name':'INBOX','delimiter':'.'}, {'id':'2','name':'INBOX.Archives.Sent.2006','delimiter':'.'},
                {'id':'3','name':'INBOX.Sent','delimiter':'.'},{'id':'4','name':'INBOX.Junk','delimiter':'.'}]
        dest=[{'name':'Sent Items','flags':'\\Sent'},{'name':'Spam','flags':'\\Junk'}]
        self.assertEqual(u.mappings(source,dest),{'1':'INBOX','2':'Archives/Sent/2006','3':'Sent Items','4':'Spam'})
        self.assertEqual(u.encode_name('Archives/日本語 & mail'),b'Archives/&ZeVnLIqe- &- mail')


if __name__=='__main__':unittest.main()
