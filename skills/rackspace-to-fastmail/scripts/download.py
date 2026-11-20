#!/usr/bin/env python3
"""Read-only, resumable Rackspace snapshot and date audit. No folders excluded."""
import base64
from collections import Counter
from datetime import datetime, timezone
from email import policy
from email.parser import BytesHeaderParser
from email.utils import parsedate_to_datetime
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

import migration_config as cfg
ACCOUNT = cfg.CONFIG['source_account']
DEST_ACCOUNT = cfg.CONFIG['destination_account']
DEST_HOST = cfg.CONFIG['destination_host']
HOST = cfg.CONFIG['source_host']
ROOT = Path(__file__).resolve().parent / 'source-mail'


def login_destination(client, password):
    cfg.bind()
    ok(client.login(DEST_ACCOUNT, password))
    caps = ok(client.capability())
    client.capabilities = tuple(word for line in caps if line for word in line.split())
    names = {v.decode().upper() if isinstance(v, bytes) else v.upper() for v in client.capabilities}
    if 'UIDPLUS' not in names:
        raise RuntimeError('UIDPLUS required for this implementation; no unrestricted expunge fallback')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def atomic(path, data):
    temporary = path.with_name(path.name + '.partial')
    with temporary.open('wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def write_json(path, value):
    atomic(path, (json.dumps(value, indent=2, ensure_ascii=True) + '\n').encode())


def ok(result):
    status, data = result
    if status != 'OK':
        raise RuntimeError('IMAP operation failed: ' + status)
    return data


def label(name):
    def decode(match):
        token = match[1].replace(',', '/')
        return base64.b64decode(token + '=' * (-len(token) % 4)).decode('utf-16-be') if token else '&'
    return re.sub(r'&([^-]*)-', decode, name.decode('ascii'))


def quote(name):
    return b'"' + name.replace(b'\\', b'\\\\').replace(b'"', b'\\"') + b'"'


def list_folders(client):
    folders = []
    for entry in ok(client.list()):
        if entry is None:
            continue
        raw = entry[0] if isinstance(entry, tuple) else entry
        match = re.fullmatch(rb'\(([^)]*)\) (NIL|"(?:[^"\\]|\\.)*") (.+)', raw)
        if not match:
            raise RuntimeError('Unsupported folder listing; refusing to skip a folder')
        flags, separator, name = match.groups()
        if name.startswith(b'{'):
            if not isinstance(entry, tuple):
                raise RuntimeError('Missing literal folder name')
            name = entry[1]
        elif name.startswith(b'"') and name.endswith(b'"'):
            name = re.sub(rb'\\(.)', rb'\1', name[1:-1])
        separator = None if separator == b'NIL' else re.sub(rb'\\(.)', rb'\1', separator[1:-1]).decode('ascii')
        folders.append({'id': sha(name), 'name': label(name), 'wire_name': base64.b64encode(name).decode(),
                        'delimiter': separator, 'flags': flags.decode('ascii'),
                        'selectable': b'\\noselect' not in flags.lower().split()})
    if not folders or len({f['id'] for f in folders}) != len(folders):
        raise RuntimeError('Empty or duplicate folder listing')
    return folders


def folder_signature(folders):
    """Compare structure, not LIST ordering or volatile unread/marked flags."""
    return {(f['wire_name'], f['delimiter'], f['selectable']) for f in folders}


def snapshot(client, folder):
    ok(client.select(quote(base64.b64decode(folder['wire_name'])), readonly=True))
    validity = client.response('UIDVALIDITY')[1]
    if not validity or not validity[0] or not validity[0].isdigit():
        raise RuntimeError('Missing UIDVALIDITY')
    result = ok(client.uid('SEARCH', None, 'ALL'))
    uids = result[0].split() if result and result[0] else []
    if any(not uid.isdigit() for uid in uids) or len(set(uids)) != len(uids):
        raise RuntimeError('Invalid UID listing')
    return validity[0].decode(), [uid.decode() for uid in uids]


def metadata(line):
    uid = re.search(rb'\bUID (\d+)\b', line)
    flags = re.search(rb'\bFLAGS \(([^)]*)\)', line)
    date = re.search(rb'\bINTERNALDATE "([^"]+)"', line)
    size = re.search(rb'\bRFC822.SIZE (\d+)\b', line)
    if not all((uid, flags, date, size)):
        raise RuntimeError('Incomplete IMAP message metadata')
    return uid[1].decode(), flags[1].decode(), date[1].decode(), int(size[1])


def header_fields(raw):
    end = re.search(br'\r?\n\r?\n', raw)
    header = BytesHeaderParser(policy=policy.default).parsebytes(raw[:end.end()] if end else raw)
    original = str(header.get('Date', ''))
    sent = None
    try:
        date = parsedate_to_datetime(original)
        if date.tzinfo is not None:
            sent = date.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        pass
    return original, sent, str(header.get('Subject', '')), str(header.get('Message-ID', ''))


def open_db(path):
    db = sqlite3.connect(path)
    db.execute('PRAGMA synchronous=FULL')
    db.execute('''CREATE TABLE IF NOT EXISTS messages (
        folder TEXT, validity TEXT, uid TEXT, path TEXT, sha256 TEXT, size INTEGER,
        flags TEXT, internaldate TEXT, date_header TEXT, sent_utc TEXT, subject TEXT,
        message_id TEXT, active INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(folder, validity, uid))''')
    return db


def date_audit(db, folders):
    names = {f['id']: f['name'] for f in folders}
    years = Counter()
    shifts = Counter()
    examples = []
    missing = 0
    suspicious = 0
    for folder, uid, sent, received, original, subject in db.execute(
            'SELECT folder,uid,sent_utc,internaldate,date_header,subject FROM messages WHERE active=1'):
        if not sent:
            missing += 1
            continue
        sent_dt = datetime.fromisoformat(sent)
        years[str(sent_dt.year)] += 1
        try:
            received_dt = datetime.strptime(received, '%d-%b-%Y %H:%M:%S %z')
        except ValueError:
            missing += 1
            continue
        delta = (received_dt - sent_dt).total_seconds() / 86400
        if abs(delta) > 7:
            suspicious += 1
            shifts[f'{sent_dt.year} -> {received_dt.year}'] += 1
            if len(examples) < 40:
                examples.append({'folder': names[folder], 'uid': uid, 'subject': subject,
                                 'date_header': original, 'rackspace_internaldate': received,
                                 'difference_days': round(delta, 2)})
    return {'basis': 'Original Date header compared with Rackspace INTERNALDATE; no dates changed',
            'messages_by_sent_year': dict(sorted(years.items())),
            'date_gaps_over_7_days': suspicious, 'missing_or_unusable_dates': missing,
            'sent_year_to_received_year': dict(shifts.most_common()), 'examples': examples}


def download(client, root):
    folders = list_folders(client)
    report = {'account': ACCOUNT, 'host': HOST, 'status': 'incomplete',
              'started_utc': datetime.now(timezone.utc).isoformat(), 'folders': folders,
              'policy': 'All selectable folders included; original bytes and metadata preserved'}
    write_json(root / 'report.json', report)
    db = open_db(root / 'inventory.sqlite3')
    try:
        db.execute('UPDATE messages SET active=0')
        db.commit()
        snapshots = {}
        for folder in folders:
            if not folder['selectable']:
                continue
            validity, uids = snapshot(client, folder)
            snapshots[folder['id']] = (validity, uids)
            folder.update(uidvalidity=validity, message_count=len(uids))
            print(f'{folder["name"]!r}: {len(uids):,} messages', flush=True)
            directory = root / 'messages' / folder['id'] / validity
            directory.mkdir(parents=True, exist_ok=True)
            for start in range(0, len(uids), 200):
                batch = uids[start:start + 200]
                responses = ok(client.uid('FETCH', ','.join(batch), '(UID FLAGS INTERNALDATE RFC822.SIZE)'))
                remote = {}
                for line in responses:
                    if isinstance(line, bytes) and b'UID ' in line:
                        uid, flags, received, size = metadata(line)
                        remote[uid] = (flags, received, size)
                if set(remote) != set(batch):
                    raise RuntimeError('Mailbox changed or metadata missing. Rerun to resume.')
                for uid in batch:
                    flags, received, size = remote[uid]
                    path = directory / (uid + '.eml')
                    saved = db.execute('SELECT sha256,size FROM messages WHERE folder=? AND validity=? AND uid=?',
                                       (folder['id'], validity, uid)).fetchone()
                    raw = path.read_bytes() if saved and path.exists() else None
                    if raw is None or len(raw) != size or size != saved[1] or sha(raw) != saved[0]:
                        fetched = ok(client.uid('FETCH', uid, '(UID RFC822.SIZE BODY.PEEK[])'))
                        bodies = [part for part in fetched if isinstance(part, tuple)]
                        if len(bodies) != 1:
                            raise RuntimeError('Missing message body')
                        meta, raw = bodies[0]
                        found_uid = re.search(rb'\bUID (\d+)\b', meta)
                        found_size = re.search(rb'RFC822.SIZE (\d+)\b', meta)
                        if not found_uid or found_uid[1].decode() != uid or not found_size or int(found_size[1]) != size or len(raw) != size:
                            raise RuntimeError('Downloaded message size/UID mismatch')
                        atomic(path, raw)
                    date, sent, subject, message_id = header_fields(raw)
                    db.execute('INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)',
                               (folder['id'], validity, uid, str(path.relative_to(root)), sha(raw), size,
                                flags, received, date, sent, subject, message_id))
                db.commit()
                print(f'  {min(start + 200, len(uids)):,}/{len(uids):,} downloaded and checked', flush=True)
            write_json(root / 'report.json', report)
        final_folders = list_folders(client)
        if folder_signature(final_folders) != folder_signature(folders):
            report['folder_structure_change'] = {
                'before': [{'name': f['name'], 'delimiter': f['delimiter'], 'selectable': f['selectable']} for f in folders],
                'after': [{'name': f['name'], 'delimiter': f['delimiter'], 'selectable': f['selectable']} for f in final_folders]}
            write_json(root / 'report.json', report)
            raise RuntimeError('Folder list changed during download. Rerun to update.')
        for folder in folders:
            if folder['selectable'] and snapshot(client, folder) != snapshots[folder['id']]:
                raise RuntimeError('New or removed mail detected. Rerun to update the snapshot.')
        count, size = db.execute('SELECT COUNT(*),COALESCE(SUM(size),0) FROM messages WHERE active=1').fetchone()
        assert count == sum(f.get('message_count', 0) for f in folders)
        print('Verifying every saved original message...', flush=True)
        for relative, checksum, length in db.execute('SELECT path,sha256,size FROM messages WHERE active=1'):
            path = root / relative
            if path.stat().st_size != length:
                raise RuntimeError('Saved message size mismatch')
            with path.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != checksum:
                    raise RuntimeError('Saved message checksum mismatch')
        audit = date_audit(db, folders)
        write_json(root / 'date-audit.json', audit)
        report.update(status='verified_snapshot', total_messages=count, original_bytes=size,
                      finished_utc=datetime.now(timezone.utc).isoformat())
        write_json(root / 'report.json', report)
        print(f'VERIFIED: {count:,} messages; {size:,} bytes. Date audit: {root / "date-audit.json"}', flush=True)
        print(f'Date gaps over 7 days: {audit["date_gaps_over_7_days"]:,}. No messages or dates changed on Rackspace.')
    finally:
        db.close()


def main():
    os.umask(0o077)
    if not sys.stdin.isatty():
        raise RuntimeError('Run in your terminal for a private password prompt')
    cfg.bind()
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        password = getpass.getpass(f'Rackspace password for {ACCOUNT} (hidden): ')
        with imaplib.IMAP4_SSL(HOST, 993, ssl_context=ssl.create_default_context(), timeout=120) as client:
            ok(client.login(ACCOUNT, password))
            password = None
            download(client, ROOT)


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print(f'Download incomplete: {error}. Rerun the same command to resume.', file=sys.stderr)
        sys.exit(1)
