import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import download as d

RAW = (b'Date: Thu, 12 Oct 2006 19:02:00 -0700\r\nSubject: Historical fixture\r\n'
       b'Message-ID: <historical@example.com>\r\nMIME-Version: 1.0\r\n'
       b'Content-Type: multipart/mixed; boundary=x\r\n\r\n--x\r\n'
       b'Content-Type: text/plain\r\n\r\nFrom original message\r\n--x\r\n'
       b'Content-Type: application/octet-stream\r\nContent-Transfer-Encoding: base64\r\n\r\nAAEC/w==\r\n--x--\r\n')


class FakeIMAP:
    def __init__(self):
        self.selected = None
        self.fetches = 0
        self.seen = True
        self.searches = 0
        self.change = False

    def list(self):
        return 'OK', [b'(\\HasChildren) "." "INBOX"', b'(\\HasNoChildren) "." "INBOX.Trash"',
                      b'(\\Noselect) "." "INBOX.Archives"']

    def select(self, name, readonly=False):
        assert readonly is True
        self.selected = name
        return 'OK', [b'1' if name == b'"INBOX"' else b'0']

    def response(self, name):
        assert name == 'UIDVALIDITY'
        return name, [b'42']

    def uid(self, command, *args):
        if command == 'SEARCH':
            self.searches += 1
            if self.selected != b'"INBOX"':
                return 'OK', [b'']
            return 'OK', [b'7 8' if self.change and self.searches > 2 else b'7']
        assert command == 'FETCH'
        if 'BODY.PEEK' in args[1]:
            self.fetches += 1
            return 'OK', [(f'1 (UID 7 RFC822.SIZE {len(RAW)} BODY[] {{{len(RAW)}}}'.encode(), RAW), b')']
        flags = r'\Seen' if self.seen else ''
        return 'OK', [f'1 (UID 7 FLAGS ({flags}) INTERNALDATE "03-Dec-2012 12:00:00 +0000" RFC822.SIZE {len(RAW)})'.encode()]


class DownloadTests(unittest.TestCase):
    def test_folder_order_and_marked_flags_do_not_invalidate_snapshot(self):
        class ChangingList(FakeIMAP):
            calls = 0
            def list(self):
                self.calls += 1
                status, entries = super().list()
                if self.calls > 1:
                    entries = [entry.replace(b'\\HasChildren', b'\\UnMarked \\HasChildren') for entry in reversed(entries)]
                return status, entries
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            d.download(ChangingList(), root)
            self.assertEqual(json.loads((root / 'report.json').read_text())['status'], 'verified_snapshot')

    def test_real_folder_change_still_stops_snapshot(self):
        class NewFolder(FakeIMAP):
            calls = 0
            def list(self):
                self.calls += 1
                status, entries = super().list()
                if self.calls > 1:
                    entries.append(b'(\\Noselect) "." "NewFolder"')
                return status, entries
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            with self.assertRaisesRegex(RuntimeError, 'Folder list changed'):
                d.download(NewFolder(), root)
            report = json.loads((root / 'report.json').read_text())
            self.assertEqual(report['status'], 'incomplete')
            self.assertEqual(len(report['folder_structure_change']['after']), 4)

    def test_download_resume_corruption_and_dates(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            client = FakeIMAP()
            d.download(client, root)
            r = json.loads((root / 'report.json').read_text())
            self.assertEqual(r['status'], 'verified_snapshot')
            self.assertEqual(len(r['folders']), 3)
            self.assertEqual(r['total_messages'], 1)
            db = sqlite3.connect(root / 'inventory.sqlite3')
            row = db.execute('SELECT path,sent_utc,internaldate,flags FROM messages WHERE active=1').fetchone()
            self.assertEqual((root / row[0]).read_bytes(), RAW)
            self.assertTrue(row[1].startswith('2006-10-13'))
            self.assertIn('2012', row[2])
            audit = json.loads((root / 'date-audit.json').read_text())
            self.assertEqual(audit['sent_year_to_received_year'], {'2006 -> 2012': 1})
            client.seen = False
            d.download(client, root)
            self.assertEqual(client.fetches, 1)
            self.assertEqual(db.execute('SELECT flags FROM messages WHERE active=1').fetchone()[0], '')
            (root / row[0]).write_bytes(b'corruption')
            d.download(client, root)
            self.assertEqual(client.fetches, 2)
            self.assertEqual((root / row[0]).read_bytes(), RAW)
            db.close()

    def test_changing_mailbox_is_not_verified(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            client = FakeIMAP()
            client.change = True
            with self.assertRaisesRegex(RuntimeError, 'New or removed'):
                d.download(client, root)
            self.assertEqual(json.loads((root / 'report.json').read_text())['status'], 'incomplete')

    def test_invalid_dates_and_encoded_names(self):
        self.assertIsNone(d.header_fields(b'Date: nonsense\r\n\r\nbody')[1])
        self.assertIsNone(d.header_fields(b'Date: 12 Oct 2006 19:02:00\r\n\r\nbody')[1])
        self.assertEqual(d.label(b'INBOX.&ZeVnLIqe-'), 'INBOX.日本語')
        self.assertEqual(d.quote(b'A"B\\C'), b'"A\\"B\\\\C"')


if __name__ == '__main__':
    unittest.main()
