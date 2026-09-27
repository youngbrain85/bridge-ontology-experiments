"""Record the English source distribution; never include ignored private files."""
from pathlib import Path
import hashlib
import json
import re
import subprocess

ROOT=Path(__file__).resolve().parents[1]

def module_version(relative):
    text=(ROOT/relative).read_text(encoding='utf-8-sig')
    match=re.search(r'^VERSION\s*=\s*[\'"]([^\'"]+)[\'"]', text, re.M)
    if not match:
        raise SystemExit('No VERSION constant in '+relative)
    return match.group(1)

def main():
    output=subprocess.check_output(['git','-C',str(ROOT),'ls-files','-z','--cached','--others','--exclude-standard'])
    names=sorted(set(x.decode('utf-8') for x in output.split(b'\0') if x))
    hashes={}
    for name in names:
        if name=='release_manifest.json':
            continue
        raw=(ROOT/name).read_bytes()
        if Path(name).suffix.lower()!='.png':
            raw=raw.replace(b'\r\n',b'\n')
        hashes[name]=hashlib.sha256(raw).hexdigest()
    manifest={
        'edition':'english-2026-09-22',
        'engine_version':module_version('experiment.py'),
        'webapp_version':module_version('webapp/local_common.py'),
        'backend_version':module_version('backend/geometry_backend.py'),
        'input_protocol_version':json.loads((ROOT/'config.json').read_text(encoding='utf-8-sig'))['input_protocol_version'],
        'base_source_release_manifest_sha256':'5e45410451ff90097d147db7fc248cc3a9bf4f1479a9b1b93d8e77f4bb04f1ab',
        'source_note':'English source edition of the synchronized local engine, web app, and converter; original experiment archives are not included.',
        'hash_policy':'SHA-256; UTF-8 text with CRLF normalized to LF, PNG bytes unchanged; this manifest excludes itself.',
        'sha256':hashes,
    }
    (ROOT/'release_manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'manifest_files':len(hashes),'edition':manifest['edition']}))

if __name__=='__main__':
    main()
