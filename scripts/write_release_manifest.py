"""Record the English source distribution; never include ignored private files."""
from pathlib import Path
import hashlib
import json
import subprocess

ROOT=Path(__file__).resolve().parents[1]

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
        'engine_version':'0.4.0',
        'webapp_version':'0.2.5',
        'backend_version':'0.1.2',
        'input_protocol_version':'deck_assembly_minimal_en_v1',
        'base_source_release_manifest_sha256':'5e45410451ff90097d147db7fc248cc3a9bf4f1479a9b1b93d8e77f4bb04f1ab',
        'source_note':'English source edition of the synchronized local engine, web app, and converter; original experiment archives are not included.',
        'hash_policy':'SHA-256; UTF-8 text with CRLF normalized to LF, PNG bytes unchanged; this manifest excludes itself.',
        'sha256':hashes,
    }
    (ROOT/'release_manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'manifest_files':len(hashes),'edition':manifest['edition']}))

if __name__=='__main__':
    main()
