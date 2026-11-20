import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import reset_fastmail as r


class ResetServer:
    capabilities=('IMAP4REV1','UIDPLUS','NAMESPACE')
    def __init__(self):
        self.folders={b'"INBOX"':{'1','2'},b'"Archive"':{'3'}}
        self.selected=None
        self.stored=[]
        self.expunged=[]
        self.interrupt=False
        self.shared=False
    def namespace(self):
        return 'OK',[b'(("" ".")) (("Other Users." ".")) (("Shared." "."))' if self.shared else b'(("" ".")) NIL NIL']
    def list(self):
        result=[b'() "." "INBOX"',b'() "." "Archive"']
        if self.shared:
            result += [b'() "." "Shared"',b'() "." "Shared.Team"',b'() "." "Other Users.Someone"']
        return 'OK',result
    def select(self,name,readonly=False):
        self.selected=name
        return 'OK',[str(len(self.folders[name])).encode()]
    def response(self,name):
        return name,[b'100']
    def uid(self,command,*args):
        if command=='SEARCH':
            return 'OK',[' '.join(sorted(self.folders[self.selected])).encode()]
        if command=='STORE':
            assert args[1:]==('+FLAGS.SILENT','(\\Deleted)')
            self.stored.append((self.selected,args[0]))
            return 'OK',[]
        if command=='EXPUNGE':
            for uid in args[0].split(','):
                self.folders[self.selected].discard(uid)
            self.expunged.append((self.selected,args[0]))
            if self.interrupt:
                self.interrupt=False
                raise OSError('Lost response after expunge')
            return 'OK',[]
        raise AssertionError(command)


class ResetTests(unittest.TestCase):
    def test_clear_resume_and_never_clear_during_test(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(r,'STATE',Path(tmp)/'reset.json'),contextlib.redirect_stdout(io.StringIO()):
            client=ResetServer();client.interrupt=True
            state={'phase':'clearing'}
            with self.assertRaises(OSError):
                r.clear_personal_mail(client,state)
            r.clear_personal_mail(client,state)
            self.assertEqual(state['phase'],'mail_cleared')
            self.assertTrue(all(not v for v in client.folders.values()))
            client.folders[b'"INBOX"'].add('4')
            state['phase']='testing'
            r.clear_personal_mail(client,state)
            self.assertEqual(client.folders[b'"INBOX"'],{'4'})
    def test_shared_namespaces_are_not_touched(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(r,'STATE',Path(tmp)/'reset.json'),contextlib.redirect_stdout(io.StringIO()):
            client=ResetServer();client.shared=True
            client.folders.update({b'"Shared.Team"':{'9'},b'"Other Users.Someone"':{'10'}})
            r.clear_personal_mail(client,{'phase':'clearing'})
            self.assertEqual(client.folders[b'"Shared.Team"'],{'9'})
            self.assertEqual(client.folders[b'"Other Users.Someone"'],{'10'})
            self.assertEqual({name for name,_ in client.stored},{b'"INBOX"',b'"Archive"'})

    def test_namespace_parser(self):
        groups=r.parse_namespaces(b' (("" "/" "X-EXT" ("value"))) (("Other Users/" "/")) (("Shared/" "/")) ')
        self.assertEqual(groups[0][0][:2],[b'',b'/'])
        self.assertEqual(groups[1][0][0],b'Other Users/')
        self.assertEqual(r.parse_namespaces(b'(("INBOX." ".") ("Personal/" "/")) NIL NIL')[0][1][0],b'Personal/')
        for raw in (b'NIL NIL NIL',b'(("" ".")) NIL',b'(("" ".")) NIL NIL junk',b'(("" "xx")) NIL NIL'):
            with self.subTest(raw=raw),self.assertRaises(RuntimeError):
                r.parse_namespaces(raw)

    def test_ambiguous_namespace_stops_before_deletion(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(r,'STATE',Path(tmp)/'reset.json'):
            client=ResetServer()
            client.namespace=lambda: ('OK',[b'(("" ".")) NIL (("" "."))'])
            with self.assertRaisesRegex(RuntimeError,'Ambiguous'):
                r.clear_personal_mail(client,{'phase':'clearing'})
            self.assertEqual(client.stored,[])


if __name__=='__main__':
    unittest.main()
