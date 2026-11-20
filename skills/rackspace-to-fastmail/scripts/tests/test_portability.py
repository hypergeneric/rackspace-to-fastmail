import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import migration_config as cfg
import fastmail_test as t
import catch_up as c
from test_catch_up import CatchupServer, row
from test_upload import cache


class PortabilityTests(unittest.TestCase):
    def test_default_dates_unchanged_and_timezone_instants_equal(self):
        row={'internaldate':'03-Dec-2012 14:18:44 -0500','sent_utc':'2006-10-13T02:02:40+00:00'}
        with patch.dict(cfg.CONFIG,{'repair_received_days':[]}):
            self.assertEqual(t.planned_date(row)[0],row['internaldate'])
        self.assertEqual(t.instant('13-Oct-2007 02:02:40 +0000'),t.instant('12-Oct-2007 22:02:40 -0400'))

    def test_approved_day_does_not_invent_unknown_timezone_or_use_future_sent_date(self):
        with patch.dict(cfg.CONFIG,{'repair_received_days':['2012-12-03']}):
            for sent in (None,'2006-10-13T02:02:40','2013-01-01T00:00:00+00:00'):
                row={'internaldate':'03-Dec-2012 14:18:44 -0500','sent_utc':sent}
                self.assertEqual(t.planned_date(row)[0],row['internaldate'])

    def test_binding_cannot_switch_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'migration-config.json';path.write_text('{}')
            with patch.object(cfg,'BASE',root),patch.object(cfg,'PATH',path),patch.dict(cfg.CONFIG,{
                'source_account':'a@example.com','destination_account':'b@example.com'}):
                cfg.bind()
                cfg.bind()
                with patch.dict(cfg.CONFIG,{'destination_account':'c@example.com'}):
                    with self.assertRaisesRegex(RuntimeError,'identity changed'):cfg.bind()

    def test_defaults_do_not_authorize_replacement(self):
        with patch.object(cfg,'bind'),patch.dict(cfg.CONFIG,{
            'replace_destination_authorized':False,'background_import_disabled':False}):
            with self.assertRaisesRegex(RuntimeError,'authorization'):cfg.destination_ready()

    def test_policy_cannot_change_after_freeze(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(cfg,'BASE',Path(tmp)),patch.dict(cfg.CONFIG,{
            'date_policy_reviewed':True,'repair_received_days':[]}):
            cfg.freeze_policy()
            with patch.dict(cfg.CONFIG,{'repair_received_days':['2012-12-03']}):
                with self.assertRaisesRegex(RuntimeError,'policy changed'):cfg.freeze_policy()

    def test_all_additions_pass_before_any_deletion_pass(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
            root=Path(tmp);client=CatchupServer();db=cache();calls=[]
            original=row(root,1)
            state={'baseline':{'folders':{}},'mode':'pre-cutover','old_dates':{},'old_flags':{}}
            folders=[{'id':name,'name':name,'delimiter':'.'} for name in ('Alpha','Zulu')]
            report={'folders':folders,'total_messages':2,'finished_utc':'fixture'}
            def reconcile(*args,**kwargs):
                calls.append((args[1],kwargs.get('defer_deletes',False)))
                if args[1]=='Zulu':raise RuntimeError('Simulated later folder upload failure')
                return {'messages':[],'uidvalidity':'42'}
            with patch.object(c,'personal_folders',return_value=[{'name':name,'flags':''} for name in ('Alpha','Zulu')]),patch.object(c,'reconcile_folder',side_effect=reconcile):
                with self.assertRaisesRegex(RuntimeError,'later folder'):
                    c.sync(client,state,report,{'Alpha':[original],'Zulu':[original]},db)
            self.assertEqual(calls,[('Alpha',True),('Zulu',True)])
            db.close()

    def test_deferred_pass_preserves_stale_copy(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
            root=Path(tmp);client=CatchupServer();db=cache();original=row(root,1)
            with patch.object(c.d,'ROOT',root):
                prior=c.u.upload_folder(client,'INBOX',[original],db)
                result=c.reconcile_folder(client,'INBOX',[],prior,{},db,False,defer_deletes=True)
                self.assertEqual(len(client.messages),1)
                self.assertEqual(result['removed_stale_managed_copies'],0)
                c.reconcile_folder(client,'INBOX',[],prior,{},db,False)
                self.assertEqual(len(client.messages),0)
            db.close()


if __name__=='__main__':unittest.main()
