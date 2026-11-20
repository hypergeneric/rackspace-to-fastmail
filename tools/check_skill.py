#!/usr/bin/env python3
"""Validate the distributed skill in an isolated, synthetic-only workspace."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    repository = Path(__file__).resolve().parents[1]
    skill = repository / 'skills/rackspace-to-fastmail'
    entry = (skill / 'SKILL.md').read_text()
    if not entry.startswith('---\nname: rackspace-to-fastmail\n'):
        raise RuntimeError('Missing expected skill frontmatter')
    for required in ('references/migration-guide.md', 'assets/migration-config.example.json',
                     'assets/CHECKPOINT-RESTORE.txt', 'scripts/create_workspace.py'):
        if not (skill / required).is_file():
            raise RuntimeError(f'Missing skill resource: {required}')
    spec = importlib.util.spec_from_file_location('workspace_builder', skill / 'scripts/create_workspace.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory(prefix='rs2fm-check-') as temporary:
        work = module.create_workspace(Path(temporary) / 'migration')
        for path in work.rglob('*.py'):
            compile(path.read_text(), str(path), 'exec')
        try:
            module.create_workspace(work)
        except FileExistsError:
            pass
        else:
            raise RuntimeError('Workspace builder allowed an existing destination')
        try:
            module.create_workspace(repository / 'must-not-be-created')
        except ValueError:
            pass
        else:
            raise RuntimeError('Workspace builder allowed private data inside repository')
        subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                       cwd=work, check=True)
    print('Workspace creation, compilation, safeguards and synthetic suite passed.')


if __name__ == '__main__':
    main()
