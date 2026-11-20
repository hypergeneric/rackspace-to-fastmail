#!/usr/bin/env python3
"""Stream and verify an encrypted migration checkpoint, preserving the working copy."""
from contextlib import contextmanager,ExitStack
from datetime import datetime,timezone
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import migration_config as cfg

ROOT=Path(__file__).resolve().parent
TARGET=ROOT.parent/'mail-checkpoint.tar.gz.gpg'


@contextmanager
def password_fd(password):
    read,write=os.pipe()
    try:
        os.write(write,password.encode()+b'\n');os.close(write);write=None
        yield read
    finally:
        os.close(read)
        if write is not None:os.close(write)


def gpg(home,fd):
    return ['gpg','--no-options','--homedir',str(home),'--batch','--no-tty','--pinentry-mode','loopback',
            '--no-symkey-cache','--passphrase-fd',str(fd)]


def files(root):
    result={}
    for path in sorted(root.rglob('*')):
        relative=path.relative_to(root)
        if '__pycache__' in relative.parts or path.name.endswith(('.lock','.partial')):continue
        if path.is_symlink():raise RuntimeError('Unexpected symlink in checkpoint')
        if path.is_file():
            if path.name.endswith(('-journal','-wal','-shm')):raise RuntimeError('Live SQLite journal present; stop concurrent work first')
            result[str(Path(root.name)/relative)]=path
    return result


