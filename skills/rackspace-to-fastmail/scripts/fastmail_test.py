#!/usr/bin/env python3
"""Upload a small, resumable date test to an isolated Fastmail folder. No deletes."""
from datetime import datetime, timezone
import fcntl
import getpass
import imaplib
import json
import os
from pathlib import Path
import re
import sqlite3
import ssl
import sys
import uuid
import download as d

STATE = Path(__file__).resolve().parent / 'fastmail-date-test.json'
STANDARD_FLAGS = {'\\Seen', '\\Answered', '\\Flagged', '\\Draft'}


def instant(value):
    return datetime.strptime(value, '%d-%b-%Y %H:%M:%S %z').astimezone(timezone.utc)


def planned_date(row):
    received = instant(row['internaldate'])
    sent = datetime.fromisoformat(row['sent_utc']) if row['sent_utc'] else None
    # Match the source server's calendar date, not an inferred UTC migration day.
    day = datetime.strptime(row['internaldate'], '%d-%b-%Y %H:%M:%S %z').date().isoformat()
    if day in d.cfg.CONFIG['repair_received_days'] and sent and sent.tzinfo and sent < received:
        return sent.strftime('%d-%b-%Y %H:%M:%S %z'), 'Approved received-day rule; original Date header'
    return row['internaldate'], 'Preserve source INTERNALDATE'


