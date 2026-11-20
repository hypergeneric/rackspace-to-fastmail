#!/usr/bin/env python3
"""Replace the explicitly discarded Fastmail import, then test original dates.
Only Fastmail personal mail is cleared. Rackspace/local source bytes are untouched.
"""
import base64
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import getpass
import hashlib
import imaplib
import json
from pathlib import Path
import os
import re
import sqlite3
import ssl
import sys
import download as d
import fastmail_test as t

STATE=Path(__file__).resolve().parent/'fastmail-reset.json'


def verify_source():
    with (d.ROOT/'.lock').open('rb') as lock:
        fcntl.flock(lock,fcntl.LOCK_SH|fcntl.LOCK_NB)
        report=json.loads((d.ROOT/'report.json').read_text())
        if report['account']!=d.ACCOUNT or report['status']!='verified_snapshot':
            raise RuntimeError('Verified local source snapshot required before destination reset')
        db=sqlite3.connect((d.ROOT/'inventory.sqlite3').as_uri()+'?mode=ro',uri=True)
        try:
            counts={}
            total=0
            print('Checking every local source message before clearing Fastmail...',flush=True)
            for folder,path,size,checksum in db.execute('SELECT folder,path,size,sha256 FROM messages WHERE active=1'):
                source=d.ROOT/path
                if not source.resolve().is_relative_to(d.ROOT.resolve()) or source.stat().st_size!=size:
                    raise RuntimeError('Invalid local source path or size')
                with source.open('rb') as stream:
                    if hashlib.file_digest(stream,'sha256').hexdigest()!=checksum:
                        raise RuntimeError('Local source checksum mismatch; destination was not cleared')
                counts[folder]=counts.get(folder,0)+1
                total+=1
            if total!=report['total_messages']:
                raise RuntimeError('Local source total mismatch')
            if set(counts)-{f['id'] for f in report['folders']}:
                raise RuntimeError('Unmapped local source folder')
            for folder in report['folders']:
                if counts.get(folder['id'],0)!=folder.get('message_count',0):
                    raise RuntimeError('Local source folder count mismatch')
            print(f'Local source verified: {total:,} messages.',flush=True)
            return {'messages':total,'snapshot_finished_utc':report['finished_utc']}
        finally:
            db.close()


def parse_namespaces(raw):
    """Parse RFC namespace lists without treating shared namespaces as personal."""
    if not isinstance(raw,bytes):
        raise RuntimeError('Unsupported namespace response')
    pos=0
    def whitespace():
        nonlocal pos
        while pos<len(raw) and raw[pos:pos+1] in (b' ',b'\t',b'\r',b'\n'):
            pos+=1
    def value(depth=0):
        nonlocal pos
        whitespace()
        if depth>10 or pos>=len(raw):
            raise RuntimeError('Malformed namespace response')
        if raw[pos:pos+1]==b'(':
            pos+=1;items=[]
            while True:
                whitespace()
                if pos>=len(raw):
                    raise RuntimeError('Unterminated namespace list')
                if raw[pos:pos+1]==b')':
                    pos+=1
                    return items
                items.append(value(depth+1))
        if raw[pos:pos+1]==b'"':
            pos+=1;result=bytearray()
            while pos<len(raw):
                char=raw[pos];pos+=1
                if char==34:
                    return bytes(result)
                if char==92:
                    if pos>=len(raw) or raw[pos] not in (34,92):
                        raise RuntimeError('Invalid namespace escape')
                    char=raw[pos];pos+=1
                result.append(char)
            raise RuntimeError('Unterminated namespace string')
        if raw[pos:pos+3].upper()==b'NIL':
            pos+=3
            return None
        raise RuntimeError('Unsupported namespace token')
    groups=[value(),value(),value()]
    whitespace()
    if pos!=len(raw) or not groups[0]:
        raise RuntimeError('Missing personal namespace or unexpected trailing data')
    for group in groups:
        if group is None:
            continue
        if not isinstance(group,list):
            raise RuntimeError('Malformed namespace group')
        for entry in group:
            if (not isinstance(entry,list) or len(entry)<2 or not isinstance(entry[0],bytes)
                    or (entry[1] is not None and (not isinstance(entry[1],bytes) or len(entry[1])!=1))):
                raise RuntimeError('Malformed namespace entry')
    return groups


def personal_folders(client):
    capabilities={c.decode().upper() if isinstance(c,bytes) else c.upper() for c in client.capabilities}
    if 'UIDPLUS' not in capabilities or 'NAMESPACE' not in capabilities:
        raise RuntimeError('UIDPLUS and NAMESPACE required for scoped destination reset')
    response=d.ok(client.namespace())
    if not response or len(response)!=1 or not isinstance(response[0],bytes):
        raise RuntimeError('Unsupported namespace response format')
    d.write_json(STATE.with_name('fastmail-namespaces.json'),
                 {'raw_namespace':response[0].decode('ascii',errors='backslashreplace')})
    personal,others,shared=parse_namespaces(response[0])
    excluded=(others or [])+(shared or [])
    if any(not entry[0] for entry in excluded):
        raise RuntimeError('Ambiguous empty shared/other-user namespace; refusing deletion')
    def belongs(name,entry):
        prefix,delimiter=entry[:2]
        return name.startswith(prefix) or bool(delimiter and prefix.endswith(delimiter) and name==prefix[:-1])
    selected=[]
    for folder in d.list_folders(client):
        name=base64.b64decode(folder['wire_name'])
        if any(belongs(name,entry) for entry in excluded):
            print(f'Skipping non-personal folder: {folder["name"]!r}',flush=True)
            continue
        if name.upper()!=b'INBOX' and not any(belongs(name,entry) for entry in personal):
            raise RuntimeError('Folder outside advertised personal namespaces; refusing deletion')
        selected.append(folder)
    if not any(base64.b64decode(f['wire_name']).upper()==b'INBOX' for f in selected):
        raise RuntimeError('Personal INBOX missing; refusing deletion')
    return selected


