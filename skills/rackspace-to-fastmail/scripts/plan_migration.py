#!/usr/bin/env python3
"""Read-only destination discovery and reviewable local migration plan."""
from collections import defaultdict
import fcntl
import getpass
import imaplib
import json
import os
import ssl
import sys
import download as d
import upload_fastmail as u
from reset_fastmail import personal_folders, parse_namespaces


def main():
    os.umask(0o077)
    if not sys.stdin.isatty():
        raise RuntimeError('Run locally for a hidden app-password prompt')
    d.cfg.bind()
    with (d.ROOT/'.lock').open('rb') as lock:
        fcntl.flock(lock,fcntl.LOCK_SH|fcntl.LOCK_NB)
        source,rows,hashes=u.load_plan()
        password=getpass.getpass('Fastmail app password (hidden): ')
        with imaplib.IMAP4_SSL(d.DEST_HOST,993,ssl_context=ssl.create_default_context(),timeout=120) as client:
            d.login_destination(client,password);password=None
            destinations=personal_folders(client)
            raw=d.ok(client.namespace())[0]
            personal,_,_=parse_namespaces(raw)
            if len(personal)!=1 or personal[0][:2]!=[b'',b'/']:
                raise RuntimeError('This uploader supports one empty-prefix slash personal namespace; adapt and test before proceeding')
            mapping=u.mappings(source['folders'],destinations)
            grouped=defaultdict(list)
            for folder in source['folders']:
                grouped[mapping[folder['id']]].append(folder['name'])
            counts={f['name']:len(d.snapshot(client,f)[1]) for f in destinations if f['selectable']}
        plan={
            'source_account':d.ACCOUNT,'destination_account':d.DEST_ACCOUNT,
            'source_snapshot':source['finished_utc'],'source_messages':source['total_messages'],
            'destination_personal_counts':counts,
            'folder_mapping':[{'source':f['name'],'destination':mapping[f['id']],
                               'messages':f.get('message_count',0)} for f in source['folders']],
            'folder_merges':{name:origins for name,origins in grouped.items() if len(origins)>1},
            'date_corrections':sum(r['requested_date']!=r['internaldate'] for group in rows.values() for r in group),
            'identical_extra_copies':sum(len(group)-1 for group in hashes.values()),
            'scope':'Initial uploader requires empty or explicitly disposable personal destination mail.'}
        d.write_json(d.cfg.BASE/'migration-plan.json',plan)
        print(json.dumps(plan,indent=2))
        print('No remote mail changed. Review the plan before recording configuration approvals.')


if __name__=='__main__':
    try:main()
    except (Exception,KeyboardInterrupt) as error:
        print(f'Planning stopped: {error}',file=sys.stderr)
        sys.exit(1)
