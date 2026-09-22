"""Check the English source distribution without credentials or provider calls."""
from pathlib import Path
import ast
import hashlib
import json
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def main():
    import experiment
    import knowledge_builder

    process = subprocess.run(
        ['git', '-C', str(ROOT), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
        check=True, stdout=subprocess.PIPE)
    names = sorted(set(x.decode('utf-8') for x in process.stdout.split(b'\0') if x))
    errors = []
    forbidden_dirs = {'runtime', 'credentials', 'experiments', 'batches', 'private_data', 'node_modules', '__pycache__'}
    forbidden_suffixes = {'.dpapi', '.pem', '.key', '.p12', '.pfx', '.dwg', '.pdf', '.glb', '.dxf', '.obj', '.zip'}
    language_pattern = re.compile('[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af\u3040-\u30ff\u4e00-\u9fff]')
    key_pattern = re.compile(r'(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{25,})')
    text_suffixes = {'.py','.js','.mjs','.json','.md','.html','.css','.ttl','.owl','.txt','.ps1','.cmd','.yml','.yaml','.svg'}
    for name in names:
        p = ROOT / name
        if not p.is_file():
            errors.append('Missing file: '+name)
            continue
        if not name.isascii():
            errors.append('Non-ASCII filename: '+name)
        if forbidden_dirs.intersection(Path(name).parts) or p.suffix.lower() in forbidden_suffixes or p.name.startswith('.env'):
            errors.append('Private/generated file is included: '+name)
        if p.stat().st_size > 5_000_000:
            errors.append('Unexpected large file: '+name)
        if p.suffix.lower() in text_suffixes or p.name.startswith('.git'):
            s = p.read_text(encoding='utf-8-sig')
            if language_pattern.search(s):
                errors.append('Non-English script in: '+name)
            if key_pattern.search(s):
                errors.append('Potential credential in: '+name)
            if re.search(r'[A-Za-z]:[\\/](?:Users|Projects)[\\/]',s):
                errors.append('Machine-specific path in: '+name)
            if p.suffix == '.py':
                ast.parse(s, filename=name)
            elif p.suffix == '.json':
                json.loads(s)

    cfg = experiment.read_json(ROOT/'config.json')
    experiment.validate_config(cfg)
    for key in ('knowledge_source','common_instruction','output_schema','geometry_contract','backend_dir'):
        if Path(cfg[key]).is_absolute() or not (ROOT/cfg[key]).exists():
            errors.append('Invalid default resource: '+key)
    for case in cfg['cases']:
        if case['case_id'] != 'SYNTHETIC_DEMO':
            errors.append('Default packet must be the synthetic example')
        for name in [case['scope'],case['text'],case['provenance']]+case['images']:
            if Path(name).is_absolute() or not (ROOT/name).is_file():
                errors.append('Missing/nonportable case resource: '+name)
        for name in case['images']:
            p=ROOT/name
            if p.exists() and p.suffix.lower()=='.png' and not p.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'):
                errors.append('Invalid PNG: '+name)
    if errors:
        print(json.dumps({'status':'failed','errors':errors},indent=2))
        return 1
    with tempfile.TemporaryDirectory(prefix='bridge_english_projection_') as tmp:
        audit = knowledge_builder.build_knowledge(ROOT/cfg['knowledge_source'], Path(tmp)/'knowledge')
        if audit['fact_count'] != 880 or audit['status'] != 'passed':
            raise AssertionError('Ontology projection audit failed')
    manifest_path=ROOT/'release_manifest.json'
    if manifest_path.exists():
        manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        for name,expected in manifest['sha256'].items():
            payload=(ROOT/name).read_bytes()
            if Path(name).suffix.lower() != '.png':
                payload=payload.replace(b'\r\n',b'\n')
            actual=hashlib.sha256(payload).hexdigest()
            if actual!=expected:
                raise AssertionError('Release hash mismatch: '+name)
        if set(manifest['sha256']) != set(names)-{'release_manifest.json'}:
            raise AssertionError('Release file inventory changed; regenerate the manifest')
    print(json.dumps({'status':'passed','source_files_checked':len(names),'ontology_facts':880,'paid_api_calls':0}))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
