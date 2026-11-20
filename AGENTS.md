# Working on rs2fm

This repository publishes an agent skill and reusable migration tools. It is not
a live migration workspace. Keep real email, credentials, inventories, logs,
account-specific configuration, and encrypted backups outside the repository.

## Source of truth

- `skills/rackspace-to-fastmail/SKILL.md`: short agent entrypoint and workflow routing.
- `skills/rackspace-to-fastmail/references/migration-guide.md`: detailed procedures,
  limitations, recovery guidance and provider references.
- `skills/rackspace-to-fastmail/scripts/`: maintained Python implementation.
- `skills/rackspace-to-fastmail/scripts/tests/`: synthetic tests.
- `skills/rackspace-to-fastmail/assets/`: configuration example and restore guide.

The split files are canonical. Do not maintain a second embedded copy of the
Python code in Markdown. Keep documentation consistent with implementation.

## Development

Use synthetic fixtures only. Do not connect to live providers or run destructive
migration commands merely to test repository changes. Read the skill workflow
before changing migration behavior. Do not copy a user's date-repair rules,
folder names, identities, or approval settings into defaults.

Preserve read-only source access, exact-byte verification, resumable state,
UIDVALIDITY-aware identity, targeted deletion, and the distinction between
pre-cutover reconciliation and post-cutover additive sync. Keep replacement
and date-repair defaults disabled. Don't weaken checks to pass a test; explain
unsupported behavior and add a representative fixture.

Do not infer DNS changes, mailbox deletion, cleanup, publication, or cancellation
from a code/documentation task. Existing explicit user authorization still applies
within its stated scope.

## Verification

Run `python3 tools/check_skill.py` from the repository root. It creates a temporary
workspace using the same helper readers use, compiles the Python files, and runs
the synthetic suite including real local GnuPG roundtrips. It does not access live
mail. Python 3.11+ and `gpg`/`gpgconf` are required. Add behavior-focused tests for
changes to parsing, date policy, resume logic, deletion, or verification.

Inspect changes for personal data and secrets before publication. Test results
establish local behavior, not live-provider compatibility or successful cutover.