def verify_source(root):
    mail=root/'source-mail'
    report=json.loads((mail/'report.json').read_text())
    upload=json.loads((root/'fastmail-upload.json').read_text())
    catchup=json.loads((root/'catch-up.json').read_text())
    if report['status']!='verified_snapshot' or upload['status'] not in ('verified_snapshot_uploaded','verified_incremental_sync') or catchup['phase']!='complete':
        raise RuntimeError('Migration/download must be in a verified completed state')
    if report['finished_utc']!=upload['source_finished_utc']:raise RuntimeError('Source and upload snapshots differ')
    db=sqlite3.connect((mail/'inventory.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)
    active=0;all_files=0
    try:
        for relative,size,checksum,present in db.execute('SELECT path,size,sha256,active FROM messages'):
            path=(mail/relative).resolve()
            if not path.is_relative_to(mail.resolve()) or path.stat().st_size!=size:raise RuntimeError('Original message path/size mismatch')
            with path.open('rb') as stream:
                if hashlib.file_digest(stream,'sha256').hexdigest()!=checksum:raise RuntimeError('Original message checksum mismatch')
            active+=bool(present);all_files+=1
    finally:db.close()
    if active!=report['total_messages']:raise RuntimeError('Snapshot count mismatch')
    return {'account':cfg.CONFIG['source_account'],'created_utc':datetime.now(timezone.utc).isoformat(),
            'scope':'Local Rackspace snapshot plus migration records; not a complete post-cutover Fastmail backup',
            'source_snapshot_utc':report['finished_utc'],'source_active_messages':active,
            'stored_original_files':all_files,'source_original_bytes':report['original_bytes'],
            'fastmail_verification_utc':upload.get('verified_utc'),'last_sync_mode':upload.get('last_sync_mode'),
            'last_catchup_new_source_messages':catchup.get('new_source_messages'),
            'working_copy_retained':True}


def encrypt(paths,target,password,home):
    with password_fd(password) as fd:
        proc=subprocess.Popen(gpg(home,fd)+['--symmetric','--cipher-algo','AES256','--s2k-mode','3',
            '--s2k-count','65011712','--compress-algo','none','--output',str(target)],stdin=subprocess.PIPE,pass_fds=(fd,))
        try:
            with tarfile.open(fileobj=proc.stdin,mode='w|gz',format=tarfile.PAX_FORMAT,dereference=True) as archive:
                for name,path in paths.items():archive.add(path,arcname=name,recursive=False)
            proc.stdin.close()
            if proc.wait()!=0:raise RuntimeError('GPG encryption failed')
        finally:
            if not proc.stdin.closed:proc.stdin.close()
            if proc.poll() is None:proc.kill()
            proc.wait()


def verify(paths,target,password,home):
    seen=set()
    with password_fd(password) as fd:
        proc=subprocess.Popen(gpg(home,fd)+['--decrypt',str(target)],stdout=subprocess.PIPE,pass_fds=(fd,))
        try:
            with tarfile.open(fileobj=proc.stdout,mode='r|gz') as archive:
                for member in archive:
                    if not member.isfile() or member.name not in paths or member.name in seen:
                        raise RuntimeError('Unexpected archive member')
                    path=paths[member.name]
                    if member.size!=path.stat().st_size:raise RuntimeError('Checkpoint size mismatch')
                    with path.open('rb') as source:
                        if hashlib.file_digest(archive.extractfile(member),'sha256').digest()!=hashlib.file_digest(source,'sha256').digest():
                            raise RuntimeError('Checkpoint content mismatch')
                    seen.add(member.name)
            while proc.stdout.read(1024*1024):pass
            if proc.wait()!=0:raise RuntimeError('Decryption/integrity verification failed')
            if seen!=set(paths):raise RuntimeError('Missing checkpoint files')
        finally:
            proc.stdout.close()
            if proc.poll() is None:proc.kill()
            proc.wait()


def main():
    os.umask(0o077)
    if not sys.stdin.isatty():raise RuntimeError('Run in your terminal for hidden password prompts')
    if TARGET.exists():raise RuntimeError('Checkpoint already exists; it will not be overwritten')
    password=getpass.getpass('Archive password from your password manager (hidden): ')
    if not password or any(c in password for c in '\r\n') or len(password.encode())>1024:raise RuntimeError('Invalid password')
    if password!=getpass.getpass('Confirm archive password (hidden): '):raise RuntimeError('Passwords do not match')
    with ExitStack() as stack:
        for path in [ROOT/'fastmail-upload.lock',ROOT/'source-mail'/'.lock',ROOT/'fastmail-date-test.lock',ROOT/'fastmail-reset.lock',ROOT/'fastmail-date-probe.lock']:
            lock=stack.enter_context(path.open('a'))
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        print('Verifying local originals...',flush=True)
        summary=verify_source(ROOT)
        (ROOT/'CHECKPOINT.json').write_text(json.dumps(summary,indent=2)+'\n')
        paths=files(ROOT)
        partial=TARGET.with_name(TARGET.name+'.partial')
        partial.unlink(missing_ok=True)
        with tempfile.TemporaryDirectory(prefix='mail-checkpoint-gpg-') as tmp:
            home=Path(tmp)
            try:
                print(f'Compressing and encrypting {len(paths):,} files; no plaintext tarball is created...',flush=True)
                encrypt(paths,partial,password,home)
                print('Decrypting and comparing every archived file...',flush=True)
                verify(paths,partial,password,home)
                if set(files(ROOT))!=set(paths):raise RuntimeError('Checkpoint file set changed during packaging')
            finally:subprocess.run(['gpgconf','--homedir',str(home),'--kill','gpg-agent'],check=False)
        partial.rename(TARGET)
        with TARGET.open('rb') as stream:checksum=hashlib.file_digest(stream,'sha256').hexdigest()
        TARGET.with_name(TARGET.name+'.sha256').write_text(checksum+'  '+TARGET.name+'\n')
        print(f'CHECKPOINT VERIFIED: {summary["source_active_messages"]:,} active messages; {TARGET.stat().st_size:,} encrypted bytes.')
        print(TARGET)
        print('Working copy retained. Copy the encrypted file and its .sha256 file to long-term storage.')


if __name__=='__main__':
    try:main()
    except (Exception,KeyboardInterrupt) as error:
        print(f'Packaging stopped: {error}. Working mail and other encrypted archives are unchanged.',file=sys.stderr)
        sys.exit(1)
