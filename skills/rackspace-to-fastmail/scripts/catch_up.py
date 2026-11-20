#!/usr/bin/env python3
"""Refresh Rackspace and reconcile only migration-owned mail in Fastmail.
--late-arrivals only adds newly discovered source messages, leaving existing
Fastmail mail and flags alone after cutover. No Rackspace mail is modified.
"""
from collections import Counter,defaultdict
from contextlib import ExitStack
from datetime import datetime,timezone
import fcntl
import getpass
import hashlib
import imaplib
import json
import os
import re
import sqlite3
import ssl
import sys
import download as d
import fastmail_test as t
import upload_fastmail as u
from reset_fastmail import personal_folders

STATE=u.STATE.with_name('catch-up.json')


def key(row):
    return '|'.join((row['folder'],row['validity'],row['uid']))


def mapping_key(row):
    return '|'.join((row['source_folder'],row['source_uidvalidity'],row['source_uid']))


def source_keys(upload):
    return set(upload.get('seen_source_keys',[]))|{mapping_key(row) for folder in upload['folders'].values() for row in folder['messages']}


def classify(records,rows,prior,late):
    wanted=Counter(row['sha256'] for row in rows)
    known={row['destination_uid']:row['sha256'] for row in prior.get('messages',[])}
    retained=[];stale=[];ignored=set();taken=Counter()
    for record in sorted(records,key=lambda r:(r['uid'] not in known,int(r['uid']))):
        uid=record['uid'];sha=record['sha256']
        if uid in known and known[uid]!=sha:
            raise RuntimeError('Previously mapped destination content changed')
        if late and uid in known:
            ignored.add(uid)
        elif taken[sha]<wanted[sha]:
            retained.append(record);taken[sha]+=1
        elif uid in known:
            stale.append(record);ignored.add(uid)
        else:
            ignored.add(uid)
    return retained,stale,ignored


def reconcile_folder(client,name,rows,prior,old_flags,cache,late,defer_deletes=False):
    validity,uids=u.select(client,name)
    if prior and prior['uidvalidity']!=validity:
        raise RuntimeError('Destination folder identity changed: '+name)
    records=[]
    for uid,record in u.metadata_scan(client,uids).items():
        saved=cache.execute('SELECT sha256 FROM verified WHERE folder=? AND validity=? AND uid=?',(name,validity,uid)).fetchone()
        if saved:record['sha256']=saved[0]
        else:record=u.fetched(client,uid)
        records.append(record)
    retained,stale,ignore=classify(records,rows,prior,late)
    expected={row['sha256']:row for row in rows}
    if not late:
        for record in retained:
            target=expected[record['sha256']]
            if record['size']!=target['size'] or t.instant(record['internaldate'])!=t.instant(target['target_date']):
                raise RuntimeError('Existing migrated content/date differs from source plan')
            desired=set(target['target_flags'])
            actual=set(record['flags'].split())
            managed=u.STANDARD|set(old_flags.get(record['sha256'],[]))
            add=desired-actual
            remove=(actual & managed)-desired
            if add or remove:
                u.select(client,name,writable=True)
                if add:d.ok(client.uid('STORE',record['uid'],'+FLAGS.SILENT','('+' '.join(sorted(add))+')'))
                if remove:d.ok(client.uid('STORE',record['uid'],'-FLAGS.SILENT','('+' '.join(sorted(remove))+')'))
    # Add and verify new/moved-in mail before removing any old managed copies.
    result=u.upload_folder(client,name,rows,cache,ignore_uids=ignore)
    if stale and not defer_deletes:
        u.select(client,name)
        for record in stale:
            check=u.fetched(client,record['uid'])
            if check['sha256']!=record['sha256']:
                raise RuntimeError('Stale destination copy changed; refusing deletion')
        u.select(client,name,writable=True)
        for start in range(0,len(stale),200):
            batch=','.join(r['uid'] for r in stale[start:start+200])
            d.ok(client.uid('STORE',batch,'+FLAGS.SILENT','(\\Deleted)'))
            d.ok(client.uid('EXPUNGE',batch))
        _,remaining=u.select(client,name)
        if any(r['uid'] in remaining for r in stale):
            raise RuntimeError('Stale managed messages remain after expunge')
    result['removed_stale_managed_copies']=0 if defer_deletes else len(stale)
    result['unmanaged_or_preserved_destination_copies']=len(ignore)-len(stale)
    return result


