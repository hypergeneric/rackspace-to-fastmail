# Rackspace → Fastmail

An agent skill and Python toolkit for moving a historical Rackspace Email mailbox
to Fastmail—with date auditing, nested folders, verified uploads, resumable
catch-up, and an encrypted archive you can read later.

It grew out of a migration where old messages displayed the date of a previous
provider migration. Supplying the original date on upload wasn't enough: an
existing identical Fastmail copy could retain the old timestamp. The toolkit
includes synthetic probes to diagnose that behavior before touching historical mail.

## What it does

- Downloads a verified local snapshot without changing Rackspace mail.
- Audits original message dates against IMAP received dates; repairs approved cases only.
- Previews nested-folder mappings and special-folder collisions.
- Tests Fastmail date handling with synthetic and representative original messages.
- Replaces an explicitly disposable import while excluding shared namespaces.
- Uploads with byte, date, flag and count verification, and resumes interrupted transfers.
- Preserves duplicate message membership rather than silently deduplicating.
- Reconciles changes before cutover and collects late arrivals afterward.
- Creates password-encrypted checkpoints and verifies every file by decrypting the archive.
- Exports MBOX files for later reading while retaining canonical raw EML files.

**Initial upload requires an empty or explicitly disposable personal Fastmail
mailbox.** This is not an automatic merge into an existing destination that must
be retained. The guide explains the boundaries and when adaptation is needed.

## Start with the skill

Give your agent [SKILL.md](skills/rackspace-to-fastmail/SKILL.md), with its sibling
directories available, and ask:

> Use this skill to help migrate my Rackspace mailbox to Fastmail. Audit the dates
> and folders first, keep Rackspace unchanged, and walk me through cutover and an
> encrypted checkpoint.

The [migration guide](skills/rackspace-to-fastmail/references/migration-guide.md)
contains the detailed sequence, recovery procedures and provider documentation links.
Download or clone this repository, or copy the entire `skills/rackspace-to-fastmail/`
directory rather than just its entrypoint.

## Prepare a private workspace

Requirements: **Python 3.11+**, Linux/macOS or a suitable POSIX environment, and
**GnuPG** (`gpg` and `gpgconf`). Runtime scripts use the Python standard library.

From the repository root:

```sh
python3 skills/rackspace-to-fastmail/scripts/create_workspace.py /path/to/new-private-workspace
cd /path/to/new-private-workspace
python3 -m unittest discover -s tests -v
```

Choose a new directory **outside this repository**. The helper copies the toolkit,
tests, default configuration and restore guide without contacting either provider.
Then follow the migration guide and configure account identities. Enter passwords
only in the scripts' private terminal prompts. No credentials belong in the repo.

All replacement approvals default to false. Date repair defaults to preserving
source received dates. Do not run the migration commands as an unattended chain:
review the plan and results at the transitions described in the guide.

## Repository layout

```text
rs2fm/
├── README.md
├── AGENTS.md
├── tools/
│   └── check_skill.py
└── skills/
    └── rackspace-to-fastmail/
        ├── SKILL.md
        ├── references/
        │   └── migration-guide.md
        ├── assets/
        │   ├── migration-config.example.json
        │   └── CHECKPOINT-RESTORE.txt
        └── scripts/
            ├── create_workspace.py
            ├── migration_config.py
            ├── download.py
            ├── plan_migration.py
            ├── fastmail_date_probe.py
            ├── fastmail_test.py
            ├── reset_fastmail.py
            ├── upload_fastmail.py
            ├── catch_up.py
            ├── package_checkpoint.py
            ├── export_mbox.py
            └── tests/
```

## Development and verification

```sh
python3 tools/check_skill.py
```

This checks workspace creation, compiles the distributed scripts and runs the
synthetic suite in a temporary directory. Tests include interrupted uploads,
duplicate handling, shared-namespace protection, configuration safeguards,
encrypted archive verification, and attachment recovery. A GPG wrong-password
message is expected during its rejection test.

See [AGENTS.md](AGENTS.md) for contributor instructions. Use synthetic fixtures;
never add private mail, account state or credentials to a contribution.

## Limits

The generalized toolkit is tested with synthetic fixtures; those tests do not
prove compatibility with every mailbox layout or future provider behavior.
The uploader targets Fastmail's empty-prefix, slash-delimited personal namespace
and stops on unsupported layouts. Review folder collisions, duplicate policies
and date rules before migration.

DNS cutover is guided, not automated. Contacts, calendars, filters, cancellation,
spam cleanup and cross-account archival merging are separate tasks. The encrypted
checkpoint covers the local source snapshot and migration records, not subsequent
mail received directly by Fastmail.
