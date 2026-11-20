#!/usr/bin/env python3
"""Resumable full migration of the verified Rackspace snapshot to Fastmail."""
import base64
from collections import Counter, defaultdict
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import getpass
import hashlib
import imaplib
import json
import os
from pathlib import Path
import re
import sqlite3
import ssl
import sys
import download as d
import fastmail_test as t
from reset_fastmail import personal_folders

STATE=Path(__file__).resolve().parent/'fastmail-upload.json'
STANDARD=t.STANDARD_FLAGS


def load_plan():
    report=json.loads((d.ROOT/'report.json').read_text())
    if report['status']!='verified_snapshot':
        raise RuntimeError('Completed Rackspace snapshot required')
    db=sqlite3.connect((d.ROOT/'inventory.sqlite3').as_uri()+'?mode=ro',uri=True)
    db.row_factory=sqlite3.Row
    try:
        rows=[dict(r) for r in db.execute('SELECT * FROM messages WHERE active=1 ORDER BY folder,CAST(uid AS INTEGER)')]
    finally:
        db.close()
    if len(rows)!=report['total_messages']:
        raise RuntimeError('Source count differs from verified report')
    by_folder=defaultdict(list)
    by_hash=defaultdict(list)
    for row in rows:
        if '\\Deleted' in row['flags'].split():
            raise RuntimeError('Source has deletion-marked messages; resolve before uploading')
        row['requested_date'],_=t.planned_date(row)
        by_hash[row['sha256']].append(row)
        by_folder[row['folder']].append(row)
    # Fastmail shares one date for identical content across copies.
    for copies in by_hash.values():
        if len({t.instant(r['requested_date']) for r in copies}) > 1 and d.cfg.CONFIG['duplicate_date_policy'] != 'earliest':
            raise RuntimeError('Identical bytes have conflicting dates; review and explicitly choose earliest if appropriate')
        flagsets = {tuple(sorted(set(r['flags'].split()) - {'\\Recent'})) for r in copies}
        if len(flagsets) > 1 and d.cfg.CONFIG['duplicate_flag_policy'] != 'union':
            raise RuntimeError('Identical bytes have conflicting flags; review before selecting union policy')
        date=min((r['requested_date'] for r in copies),key=t.instant)
        flags=set()
        for row in copies:
            for flag in row['flags'].split():
                if flag in STANDARD or re.fullmatch(r'[A-Za-z0-9$_.-]+',flag):
                    flags.add(flag)
                elif flag!='\\Recent':
                    raise RuntimeError('Unsupported source flag syntax')
        for row in copies:
            row['target_date']=date
            row['target_flags']=sorted(flags)
    for folder in report['folders']:
        if len(by_folder[folder['id']])!=folder.get('message_count',0):
            raise RuntimeError('Source folder count mismatch')
    return report,by_folder,by_hash


def encode_name(text):
    # Modified UTF-7 for IMAP mailbox names (ASCII names are unchanged).
    out=[];pending=[]
    def flush():
        if pending:
            token=base64.b64encode(''.join(pending).encode('utf-16-be')).decode().rstrip('=').replace('/',',')
            out.append('&'+token+'-');pending.clear()
    for char in text:
        if 32<=ord(char)<=126:
            flush();out.append('&-' if char=='&' else char)
        else:
            pending.append(char)
    flush()
    return ''.join(out).encode('ascii')


def mappings(source_folders,destination):
    special={}
    for folder in destination:
        for flag in folder['flags'].lower().split():
            if flag in ('\\sent','\\drafts','\\trash','\\junk'):
                special[flag]=folder['name']
    result={}
    for folder in source_folders:
        name=folder['name']
        parts=name.split(folder['delimiter']) if folder['delimiter'] else [name]
        if parts[0].upper()=='INBOX':
            parts=parts[1:]
        if not parts:
            dest='INBOX'
        elif len(parts)==1 and parts[0].lower() in ('sent','drafts','trash','spam','junk'):
            role={'sent':'\\sent','drafts':'\\drafts','trash':'\\trash','spam':'\\junk','junk':'\\junk'}[parts[0].lower()]
            dest=special.get(role,{'\\sent':'Sent','\\drafts':'Drafts','\\trash':'Trash','\\junk':'Spam'}[role])
        else:
            if any('/' in part for part in parts):
                raise RuntimeError('Source folder component contains destination delimiter')
            dest='/'.join(parts)
        result[folder['id']]=dest
    return result


