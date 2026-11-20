import contextlib
import hashlib
import io
import json
import mailbox
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
import export_mbox as e
import package_checkpoint as p
from test_download import RAW


class CheckpointTests(unittest.TestCase):
    def test_encryption_roundtrip_wrong_password_and_content_change(self):
        with tempfile.TemporaryDirectory(prefix='mail-checkpoint-test-') as tmp:
            root=Path(tmp);source=root/'migration';source.mkdir()
            (source/'mail.eml').write_bytes(RAW)
            (source/'metadata.json').write_text('{"test":true}')
            (source/'.lock').write_text('')
            home=root/'gpg';home.mkdir(mode=0o700)
            target=root/'fixture.gpg';paths=p.files(source)
            self.assertEqual(len(paths),2)
            try:
                p.encrypt(paths,target,'synthetic-test-password',home)
                p.verify(paths,target,'synthetic-test-password',home)
                with self.assertRaises(Exception):p.verify(paths,target,'wrong-password',home)
                (source/'metadata.json').write_text('{"test":false}')
                with self.assertRaisesRegex(RuntimeError,'size mismatch|content mismatch'):
                    p.verify(paths,target,'synthetic-test-password',home)
            finally:subprocess.run(['gpgconf','--homedir',str(home),'--kill','gpg-agent'],check=False)
    def test_mbox_restore_keeps_attachment(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
            root=Path(tmp)/'source';root.mkdir();(root/'mail.eml').write_bytes(RAW)
            (root/'report.json').write_text(json.dumps({'status':'verified_snapshot','folders':[
                {'id':'f','name':'INBOX.Archives.2006','selectable':True,'message_count':1}]}))
            db=sqlite3.connect(root/'inventory.sqlite3')
            db.execute('CREATE TABLE messages (path TEXT,sha256 TEXT,size INT,active INT,folder TEXT,uid TEXT)')
            db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',('mail.eml',hashlib.sha256(RAW).hexdigest(),len(RAW),1,'f','1'))
            db.commit();db.close()
            out=Path(tmp)/'export';e.export(root,out)
            path=next(out.glob('*.mbox'));box=mailbox.mbox(str(path),create=False)
            try:
                self.assertEqual(len(box),1)
                self.assertEqual(list(box[0].walk())[-1].get_payload(decode=True),b'\x00\x01\x02\xff')
            finally:box.close()
            with self.assertRaises(FileExistsError):e.export(root,out)


if __name__=='__main__':unittest.main()