def sync(client,state,report,by_folder,cache):
    old=state['baseline']
    late=state['mode']=='late-arrivals'
    destinations=personal_folders(client)
    mapping=u.mappings(report['folders'],destinations)
    for name in mapping.values():
        if not isinstance(name,str):raise RuntimeError('Invalid folder mapping')
    grouped=defaultdict(list)
    seen=source_keys(old)
    current_keys=set()
    old_dates=state['old_dates'];old_flags=state['old_flags']
    for folder in report['folders']:
        name=mapping[folder['id']]
        grouped[name]  # preserve empty folders
        for row in by_folder[folder['id']]:
            current_keys.add(key(row))
            if late and key(row) in seen:continue
            if row['sha256'] in old_dates:row['target_date']=old_dates[row['sha256']]
            grouped[name].append(row)
    existing={f['name']:f for f in destinations}
    desired=set(grouped)
    for name in list(desired):
        parts=name.split('/')
        desired.update('/'.join(parts[:i]) for i in range(1,len(parts)))
    for name in sorted(desired,key=lambda n:(n.count('/'),n)):
        if name not in existing:d.ok(client.create(d.quote(u.encode_name(name))))
        d.ok(client.subscribe(d.quote(u.encode_name(name))))
    names=set(grouped) if late else set(grouped)|set(old['folders'])
    # First pass persists no final mapping: a restart repeats reconciliation safely.
    # All move targets are verified before any old folder membership is removed.
    if not late:
        for name in sorted(names):
            if name not in existing and name not in desired:
                continue
            reconcile_folder(client,name,grouped.get(name,[]),old['folders'].get(name,{}),
                             old_flags,cache,late,defer_deletes=True)
    new={}
    for name in sorted(names):
        if name not in existing and name not in desired:
            # Removed source folder also absent at destination: nothing left to remove.
            continue
        rows=grouped.get(name,[])
        if late and not rows:continue
        print(f'Reconciling {name!r}: {len(rows):,} '+('new source messages' if late else 'source messages'),flush=True)
        result=reconcile_folder(client,name,rows,old['folders'].get(name,{}),old_flags,cache,late)
        new[name]=result
        state.setdefault('progress',{})[name]=result
        d.write_json(STATE,state)
    # Recheck all target memberships after modifications; do not overwrite Fastmail-only arrivals.
    for name,result in new.items():
        validity,uids=u.select(client,name)
        if validity!=result['uidvalidity']:raise RuntimeError('Destination identity changed at final check')
        expected_uids={r['destination_uid'] for r in result['messages']}
        if not expected_uids<=set(uids):raise RuntimeError('Destination messages disappeared during catch-up')
        for record in u.metadata_scan(client,list(expected_uids)).values():
            target_uid=next(r for r in result['messages'] if r['destination_uid']==record['uid'])
            record['sha256']=target_uid['sha256']
            target=next(r for r in grouped.get(name,[]) if key(r)==mapping_key(target_uid))
            u.check_record(record,target)
    if late:
        output=dict(old)
        output['folders']={name:dict(folder) for name,folder in old['folders'].items()}
        for name,result in new.items():
            if name in output['folders']:
                combined=output['folders'][name]
                combined['messages']=combined['messages']+result['messages']
                combined['message_count']=len(combined['messages'])
            else:output['folders'][name]=result
        output['seen_source_keys']=sorted(seen|current_keys)
    else:
        output=dict(old)
        output['folders']={name:result for name,result in new.items() if name in grouped}
        output['seen_source_keys']=sorted(current_keys)
        if sum(f['message_count'] for f in output['folders'].values())!=report['total_messages']:
            raise RuntimeError('Source and migrated message totals differ')
    output.update(status='verified_incremental_sync',source_finished_utc=report['finished_utc'],
                  verified_utc=datetime.now(timezone.utc).isoformat(),last_sync_mode=state['mode'],
                  source_messages=report['total_messages'],
                  folder_mapping=[{'source':f['name'],'destination':mapping[f['id']]} for f in report['folders']])
    output['canonical_dates']=dict(old_dates)
    for rows in by_folder.values():
        for row in rows:
            output['canonical_dates'].setdefault(row['sha256'],row['target_date'])
    output['total_messages']=sum(f['message_count'] for f in output['folders'].values())
    d.write_json(u.STATE,output)
    new_keys=current_keys-seen
    state.update(phase='complete',finished_utc=output['verified_utc'],new_source_messages=len(new_keys),
                 source_total=report['total_messages'],managed_destination_total=output['total_messages'])
    d.write_json(STATE,state)
    print(f'CATCH-UP VERIFIED: {len(new_keys):,} newly discovered source messages; {report["total_messages"]:,} current source messages.',flush=True)
    print('Rackspace unchanged. Fastmail-only messages were preserved. No DNS changes made.')