def select(client,name,writable=False):
    d.ok(client.select(d.quote(encode_name(name)),readonly=not writable))
    validity=client.response('UIDVALIDITY')[1]
    if not validity or not validity[0]:
        raise RuntimeError('Missing destination UIDVALIDITY')
    uids=d.ok(client.uid('SEARCH',None,'ALL'))[0].split()
    return validity[0].decode(),[u.decode() for u in uids]


def fetched(client,uid):
    data=d.ok(client.uid('FETCH',str(uid),'(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[])'))
    parts=[x for x in data if isinstance(x,tuple)]
    if len(parts)!=1:
        raise RuntimeError('Missing destination body')
    observed_uid,flags,date,size=d.metadata(parts[0][0])
    raw=parts[0][1]
    if observed_uid!=str(uid) or len(raw)!=size:
        raise RuntimeError('Destination UID/size mismatch')
    return {'uid':str(uid),'sha256':d.sha(raw),'size':size,'internaldate':date,'flags':flags}


def check_record(record,expected):
    if record['sha256']!=expected['sha256'] or record['size']!=expected['size']:
        raise RuntimeError('Destination bytes differ from source')
    if t.instant(record['internaldate'])!=t.instant(expected['target_date']):
        raise RuntimeError('Destination received date differs from plan')
    if set(record['flags'].split()) & STANDARD != set(expected['target_flags']) & STANDARD:
        raise RuntimeError('Destination read/starred/answered/draft flags differ from source')


def metadata_scan(client,uids):
    found={}
    for start in range(0,len(uids),200):
        batch=uids[start:start+200]
        for line in d.ok(client.uid('FETCH',','.join(batch),'(UID FLAGS INTERNALDATE RFC822.SIZE)')):
            if isinstance(line,bytes) and b'UID ' in line:
                uid,flags,date,size=d.metadata(line)
                found[uid]={'uid':uid,'flags':flags,'internaldate':date,'size':size}
        if not set(batch)<=set(found):
            raise RuntimeError('Destination changed during verification')
    return found


def scan_folder(client,name,expected,cache,ignore_uids=None):
    validity,uids=select(client,name)
    by_hash=defaultdict(list)
    for row in expected:by_hash[row['sha256']].append(row)
    seen=defaultdict(list)
    metadata=metadata_scan(client,uids)
    for uid in uids:
        if uid in (ignore_uids or set()):
            continue
        record=metadata[uid]
        saved=cache.execute('SELECT sha256 FROM verified WHERE folder=? AND validity=? AND uid=?',(name,validity,uid)).fetchone()
        if saved:
            record['sha256']=saved[0]
        else:
            record=fetched(client,uid)
        copies=by_hash.get(record['sha256'])
        if not copies or len(seen[record['sha256']])>=len(copies):
            raise RuntimeError('Unexpected or surplus mail in destination folder '+name)
        check_record(record,copies[0])
        cache.execute('INSERT OR REPLACE INTO verified VALUES (?,?,?,?)',(name,validity,uid,record['sha256']))
        seen[record['sha256']].append(record)
    cache.commit()
    return validity,seen


def upload_folder(client,name,expected,cache,ignore_uids=None):
    validity,seen=scan_folder(client,name,expected,cache,ignore_uids)
    consumed=Counter()
    for index,row in enumerate(expected,1):
        sha=row['sha256']
        if consumed[sha]<len(seen[sha]):
            consumed[sha]+=1
            continue
        raw=(d.ROOT/row['path']).read_bytes()
        if len(raw)!=row['size'] or d.sha(raw)!=sha:
            raise RuntimeError('Local source message changed')
        # APPENDUID is required; a lost response is reconciled by scanning on the next run.
        client.response('APPENDUID')
        response=d.ok(client.append(d.quote(encode_name(name)), '('+' '.join(row['target_flags'])+')',
                                    '"'+row['target_date']+'"',raw))
        append_response=client.response('APPENDUID')[1]
        match=None
        for item in append_response or []:
            if isinstance(item,bytes):
                match=re.fullmatch(rb'(\d+) (\d+)',item.strip())
                if match:break
        if not match:
            for item in response:
                if isinstance(item,bytes):
                    match=re.search(rb'\[APPENDUID (\d+) (\d+)\]',item)
                    if match:break
        if not match or match[1].decode()!=validity:
            raise RuntimeError('APPEND outcome needs reconciliation; rerun the same command')
        uid=match[2].decode()
        if uid in (ignore_uids or set()):
            raise RuntimeError('Fastmail reused a protected destination UID; reconcile before continuing')
        if any(r['uid']==uid for records in seen.values() for r in records):
            raise RuntimeError('Fastmail collapsed an identical source copy; review multiplicity before continuing')
        record=fetched(client,uid)
        check_record(record,row)
        cache.execute('INSERT OR REPLACE INTO verified VALUES (?,?,?,?)',(name,validity,uid,sha))
        cache.commit()
        seen[sha].append(record);consumed[sha]+=1
        if index%100==0 or index==len(expected):
            print(f'  {name}: {index:,}/{len(expected):,} uploaded and verified',flush=True)
    _,seen=scan_folder(client,name,expected,cache,ignore_uids)
    if sum(map(len,seen.values()))!=len(expected):
        raise RuntimeError('Destination folder count mismatch')
    used=Counter();mapping=[];keyword_gaps=Counter()
    for row in expected:
        record=seen[row['sha256']][used[row['sha256']]];used[row['sha256']]+=1
        mapping.append({'source_folder':row['folder'],'source_uidvalidity':row['validity'],
                        'source_uid':row['uid'],'destination_uid':record['uid'],'sha256':row['sha256']})
        for flag in set(row['target_flags'])-set(record['flags'].split())-STANDARD:
            keyword_gaps[flag]+=1
    return {'uidvalidity':validity,'message_count':len(expected),'messages':mapping,
            'unsupported_or_changed_keywords':dict(keyword_gaps)}


