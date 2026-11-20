#!/usr/bin/env python3
"""Export the checkpoint's active original messages into one MBOX per folder."""
import fcntl
import hashlib
import json
import mailbox
import os
from pathlib import Path
import re
import sqlite3
import sys

ROOT=Path(__file__).resolve().parent/'source-mail'


def export(root,destination):
    report=json.loads((root/'report.json').read_text())
    if report['status']!='verified_snapshot':raise RuntimeError('Verified source snapshot required')
    destination.mkdir(parents=True,exist_ok=False)
    db=sqlite3.connect((root/'inventory.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)
    mapping=[]
    try:
        for folder in report['folders']:
            if not folder['selectable']:continue
            filename=(re.sub(r'[^A-Za-z0-9._-]','_',folder['name'])[:80] or 'folder')+'-'+folder['id'][:12]+'.mbox'
            path=destination/filename
            partial=destination/(filename+'.partial')
            box=mailbox.mbox(str(partial),create=True)
            count=0
            try:
                for relative,checksum,size in db.execute('SELECT path,sha256,size FROM messages WHERE active=1 AND folder=? ORDER BY CAST(uid AS INTEGER)',(folder['id'],)):
                    source=(root/relative).resolve()
                    if not source.is_relative_to(root.resolve()):raise RuntimeError('Invalid source path')
                    raw=source.read_bytes()
                    if len(raw)!=size or hashlib.sha256(raw).hexdigest()!=checksum:raise RuntimeError('Original checksum mismatch')
                    box.add(raw.replace(b'\r\n',b'\n'));count+=1
                box.flush()
            finally:box.close()
            if count!=folder['message_count']:raise RuntimeError('Source folder count mismatch')
            check=mailbox.mbox(str(partial),create=False)
            try:
                if len(check)!=count:raise RuntimeError('Export count mismatch')
            finally:check.close()
            partial.rename(path)
            mapping.append({'source_folder':folder['name'],'mbox':filename,'message_count':count})
            print(f'{folder["name"]}: {count:,} messages',flush=True)
        (destination/'folder-map.json').write_text(json.dumps(mapping,indent=2)+'\n')
    finally:db.close()


if __name__=='__main__':
    os.umask(0o077)
    if len(sys.argv)!=2:raise SystemExit('Usage: python3 migration/export_mbox.py /path/to/new-export-folder')
    lockpath=ROOT/'.lock'
    # Restored checkpoints omit transient lock files; live working copies have one.
    if lockpath.exists():
        with lockpath.open('rb') as lock:
            fcntl.flock(lock,fcntl.LOCK_SH|fcntl.LOCK_NB)
            export(ROOT,Path(sys.argv[1]))
    else:export(ROOT,Path(sys.argv[1]))