def prepare():
    with (d.ROOT / '.lock').open('rb') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        report = json.loads((d.ROOT / 'report.json').read_text())
        if report['status'] != 'verified_snapshot':
            raise RuntimeError('A completed Rackspace snapshot is required')
        db = sqlite3.connect((d.ROOT / 'inventory.sqlite3').as_uri() + '?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        try:
            if STATE.exists():
                state = json.loads(STATE.read_text())
            else:
                rows = [dict(r) for r in db.execute('SELECT * FROM messages WHERE active=1 AND size < 5000000 ORDER BY folder,CAST(uid AS INTEGER)')]
                picked = []
                hashes = set()
                def pick(predicate):
                    for row in rows:
                        if row['sha256'] not in hashes and '\\Deleted' not in row['flags'].split() and predicate(row):
                            picked.append(row.copy())
                            hashes.add(row['sha256'])
                            return
                years = sorted({r['sent_utc'][:4] for r in rows if r['sent_utc']})
                for year in years:
                    pick(lambda r, y=year: bool(r['sent_utc']) and r['sent_utc'].startswith(y)
                         and planned_date(r)[0] != r['internaldate'])
                for year in (years[:1] + years[-1:]):
                    pick(lambda r, y=year: bool(r['sent_utc']) and r['sent_utc'].startswith(y)
                         and planned_date(r)[0] == r['internaldate'])
                pick(lambda r: not r['sent_utc'])
                if not picked:
                    pick(lambda r: True)
                if not picked:
                    raise RuntimeError('No eligible real-message samples; review empty/oversized source')
                names = {f['id']: f['name'] for f in report['folders']}
                for row in picked:
                    row['target_internaldate'], row['date_reason'] = planned_date(row)
                    row['source_folder_name'] = names[row['folder']]
                    flags = row['flags'].split()
                    row['target_flags'] = sorted(set(flags) & STANDARD_FLAGS)
                    row['omitted_test_keywords'] = sorted(set(flags) - STANDARD_FLAGS)
                state = {'account': d.DEST_ACCOUNT, 'host': d.DEST_HOST,
                         'folder': 'Migration-Date-Test-' + uuid.uuid4().hex[:12],
                         'status': 'planned', 'created': False, 'messages': picked,
                         'scope': 'Dedicated test folder only. No source or existing destination mail changed.'}
                d.write_json(STATE, state)
            bodies = {}
            for row in state['messages']:
                current = db.execute('SELECT sha256 FROM messages WHERE folder=? AND validity=? AND uid=? AND active=1',
                                     (row['folder'], row['validity'], row['uid'])).fetchone()
                if not current or current['sha256'] != row['sha256']:
                    raise RuntimeError('Source snapshot changed; review the saved test plan')
                raw = (d.ROOT / row['path']).read_bytes()
                if len(raw) != row['size'] or d.sha(raw) != row['sha256']:
                    raise RuntimeError('Source message checksum mismatch')
                bodies[row['sha256']] = raw
            return state, bodies
        finally:
            db.close()


def inspect(client, state):
    d.ok(client.select(d.quote(state['folder'].encode()), readonly=True))
    validity = client.response('UIDVALIDITY')[1]
    if not validity or not validity[0]:
        raise RuntimeError('Fastmail did not return UIDVALIDITY')
    validity = validity[0].decode()
    if state.get('uidvalidity') and state['uidvalidity'] != validity:
        raise RuntimeError('Test folder UIDVALIDITY changed; refusing to reuse it')
    state['uidvalidity'] = validity
    uids = d.ok(client.uid('SEARCH', None, 'ALL'))[0].split()
    if len(uids) > len(state['messages']):
        raise RuntimeError('Unexpected extra messages in test folder')
    expected = {r['sha256']: r for r in state['messages']}
    found = {}
    for uid in uids:
        response = d.ok(client.uid('FETCH', uid, '(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[])'))
        bodies = [part for part in response if isinstance(part, tuple)]
        if len(bodies) != 1:
            raise RuntimeError('Missing test message body')
        meta, raw = bodies[0]
        actual_uid, flags, date, size = d.metadata(meta)
        checksum = d.sha(raw)
        if checksum not in expected or checksum in found:
            raise RuntimeError('Unexpected, duplicate or changed message bytes in Fastmail test folder')
        row = expected[checksum]
        observed = {'uid': actual_uid, 'sha256': checksum, 'internaldate': date, 'flags': flags}
        row['destination'] = observed
        if actual_uid != uid.decode() or len(raw) != size or size != row['size']:
            raise RuntimeError('Fastmail message UID/size mismatch')
        if instant(date) != instant(row['target_internaldate']):
            raise RuntimeError('Fastmail did not retain the requested date for source UID ' + row['uid'])
        if set(flags.split()) & STANDARD_FLAGS != set(row['target_flags']):
            raise RuntimeError('Fastmail read/starred/answered/draft flags differ')
        found[checksum] = actual_uid
    return found


def run(client, state, bodies):
    if not state['created']:
        # A prior interrupted CREATE may have succeeded. Select only our saved random folder name.
        status, _ = client.select(d.quote(state['folder'].encode()), readonly=True)
        if status != 'OK':
            d.ok(client.create(d.quote(state['folder'].encode())))
        state['created'] = True
        d.write_json(STATE, state)
    state['status'] = 'testing'
    d.write_json(STATE, state)
    try:
        found = inspect(client, state)
        for row in state['messages']:
            if row['sha256'] not in found:
                state['pending_source_sha256'] = row['sha256']
                d.write_json(STATE, state)
                flags = '(' + ' '.join(row['target_flags']) + ')'
                d.ok(client.append(d.quote(state['folder'].encode()), flags,
                                   '"' + row['target_internaldate'] + '"', bodies[row['sha256']]))
                found = inspect(client, state)
                if row['sha256'] not in found:
                    raise RuntimeError('APPEND completed but expected message was not found')
                state.pop('pending_source_sha256', None)
                d.write_json(STATE, state)
            print(f'Checked {row["source_folder_name"]} / UID {row["uid"]}: {row["target_internaldate"]}', flush=True)
        if len(found) != len(state['messages']):
            raise RuntimeError('Test folder count mismatch')
        d.ok(client.subscribe(d.quote(state['folder'].encode())))
        state['status'] = 'verified_imap_awaiting_visual_check'
        state['verified_utc'] = datetime.now(timezone.utc).isoformat()
        state.pop('error', None)
        d.write_json(STATE, state)
        print(f'VERIFIED: {len(found)} messages, exact bytes, dates and standard flags.')
        print('Open this folder in Fastmail:', state['folder'])
        print('Compare these messages with the planned dates in your display timezone.')
    except Exception as error:
        state['status'] = 'test_incomplete'
        state['error'] = str(error)
        d.write_json(STATE, state)
        raise


def main():
    os.umask(0o077)
    if not sys.stdin.isatty():
        raise RuntimeError('Run in your terminal for a private app-password prompt')
    with STATE.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        d.cfg.freeze_policy()
        state, bodies = prepare()
        print(f'Test: {len(state["messages"])} messages to {state["folder"]}. No existing mail will be deleted.')
        password = getpass.getpass('Fastmail app password (hidden): ')
        with imaplib.IMAP4_SSL(d.DEST_HOST, 993, ssl_context=ssl.create_default_context(), timeout=120) as client:
            d.login_destination(client,password)
            password = None
            run(client, state, bodies)


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print(f'Test stopped: {error}. Rerun to reconcile and resume; do not delete the test folder or state file.', file=sys.stderr)
        sys.exit(1)