def main():
    os.umask(0o077)
    if sys.argv[1:] not in ([],['--late-arrivals']):raise RuntimeError('Usage: python3 migration/catch_up.py [--late-arrivals]')
    if not sys.stdin.isatty():raise RuntimeError('Run in your terminal for private password prompts')
    d.cfg.bind()
    d.cfg.freeze_policy()
    mode='late-arrivals' if '--late-arrivals' in sys.argv else 'pre-cutover'
    if d.cfg.CONFIG['cutover_complete'] and mode != 'late-arrivals':
        raise RuntimeError('DNS has switched; use --late-arrivals')
    with ExitStack() as stack:
        for path,read_only in ((u.STATE.with_suffix('.lock'),False),(d.ROOT/'.lock',True)):
            lock=stack.enter_context(path.open('rb' if read_only else 'a'))
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        state=json.loads(STATE.read_text()) if STATE.exists() else None
        if not state or state['phase']=='complete':
            old=json.loads(u.STATE.read_text())
            if old['status'] not in ('verified_snapshot_uploaded','verified_incremental_sync'):
                raise RuntimeError('Complete the initial Fastmail upload before catch-up')
            if mode=='pre-cutover' and old.get('last_sync_mode')=='late-arrivals':
                raise RuntimeError('Late-arrival mode has already begun; keep using --late-arrivals after cutover')
            report,by_folder,by_hash=u.load_plan()
            if report['finished_utc']!=old['source_finished_utc']:
                raise RuntimeError('Source refreshed outside this tool; review baseline before catch-up')
            state={'mode':mode,'phase':'downloading','baseline':old,
                   'old_dates':{sha:old.get('canonical_dates',{}).get(sha,rows[0]['target_date']) for sha,rows in by_hash.items()},
                   'old_flags':{sha:rows[0]['target_flags'] for sha,rows in by_hash.items()},
                   'started_utc':datetime.now(timezone.utc).isoformat()}
            d.write_json(STATE,state)
        elif state['mode']!=mode:
            raise RuntimeError('Resume the unfinished catch-up using its original mode first')
        if state['phase']=='downloading':
            password=getpass.getpass('Rackspace password (hidden): ')
            with imaplib.IMAP4_SSL(d.HOST,993,ssl_context=ssl.create_default_context(),timeout=120) as client:
                d.ok(client.login(d.ACCOUNT,password));password=None
                d.download(client,d.ROOT)
            state['phase']='syncing';d.write_json(STATE,state)
        report,by_folder,by_hash=u.load_plan()
        # Source data may be shared with read-only analysis; verify every current original.
        for rows in by_folder.values():
            for row in rows:
                path=d.ROOT/row['path']
                if path.stat().st_size!=row['size']:raise RuntimeError('Local source size mismatch')
                with path.open('rb') as stream:
                    if hashlib.file_digest(stream,'sha256').hexdigest()!=row['sha256']:raise RuntimeError('Local source checksum mismatch')
        previous_validities = {}
        for folder in state['baseline']['folders'].values():
            for item in folder['messages']:
                previous_validities.setdefault(item['source_folder'], set()).add(item['source_uidvalidity'])
        for folder, rows in by_folder.items():
            if rows and folder in previous_validities and {r['validity'] for r in rows} != previous_validities[folder]:
                raise RuntimeError('Source UIDVALIDITY changed; reconcile identities by content before any delta writes')
        oldkeys=source_keys(state['baseline'])
        nowkeys={key(r) for rows in by_folder.values() for r in rows}
        print(f'Source refreshed: {len(nowkeys):,} messages; {len(nowkeys-oldkeys):,} new source IDs; {len(oldkeys-nowkeys):,} prior IDs absent.',flush=True)
        print('Mode:',mode,flush=True)
        password=getpass.getpass('Fastmail app password (hidden): ')
        cache=sqlite3.connect(u.STATE.with_suffix('.sqlite3'));stack.callback(cache.close)
        with imaplib.IMAP4_SSL(d.DEST_HOST,993,ssl_context=ssl.create_default_context(),timeout=120) as client:
            d.login_destination(client,password);password=None
            sync(client,state,report,by_folder,cache)


if __name__=='__main__':
    try:main()
    except (Exception,KeyboardInterrupt) as error:
        print(f'Catch-up paused: {error}. Rerun the same command to resume; do not restart the full uploader.',file=sys.stderr)
        sys.exit(1)
