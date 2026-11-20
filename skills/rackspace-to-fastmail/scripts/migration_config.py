"""Nonsecret configuration and account binding for this isolated migration."""
import hashlib
import json
import locale
from pathlib import Path

locale.setlocale(locale.LC_TIME, 'C')
BASE = Path(__file__).resolve().parent
PATH = BASE / 'migration-config.json'
DEFAULT = {
    'source_account': 'source@example.invalid',
    'destination_account': 'destination@example.invalid',
    'source_host': 'secure.emailsrvr.com',
    'destination_host': 'imap.fastmail.com',
    'replace_destination_authorized': False,
    'background_import_disabled': False,
    'date_policy_reviewed': False,
    'folder_mapping_reviewed': False,
    'visual_date_test_passed': False,
    'cutover_complete': False,
    'repair_received_days': [],
    'duplicate_date_policy': 'stop',
    'duplicate_flag_policy': 'stop'
}
CONFIG = {**DEFAULT, **(json.loads(PATH.read_text()) if PATH.exists() else {})}

def bind():
    if not PATH.exists():
        raise RuntimeError('Create migration-config.json from the embedded example first')
    identity = {k: CONFIG[k] for k in ('source_account','destination_account','source_host','destination_host')}
    for key, value in identity.items():
        if not isinstance(value, str) or not value or any(c in value for c in '\r\n') or value.endswith('.invalid'):
            raise RuntimeError('Configure real account/server identities before connecting')
    if identity['source_host'].lower() == identity['destination_host'].lower():
        raise RuntimeError('Source and destination servers must differ')
    marker = BASE / 'account-binding.json'
    # Exclusive creation prevents silently rebinding a populated workspace.
    try:
        with marker.open('x') as stream:
            json.dump(identity, stream, sort_keys=True)
    except FileExistsError:
        if json.loads(marker.read_text()) != identity:
            raise RuntimeError('Account identity changed; use a new isolated workspace')

def destination_ready():
    bind()
    if CONFIG['replace_destination_authorized'] is not True or CONFIG['background_import_disabled'] is not True:
        raise RuntimeError('Replacement requires recorded user authorization and disabled background import')

def freeze_policy():
    if CONFIG['date_policy_reviewed'] is not True:
        raise RuntimeError('Review the date audit and approve the date policy first')
    keys = ('repair_received_days','duplicate_date_policy','duplicate_flag_policy')
    policy = {k: CONFIG[k] for k in keys}
    marker = BASE / 'frozen-policy.json'
    try:
        with marker.open('x') as stream:
            json.dump(policy, stream, sort_keys=True)
    except FileExistsError:
        if json.loads(marker.read_text()) != policy:
            raise RuntimeError('Date/duplicate policy changed after testing; reconcile existing state before continuing')