def clear_personal_mail(client,state):
    if state['phase']!='clearing':
        return
    folders=personal_folders(client)
    for folder in folders:
        if not folder['selectable']:
            continue
        validity,uids=d.snapshot(client,folder)
        entry=state.setdefault('folders',{}).setdefault(folder['id'],
            {'name':folder['name'],'uidvalidity':validity,'initial_count':len(uids),'empty':False})
        if entry['uidvalidity']!=validity:
            raise RuntimeError('Destination folder identity changed during reset')
        d.write_json(STATE,state)
        if uids:
            d.ok(client.select(d.quote(base64.b64decode(folder['wire_name'])),readonly=False))
            for start in range(0,len(uids),500):
                batch=','.join(uids[start:start+500])
                d.ok(client.uid('STORE',batch,'+FLAGS.SILENT','(\\Deleted)'))
                d.ok(client.uid('EXPUNGE',batch))
            print(f'Cleared {folder["name"]!r}: {len(uids):,} messages',flush=True)
        if d.snapshot(client,folder)[1]:
            raise RuntimeError('Destination repopulated during reset; stop any fetch/import first')
        entry['empty']=True
        d.write_json(STATE,state)
    # Include newly listed folders and verify the entire personal mailbox is empty.
    for folder in personal_folders(client):
        if folder['selectable'] and d.snapshot(client,folder)[1]:
            raise RuntimeError('Destination is not empty after reset; investigate before uploading')
    state['phase']='mail_cleared'
    state['cleared_utc']=datetime.now(timezone.utc).isoformat()
    d.write_json(STATE,state)
    print('All stale Fastmail mail cleared. Empty folders are retained for now.',flush=True)


def main():
    os.umask(0o077)
    if not sys.stdin.isatty():
        raise RuntimeError('Run in your terminal for the private password prompt')
    d.cfg.destination_ready()
    d.cfg.freeze_policy()
    if d.cfg.CONFIG["cutover_complete"]:
        raise RuntimeError("Cannot reset after DNS cutover")
    with ExitStack() as stack:
        source_lock=stack.enter_context((d.ROOT/'.lock').open('rb'))
        fcntl.flock(source_lock,fcntl.LOCK_SH|fcntl.LOCK_NB)
        for path in (STATE.with_suffix('.lock'),t.STATE.with_suffix('.lock')):
            lock=stack.enter_context(path.open('a'))
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        probe=json.loads((STATE.parent/'fastmail-date-probe.json').read_text())
        if not probe.get('after_delete',{}).get('requested_date_retained'):
            raise RuntimeError('Successful post-deletion synthetic date test required')
        if STATE.exists():
            state=json.loads(STATE.read_text())
            if state['phase']=='complete':
                print('Reset and original-message test already completed; refusing to clear mail again.')
                return
        else:
            state={'account':d.DEST_ACCOUNT,'host':d.DEST_HOST,'phase':'clearing',
                   'authorization':'Replacement authorized in migration-config.json; background import disabled',
                   'started_utc':datetime.now(timezone.utc).isoformat()}
        state['source_verification']=verify_source()
        test,bodies=t.prepare()
        d.write_json(STATE,state)
        print('This run clears stale personal mail from Fastmail, then uploads representative verified date-test messages.')
        password=getpass.getpass('Fastmail app password (hidden): ')
        with imaplib.IMAP4_SSL(d.DEST_HOST,993,ssl_context=ssl.create_default_context(),timeout=120) as client:
            d.login_destination(client,password)
            password=None
            clear_personal_mail(client,state)
            if state['phase']=='mail_cleared':
                test.pop('uidvalidity',None)
                test.pop('pending_source_sha256',None)
                test.pop('error',None)
                test['created']=False
                test['status']='planned_after_reset'
                for row in test['messages']:
                    row.pop('destination',None)
                d.write_json(t.STATE,test)
                state['phase']='testing'
                d.write_json(STATE,state)
            t.run(client,test,bodies)
            state['phase']='complete'
            state['test_folder']=test['folder']
            state['finished_utc']=datetime.now(timezone.utc).isoformat()
            d.write_json(STATE,state)


if __name__=='__main__':
    try:
        main()
    except (Exception,KeyboardInterrupt) as error:
        print(f'Reset/test stopped: {error}. Rerun the same command to reconcile and resume.',file=sys.stderr)
        sys.exit(1)
