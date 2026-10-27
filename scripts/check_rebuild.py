"""Fresh-directory rebuild and actual-HTTP-ingested feature parity, no Git operations."""
from pathlib import Path
import argparse,json,shutil,tempfile,time
from datetime import datetime,timezone
from ds_m01.generate import generate
from ds_m01.pipeline import build
from ds_m01.warehouse import build_warehouse
from ds_m01.common import digest_file,write_source_manifest

p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=Path('.'));p.add_argument('--config',type=Path);args=p.parse_args()
root=args.root.resolve();config_path=args.config or (root/'config/m2.json' if (root/'config/m2.json').exists() else root/'config/m1.json');config=json.loads(config_path.read_text());start=time.perf_counter()
with tempfile.TemporaryDirectory(prefix='ds-m01-rebuild-') as tmp:
    work=Path(tmp)
    for directory in ['config','contracts','sql','reports','data/ingested']: (work/directory).mkdir(parents=True,exist_ok=True)
    shutil.copy2(root/'contracts/fields.json',work/'contracts/fields.json')
    counts=generate(work,config);write_source_manifest(work,counts);build(work,config);quality=build_warehouse(work,config)
    comparisons=[]
    for folder in ['raw','processed','parquet']:
        for original in sorted((root/'data'/folder).iterdir()):
            if original.suffix not in ('.jsonl','.parquet'):continue
            other=work/'data'/folder/original.name
            actual=digest_file(other) if other.exists() else None
            comparisons.append({'path':f'data/{folder}/{original.name}','original_sha256':digest_file(original),'rebuilt_sha256':actual,'equal':actual==digest_file(original)})
    before={x.name:digest_file(x) for x in (work/'data/processed').glob('*.jsonl')}
    # Snapshot hashes are recorded; release reproducible scratch outputs before
    # the atomic HTTP rebuild, so two full provenance copies are never retained here.
    shutil.rmtree(work/'data/processed')
    shutil.copy2(root/'data/ingested/telemetry_event.jsonl',work/'data/ingested/telemetry_event.jsonl')
    http_config=dict(config,telemetry_mode='http_snapshot');build(work,http_config)
    parity=[{'file':x.name,'equal':before[x.name]==digest_file(x)} for x in (work/'data/processed').glob('*.jsonl')]
    report={'status':'passed' if quality['passed'] and all(x['equal'] for x in comparisons+parity) else 'failed',
            'scope':'fresh generation, point-in-time pipeline, typed DuckDB validation, Parquet; HTTP telemetry feature parity with operational snapshots',
            'generated_counts':counts,'files':comparisons,'http_input_parity':parity,'quality_passed':quality['passed'],
            'config_sha256':digest_file(config_path),'postgres_service_parity':'not_run','finished_at_utc':datetime.now(timezone.utc).isoformat(),'elapsed_seconds':round(time.perf_counter()-start,3)}
    (root/'reports/rebuild_report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k not in ('files','http_input_parity')},indent=2))
    if report['status']!='passed':raise SystemExit(1)
