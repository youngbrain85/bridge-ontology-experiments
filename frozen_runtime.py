"""Validate stored software before loading it in an isolated worker process."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import types

MODULES = ('providers', 'knowledge_builder', 'review_export', 'experiment')
REQUIRED = tuple(name + '.py' for name in MODULES) + ('model_catalog.json',)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verified_software(experiment):
    exp = Path(experiment).resolve()
    manifest_path = exp / 'manifest.json'
    if sha(manifest_path) != (exp / 'manifest.sha256').read_text(encoding='ascii').strip():
        raise ValueError('Manifest changed')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    files = manifest.get('files')
    if not isinstance(files, dict) or any('software/' + name not in files for name in REQUIRED):
        raise ValueError('Frozen files changed: missing software manifest')
    for name, expected in files.items():
        path = (exp / name).resolve()
        try:
            path.relative_to(exp)
        except ValueError:
            raise ValueError('Frozen files changed: path outside experiment')
        if not path.is_file() or sha(path) != expected:
            raise ValueError('Frozen files changed: ' + name)
    software = (exp / 'software').resolve()
    # Refuse additional executable import sources that were not frozen.
    for path in software.rglob('*'):
        if path.is_file() and (path.suffix in ('.py', '.pyd') or path.name == 'model_catalog.json'):
            if path.relative_to(exp).as_posix() not in files:
                raise ValueError('Frozen files changed: unrecorded software')
    return software


def load_in_worker(experiment):
    """Only call in a single-experiment process, never in the threaded server."""
    software = verified_software(experiment)
    for name in MODULES:
        sys.modules.pop(name, None)
    # Do not add the frozen directory to sys.path: unrelated unrecorded modules
    # must not shadow standard-library dependencies. Project modules are loaded
    # explicitly below, in dependency order, into sys.modules.
    # -B disables cache writes, not reads. Execute verified source directly so a
    # stale or unrecorded pyc cannot override the frozen source bytes.
    manifest = json.loads((Path(experiment) / 'manifest.json').read_text(encoding='utf-8-sig'))
    sources = {name: (software / (name + '.py')).read_bytes() for name in MODULES}
    for name, source in sources.items():
        if hashlib.sha256(source).hexdigest() != manifest['files']['software/' + name + '.py']:
            raise ValueError('Frozen files changed: software source')
    for name, source in sources.items():
        module = types.ModuleType(name)
        module.__file__ = str(software / (name + '.py'))
        module.__package__ = ''
        sys.modules[name] = module
        exec(compile(source, module.__file__, 'exec'), module.__dict__)
    engine = sys.modules['experiment']
    if Path(engine.__file__).resolve() != software / 'experiment.py':
        raise ValueError('Runner code differs from frozen version')
    engine.verify_frozen(experiment)
    return engine


def check_in_process(experiment):
    software = verified_software(experiment)
    result = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(Path(__file__).resolve()),
                             '--check', str(Path(experiment).resolve())],
                            capture_output=True, text=True, encoding='utf-8', timeout=120,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError('Frozen experiment check failed')
    value = json.loads(result.stdout)
    if value.get('passed') is not True:
        raise ValueError('Frozen experiment check failed')
    return value


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', required=True)
    args = parser.parse_args()
    engine = load_in_worker(args.check)
    print(json.dumps(engine.verify_frozen(args.check)))
