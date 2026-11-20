#!/usr/bin/env python3
"""Copy this skill's toolkit to a new private directory without remote actions."""
import argparse
import os
from pathlib import Path
import shutil


def create_workspace(destination):
    skill = Path(__file__).resolve().parents[1]
    repository = skill.parents[1]
    destination = Path(destination).expanduser().resolve()
    if destination == repository or repository in destination.parents:
        raise ValueError('Use a private working directory outside the repository')
    os.umask(0o077)
    destination.mkdir(parents=True, exist_ok=False)
    for source in sorted((skill / 'scripts').glob('*.py')):
        if source.name != 'create_workspace.py':
            shutil.copyfile(source, destination / source.name)
    shutil.copytree(skill / 'scripts/tests', destination / 'tests',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copyfile(skill / 'assets/migration-config.example.json',
                    destination / 'migration-config.json')
    shutil.copyfile(skill / 'assets/CHECKPOINT-RESTORE.txt',
                    destination / 'CHECKPOINT-RESTORE.txt')
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    result = create_workspace(args.destination)
    print(f'Created {result}. No remote actions performed.')
    print('Run tests there before configuring accounts or entering credentials.')