def cleanup_test_folders(client,state):
    if state.get('test_cleanup_complete'):return
    test=json.loads(t.STATE.read_text())
    if test['status']!='verified_imap_awaiting_visual_check':
        raise RuntimeError('Successful original-message date test required')
    probe=json.loads((STATE.parent/'fastmail-date-probe.json').read_text())
    named={test['folder']}|{p['folder'] for p in probe['probes']}
    for folder in personal_folders(client):
        if not folder['selectable']:continue
        validity,uids=d.snapshot(client,folder)
        if folder['name']==test['folder']:
            # Resume accepts a subset after an interrupted expunge; all bodies must be known samples.
            hashes=Counter(r['sha256'] for r in test['messages'])
            for uid in uids:
                record=fetched(client,uid)
                if hashes[record['sha256']]<=0:raise RuntimeError('Unexpected mail in date test folder')
                hashes[record['sha256']]-=1
            if uids:
                d.ok(client.select(d.quote(base64.b64decode(folder['wire_name'])),readonly=False))
                d.ok(client.uid('STORE',','.join(uids),'+FLAGS.SILENT','(\\Deleted)'))
                d.ok(client.uid('EXPUNGE',','.join(uids)))
        elif uids:
            raise RuntimeError('Unexpected mail after reset in '+folder['name'])
    for folder in personal_folders(client):
        if folder['name'] in named:
            if folder['selectable'] and d.snapshot(client,folder)[1]:
                raise RuntimeError('Test folder is not empty')
            # SELECT INBOX first so no mailbox being deleted is selected.
            d.ok(client.select(b'INBOX',readonly=True))
            d.ok(client.delete(d.quote(base64.b64decode(folder['wire_name']))))
    state['test_cleanup_complete']=True
    d.write_json(STATE,state)


def run(client,source,by_folder,state,cache):
    cleanup_test_folders(client,state)
    destinations=personal_folders(client)
    # The observed personal namespace is empty-prefix with slash delimiter.
    namespace=json.loads(STATE.with_name('fastmail-namespaces.json').read_text())['raw_namespace']
    if not namespace.startswith('(("" "/"))'):
        raise RuntimeError('Destination namespace differs from tested Fastmail layout')
    mapping=mappings(source['folders'],destinations)
    grouped=defaultdict(list)
    for folder in source['folders']:grouped[mapping[folder['id']]].extend(by_folder[folder['id']])
    desired=set(grouped)
    for name in list(desired):
        parts=name.split('/')
        desired.update('/'.join(parts[:n]) for n in range(1,len(parts)))
    existing={f['name']:f for f in destinations}
    special_roles={'\\sent','\\drafts','\\trash','\\junk','\\archive','\\all','\\flagged','\\important'}
    for name,folder in sorted(existing.items(),key=lambda item:item[0].count('/'),reverse=True):
        if name in desired or name.upper()=='INBOX' or special_roles & set(folder['flags'].lower().split()):continue
        if folder['selectable'] and d.snapshot(client,folder)[1]:
            raise RuntimeError('Unexpected nonempty destination folder: '+name)
        d.ok(client.select(b'INBOX',readonly=True))
        status,_=client.delete(d.quote(base64.b64decode(folder['wire_name'])))
        if status=='OK':existing.pop(name,None)
        else:state.setdefault('retained_empty_folders',[]).append(name)
    for name in sorted(desired,key=lambda n:(n.count('/'),n)):
        if name not in existing:
            d.ok(client.create(d.quote(encode_name(name))))
        d.ok(client.subscribe(d.quote(encode_name(name))))
    state['folder_mapping']=[{'source':f['name'],'destination':mapping[f['id']]} for f in source['folders']]
    state['status']='uploading';d.write_json(STATE,state)
    for name,rows in sorted(grouped.items()):
        print(f'Folder {name!r}: {len(rows):,} source messages',flush=True)
        state.setdefault('folders',{})[name]=upload_folder(client,name,rows,cache)
        d.write_json(STATE,state)
    print('Final account-wide verification...',flush=True)
    for folder in personal_folders(client):
        if not folder['selectable']:continue
        if folder['name'] not in grouped:
            if d.snapshot(client,folder)[1]:raise RuntimeError('Unexpected messages outside migrated folders')
        else:
            state['folders'][folder['name']]=upload_folder(client,folder['name'],grouped[folder['name']],cache)
    count=sum(f['message_count'] for f in state['folders'].values())
    if count!=source['total_messages']:raise RuntimeError('Final account count differs from source')
    state.update(status='verified_snapshot_uploaded',verified_utc=datetime.now(timezone.utc).isoformat(),total_messages=count)
    d.write_json(STATE,state)
    print(f'VERIFIED: {count:,} messages in {len(grouped)} destination folders. Original bytes, dates and standard flags checked.')
    gaps=Counter()
    for folder in state['folders'].values():gaps.update(folder['unsupported_or_changed_keywords'])
    if gaps:print('Custom keyword differences recorded:',dict(gaps))
    print('Rackspace still receives mail. A final catch-up and DNS cutover are still required.')


