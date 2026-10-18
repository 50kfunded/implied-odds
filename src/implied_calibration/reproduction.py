"""I rebuild the included sample report offline in a temporary research database."""

import argparse
import hashlib
import json
from pathlib import Path
import tempfile

from .baseline_lock import load_locked_baselines
from .ingest import ingest
from .locking import load_locked_protocol
from .report import build_report, write_report
from .store import Store
from .synthetic_report import synthetic_report


def verify_sample(manifest_path: Path) -> dict:
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if (not isinstance(manifest,dict) or set(manifest) != {'schema_version','kind','description','as_of_ms','files'}
            or type(manifest['schema_version']) is not int or manifest['schema_version'] != 1
            or manifest['kind'] != 'public_market_capture' or not isinstance(manifest['files'],dict)
            or not manifest['files'] or type(manifest['as_of_ms']) is not int or manifest['as_of_ms'] < 0):
        raise ValueError('Invalid included-sample manifest')
    total = 0
    for relative, expected in sorted(manifest['files'].items()):
        path = (manifest_path.parent/relative).resolve()
        if not path.is_relative_to(manifest_path.parent) or not path.is_file() or Path(relative).is_absolute():
            raise ValueError('Sample path must stay within the recordings folder')
        body = path.read_bytes()
        if (not isinstance(expected,dict) or set(expected) != {'sha256','bytes'} or expected['bytes'] != len(body)
                or expected['sha256'] != hashlib.sha256(body).hexdigest()):
            raise ValueError(f'Included sample differs from its manifest: {relative}')
        total += len(body)
    if total >= 5_000_000:
        raise ValueError('Included sample must remain below 5 MB')
    inventory = {p.relative_to(manifest_path.parent).as_posix() for p in (manifest_path.parent/'smoke-01').rglob('*') if p.is_file()}
    if inventory != set(manifest['files']):
        raise ValueError('Included sample inventory differs from its manifest; I reject unlisted files')
    return manifest


def reproduce(output: Path, *, manifest_path: Path | None = None, include_synthetic: bool = True) -> dict:
    from .cli import provenance

    root = Path(__file__).resolve().parents[2]
    manifest_path = root/'recordings'/'manifest.json' if manifest_path is None else Path(manifest_path)
    manifest = verify_sample(manifest_path)
    parent = load_locked_protocol(root/'protocol'/'protocol.json')
    extension = load_locked_baselines(root/'protocol'/'baselines.json',parent)
    details = provenance()
    output = Path(output).resolve()
    if output.is_relative_to(manifest_path.resolve().parent):
        raise ValueError('Report output must stay outside the retained recording folder')
    output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='rebuild-',dir=output) as temporary:
        db = Path(temporary)/'sample.sqlite'
        store = Store(db)
        try:
            ingestion = ingest(manifest_path.resolve().parent/'smoke-01',store)
        finally:
            store.close()
        market = build_report(db,parent,extension,manifest['as_of_ms'],details['git_commit'])
        market['provenance']['working_tree_dirty'] = details['working_tree_dirty']
        market['retained_sample'] = manifest
        market['ingestion'] = ingestion
        market['checklist']['raw_retention_verified_against_manifest'] = True
        reports = {'market':write_report(market,output/'market')}
    if include_synthetic:
        synthetic = synthetic_report(parent,extension,details['git_commit'])
        synthetic['provenance']['working_tree_dirty'] = details['working_tree_dirty']
        reports['synthetic'] = write_report(synthetic,output/'synthetic')
    files = {f'{folder}/{name}':hashlib.sha256((output/folder/name).read_bytes()).hexdigest()
             for folder,report in reports.items() for name in report['report_files']}
    result = {'status':'reproduced','sample_kind':manifest['kind'],'as_of_ms':manifest['as_of_ms'],
              'sample_files':manifest['files'],'provenance':details,'reports':reports,'file_sha256':files,
              'note':"I keep the market report separate from generated illustrations. I don't create research forecasts during reproduction."}
    (output/'reproduction.json').write_text(json.dumps(result,indent=2,sort_keys=True,allow_nan=False)+'\n',encoding='utf-8',newline='\n')
    synthetic_link = '<p><a href="synthetic/report.html">Synthetic method illustration</a></p>' if include_synthetic else ''
    html = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Implied odds reports</title>
<style>body{{font:18px/1.6 system-ui,sans-serif;max-width:760px;margin:70px auto;padding:24px;color:#183042}}a{{color:#185c80}}</style></head><body>
<h1>Implied odds</h1><p>I rebuild these reports offline from retained or generated inputs.</p>
<p><a href="market/report.html">Retained market sample</a></p>{synthetic_link}
<p>I haven't collected settled research forecasts or measured calibration. The synthetic illustration exercises the reporting code.</p>
<p>I keep source and output hashes in reproduction.json.</p></body></html>'''
    (output/'index.html').write_text(html,encoding='utf-8',newline='\n')
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description='I reproduce the included market report and a separate synthetic illustration offline.')
    parser.add_argument('--output',type=Path,default=Path('results/reproduction'))
    parser.add_argument('--no-synthetic',action='store_true')
    args = parser.parse_args()
    result = reproduce(args.output,include_synthetic=not args.no_synthetic)
    print(json.dumps({'status':result['status'],'reports':result['reports']},indent=2,sort_keys=True))
    return 0
