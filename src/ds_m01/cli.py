import argparse
import json
from pathlib import Path
import time
from datetime import datetime,timezone
from .generate import generate
from .pipeline import build
from .warehouse import build_warehouse
from .common import write_source_manifest


def main():
    p=argparse.ArgumentParser(description='DS-M01 reproducible synthetic ETA data pipeline')
    p.add_argument('command',choices=['generate','build','validate','run','http-check'])
    p.add_argument('--root',type=Path,default=Path('.'))
    p.add_argument('--config',type=Path,default=None)
    p.add_argument('--telemetry-mode',choices=['snapshot','http_snapshot'],default=None)
    args=p.parse_args();root=args.root.resolve()
    config_path = args.config or (root/'config/m2.json' if (root/'config/m2.json').exists() else root/'config/m1.json')
    c=json.loads(config_path.read_text())
    if args.telemetry_mode:c['telemetry_mode']=args.telemetry_mode
    start=time.perf_counter();result={}
    if args.command in ('generate','run'):
        result['generated_counts']=generate(root,c)
        write_source_manifest(root,result['generated_counts'])
    if args.command in ('build','run'):result['summary']=build(root,c)
    if args.command in ('validate','run'):
        result['quality']=build_warehouse(root,c)
    if args.command=='http-check':
        from .event_api import check
        result=check(root)
    result['execution']={'finished_at_utc':datetime.now(timezone.utc).isoformat(),'elapsed_seconds':round(time.perf_counter()-start,3),'command':args.command,'config_path':str(config_path),'config_sha256':__import__('hashlib').sha256(config_path.read_bytes()).hexdigest()}
    (root/'reports').mkdir(exist_ok=True)
    (root/'reports'/f'last_{args.command}.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    if 'quality' in result and not result['quality']['passed']:raise SystemExit('Quality checks failed; see reports/quality_report.json')
    print(json.dumps({k:v for k,v in result.items() if k!='quality'},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
