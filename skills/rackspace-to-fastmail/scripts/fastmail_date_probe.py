#!/usr/bin/env python3
"""Test APPEND dates and duplicate behavior using synthetic mail only."""
from pathlib import Path
import fcntl
import getpass
import imaplib
import json
import os
import ssl
import sys
import uuid
import download as d
from fastmail_test import instant

STATE=Path(__file__).resolve().parent/'fastmail-date-probe.json'


def synthetic(token):
    return (f'From: Migration Date Probe <probe@example.invalid>\r\n'
            f'To: Migration Date Probe <probe@example.invalid>\r\n'
            f'Message-ID: <{token}@migration-probe.invalid>\r\n'
            'Date: Thu, 12 Oct 2006 19:02:40 -0700\r\n'
            'Subject: Synthetic migration date probe - not historical mail\r\n'
            'MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n'
            f'Synthetic fixture {token}. No private email content.\r\n').encode()


def check(client, folder, raw):
    d.ok(client.select(d.quote(folder.encode()),readonly=True))
    uids=d.ok(client.uid('SEARCH',None,'ALL'))[0].split()
    if not uids:
        return None
    if len(uids)!=1:
        raise RuntimeError('Unexpected message count in synthetic probe folder')
    result=d.ok(client.uid('FETCH',uids[0],'(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[])'))
    parts=[x for x in result if isinstance(x,tuple)]
    if len(parts)!=1:
        raise RuntimeError('Missing probe body')
    uid,flags,date,size=d.metadata(parts[0][0])
    if parts[0][1]!=raw or size!=len(raw) or uid!=uids[0].decode():
        raise RuntimeError('Synthetic probe content mismatch')
    return {'uid':uid,'internaldate':date,'sha256':d.sha(raw)}


def run(client,state):
    if state.get('after_delete'):
        raise RuntimeError('Post-deletion probe already started; use --after-delete to resume')
    raw=synthetic(state['token'])
    for probe in state['probes']:
        status,_=client.select(d.quote(probe['folder'].encode()),readonly=True)
        if status!='OK':
            d.ok(client.create(d.quote(probe['folder'].encode())))
        result=check(client,probe['folder'],raw)
        if result is None:
            d.ok(client.append(d.quote(probe['folder'].encode()),'(\\Seen)',
                               '"'+probe['requested_internaldate']+'"',raw))
            result=check(client,probe['folder'],raw)
        if result is None:
            raise RuntimeError('Probe missing after APPEND')
        probe['observed']=result
        probe['requested_date_retained']=instant(result['internaldate'])==instant(probe['requested_internaldate'])
        d.ok(client.subscribe(d.quote(probe['folder'].encode())))
        d.write_json(STATE,state)
        print(probe['label'],flush=True)
        print('  Requested:',probe['requested_internaldate'],flush=True)
        print('  Returned: ',result['internaldate'],flush=True)
    first,second=state['probes']
    same=instant(first['observed']['internaldate'])==instant(second['observed']['internaldate'])
    if first['requested_date_retained'] and same:
        state['finding']='New message honors APPEND date; identical copy reused the first date across folders.'
    elif all(p['requested_date_retained'] for p in state['probes']):
        state['finding']='Both APPEND dates retained. Existing-import behavior needs further investigation.'
    else:
        state['finding']='APPEND behavior differs from requested dates; inspect probe results before proceeding.'
    state['status']='complete'
    d.write_json(STATE,state)
    print(state['finding'])
    print('No existing Fastmail or Rackspace messages changed.')


def after_delete(client,state):
    if state.get('status')!='complete':
        raise RuntimeError('Complete the initial synthetic probe first')
    capabilities={value.decode('ascii').upper() if isinstance(value,bytes) else value.upper()
                  for value in client.capabilities}
    if 'UIDPLUS' not in capabilities:
        raise RuntimeError('UIDPLUS is required for targeted expunge')
    expected=['Migration-Probe-'+state['token'][:12]+'-'+suffix for suffix in ('A','B')]
    if [p['folder'] for p in state['probes']]!=expected:
        raise RuntimeError('Unexpected synthetic folder names; refusing deletion')
    raw=synthetic(state['token'])
    progress=state.setdefault('after_delete',{'phase':'clearing','requested_internaldate':'13-Oct-2008 02:02:40 +0000'})
    d.write_json(STATE,state)
    if progress['phase']=='clearing':
        # Check both folders before changing either; only exact synthetic bytes qualify.
        for probe in state['probes']:
            check(client,probe['folder'],raw)
        for probe in state['probes']:
            result=check(client,probe['folder'],raw)
            if result:
                d.ok(client.select(d.quote(probe['folder'].encode()),readonly=False))
                d.ok(client.uid('STORE',result['uid'],'+FLAGS.SILENT','(\\Deleted)'))
                d.ok(client.uid('EXPUNGE',result['uid']))
            if check(client,probe['folder'],raw) is not None:
                raise RuntimeError('Synthetic message still present after expunge')
        progress['phase']='cleared'
        d.write_json(STATE,state)
    folder=state['probes'][0]['folder']
    result=check(client,folder,raw)
    if result is None:
        d.ok(client.append(d.quote(folder.encode()),'(\\Seen)',
                           '"'+progress['requested_internaldate']+'"',raw))
        result=check(client,folder,raw)
    if result is None:
        raise RuntimeError('Synthetic message missing after reupload')
    progress['observed']=result
    progress['phase']='complete'
    progress['requested_date_retained']=instant(result['internaldate'])==instant(progress['requested_internaldate'])
    d.write_json(STATE,state)
    print('C: same synthetic message after removing both prior copies')
    print('  Requested:',progress['requested_internaldate'])
    print('  Returned: ',result['internaldate'])
    if progress['requested_date_retained']:
        print('PASS: deleting both copies allowed the new date on reupload.')
    else:
        print('Old date persists after deletion; a simple purge/reupload is not yet proven to fix historical mail.')
    print('Only the two synthetic probe copies were removed. Historical mail was untouched.')


def main():
    os.umask(0o077)
    if sys.argv[1:] not in ([],['--after-delete']):
        raise RuntimeError('Usage: python3 migration/fastmail_date_probe.py [--after-delete]')
    if not sys.stdin.isatty():
        raise RuntimeError('Run in your terminal for a hidden app-password prompt')
    with STATE.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if STATE.exists():
            state=json.loads(STATE.read_text())
        else:
            if '--after-delete' in sys.argv:
                raise RuntimeError('Initial probe state missing')
            token=uuid.uuid4().hex
            state={'token':token,'status':'planned','probes':[
                {'label':'A: brand-new synthetic message','folder':'Migration-Probe-'+token[:12]+'-A','requested_internaldate':'13-Oct-2007 02:02:40 +0000'},
                {'label':'B: identical message, different requested date','folder':'Migration-Probe-'+token[:12]+'-B','requested_internaldate':'13-Oct-2008 02:02:40 +0000'}]}
            d.write_json(STATE,state)
        password=getpass.getpass('Fastmail app password (hidden): ')
        with imaplib.IMAP4_SSL(d.DEST_HOST,993,ssl_context=ssl.create_default_context(),timeout=120) as client:
            d.login_destination(client,password)
            password=None
            if '--after-delete' in sys.argv:
                after_delete(client,state)
            else:
                run(client,state)


if __name__=='__main__':
    try:
        main()
    except (Exception,KeyboardInterrupt) as error:
        print(f'Probe stopped: {error}. Rerun to resume.',file=sys.stderr)
        sys.exit(1)