def main():
    os.umask(0o077)
    if not sys.stdin.isatty():raise RuntimeError('Run in your terminal for a hidden password prompt')
    d.cfg.destination_ready()
    d.cfg.freeze_policy()
    if d.cfg.CONFIG['cutover_complete']:
        raise RuntimeError('After cutover use catch_up.py --late-arrivals')
    if d.cfg.CONFIG['folder_mapping_reviewed'] is not True or d.cfg.CONFIG['visual_date_test_passed'] is not True:
        raise RuntimeError('Review folder mapping and visually confirm the real date test before full upload')
    with ExitStack() as stack:
        lock=stack.enter_context(STATE.with_suffix('.lock').open('a'))
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        source_lock=stack.enter_context((d.ROOT/'.lock').open('rb'))
        fcntl.flock(source_lock,fcntl.LOCK_SH|fcntl.LOCK_NB)
        source,by_folder,by_hash=load_plan()
        if STATE.exists():
            state=json.loads(STATE.read_text())
            if state['source_finished_utc']!=source['finished_utc']:
                raise RuntimeError('Source snapshot changed. Finish/review this upload before planning catch-up.')
        else:
            state={'status':'planned','source_finished_utc':source['finished_utc'],'source_messages':source['total_messages'],
                   'date_policy':dict(d.cfg.CONFIG),
                   'source_date_corrections':sum(r['requested_date']!=r['internaldate'] for rows in by_folder.values() for r in rows),
                   'identical_extra_copies_preserved':sum(len(v)-1 for v in by_hash.values()),'folders':{}}
            d.write_json(STATE,state)
        # Check originals even for files whose destination content was verified in a previous run.
        print('Verifying local source before upload...',flush=True)
        for rows in by_folder.values():
            for row in rows:
                path=d.ROOT/row['path']
                if path.stat().st_size!=row['size']:raise RuntimeError('Source size mismatch')
                with path.open('rb') as stream:
                    if hashlib.file_digest(stream,'sha256').hexdigest()!=row['sha256']:raise RuntimeError('Source checksum mismatch')
        print(f'Source: {source["total_messages"]:,} messages; {state["source_date_corrections"]:,} historical date corrections.',flush=True)
        cache=sqlite3.connect(STATE.with_suffix('.sqlite3'))
        stack.callback(cache.close)
        cache.execute('CREATE TABLE IF NOT EXISTS verified (folder TEXT,validity TEXT,uid TEXT,sha256 TEXT,PRIMARY KEY(folder,validity,uid))')
        password=getpass.getpass('Fastmail app password (hidden): ')
        with imaplib.IMAP4_SSL(d.DEST_HOST,993,ssl_context=ssl.create_default_context(),timeout=120) as client:
            d.login_destination(client,password);password=None
            run(client,source,by_folder,state,cache)


if __name__=='__main__':
    try:main()
    except (Exception,KeyboardInterrupt) as error:
        print(f'Upload paused: {error}. Rerun the same command to reconcile and resume.',file=sys.stderr)
        sys.exit(1)
