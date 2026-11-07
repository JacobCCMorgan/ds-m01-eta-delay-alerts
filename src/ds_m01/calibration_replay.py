"""Verify retained P05 models, calibrated assessment and optional source labels.

Never refit base estimators or calibrators. Optional source checks rebuild
membership/features and cutoff-gated target joins from the frozen input bytes.
"""
import argparse
import json
from pathlib import Path

from .common import digest_file, read_jsonl
from .candidate_model import canonical_digest
from .calibration_models import validate_interval_calibrator, validate_probability_calibrator
from .calibration_refit import replay_predictions, prepare_refit_inputs
from .calibration_run import (PROTOCOL_SHA256, ARTIFACT_LISTS, ARTIFACT_JSON, CODE_MODULES,
                              verify_settings, read_refit, prepare_role_rows, source_bindings,
                              apply_assessment)
from .calibration_metrics import build_metrics
from .delay_replay import compare_values


def verify(root, report, *, input_root=None):
    root,report=Path(root).resolve(),Path(report).resolve()
    source,settings=verify_settings(root,root/'config/m3d.json')
    receipt=json.loads((report/'run_receipt.json').read_text())
    if receipt.get('status')!='completed_calibration_assessment' or receipt.get('protocol_sha256')!=PROTOCOL_SHA256:
        raise ValueError('Unrecognized calibration receipt')
    for name in ('buffer_labels_used','assessment_labels_used_for_fitting','july_labels_used_for_base_training','final_test_evaluated'):
        if receipt.get(name) is not False:
            raise ValueError(f'Calibration receipt exceeds scope: {name}')
    if receipt.get('input_sha256')!=settings['expected_source_sha256'] or receipt.get('parent_config_sha256')!=settings['expected_parent_config_sha256']:
        raise ValueError('Receipt source/configuration pins differ')
    expected_outputs={name+'.jsonl' for name in ARTIFACT_LISTS}|{'refit_'+name+'.json' for name in ARTIFACT_JSON}|{
        'calibration_fit_eta_rows.jsonl','calibration_fit_probability_rows.jsonl','assessment_eta_rows.jsonl',
        'assessment_probability_rows.jsonl','calibrators.json','metrics.json','summary.json'}
    if set(receipt.get('output_sha256',{}))!=expected_outputs or set(receipt.get('code_sha256',{}))!=set(CODE_MODULES):
        raise ValueError('Receipt output/code inventory differs')
    for name,expected in receipt['output_sha256'].items():
        if digest_file(report/name)!=expected:
            raise ValueError(f'Retained artifact changed: {name}')
    for name,expected in receipt['code_sha256'].items():
        if digest_file(root/'src/ds_m01'/name)!=expected or digest_file(Path(__file__).parent/name)!=expected:
            raise ValueError(f'Executed/retained source code differs: {name}')
    def compare(a,b):
        compare_values(a,b,absolute_tolerance=settings['replay']['absolute_numeric_tolerance'],
                       relative_tolerance=settings['replay']['relative_numeric_tolerance'])
    artifacts=read_refit(report)
    rebuilt=replay_predictions(artifacts,source,settings)
    for name in ('eta_predictions','baseline_predictions','delay_predictions'):
        compare(artifacts[name],rebuilt[name])
    states=json.loads((report/'calibrators.json').read_text())
    bindings=source_bindings(artifacts,settings)
    if set(states)!={'eta','probability'}:
        raise ValueError('Missing calibration family')
    for family,validator in (('eta',validate_interval_calibrator),('probability',validate_probability_calibrator)):
        seen=set()
        for state in states[family]:
            validator(state)
            if state['tenant_id'] in seen or state['source_bindings']!=bindings:
                raise ValueError('Duplicate calibrator or source binding changed')
            seen.add(state['tenant_id'])
    roles={'calibration_fit':{},'assessment':{}}
    stored={}
    for family in ('eta','probability'):
        roles['calibration_fit'][family]=list(read_jsonl(report/f'calibration_fit_{family}_rows.jsonl'))
        stored[family]=list(read_jsonl(report/f'assessment_{family}_rows.jsonl'))
        roles['assessment'][family]=[r for r in stored[family] if r['variant']=='raw']
    # Bind each stored calibration target/feature subset to its fitted artifact.
    from .calibration_models import _fit_rows
    for family in ('eta','probability'):
        for state in states[family]:
            fit=[r for r in roles['calibration_fit'][family] if r['tenant_id']==state['tenant_id'] and r['eligibility']=='eligible' and r['status']=='available']
            normalized=_fit_rows(fit,state['tenant_id'],state['fit_metadata'],'interval' if family=='eta' else 'probability')
            if canonical_digest(normalized)!=state['fit_rows_sha256']:
                raise ValueError('Calibrator fitting rows differ')
    paired=apply_assessment(roles['assessment'],states)
    for family in ('eta','probability'):
        compare(stored[family],paired[family])
    compare(json.loads((report/'metrics.json').read_text()),build_metrics(paired['eta'],paired['probability']))
    if input_root is not None:
        inputs=prepare_refit_inputs(input_root,source,settings)
        compare(artifacts['memberships'],inputs['memberships'])
        compare(artifacts['features'],inputs['features'])
        source_roles=prepare_role_rows(artifacts,input_root,settings,source)
        for role in roles:
            for family in ('eta','probability'):
                compare(roles[role][family],source_roles[role][family])
    return {'status':'passed','protocol_sha256':PROTOCOL_SHA256,
            'raw_eta_opportunities':len(artifacts['eta_predictions']),
            'raw_probability_rows':len(artifacts['delay_predictions']),
            'assessment_opportunities':len(roles['assessment']['eta']),
            'calibrators':sum(map(len,states.values())),
            'retained_inference_equal':True,'calibration_application_equal':True,'metrics_verified':True,
            'source_targets_verified':input_root is not None,'outcome_source_opened':input_root is not None,
            'base_model_training_executed':False,'calibrator_fitting_executed':False,
            'buffer_targets_used':False,'final_test_evaluated':False,'differences':0}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--report',type=Path,default=Path('reports/m3d'))
    p.add_argument('--input-root',type=Path)
    a=p.parse_args()
    report=a.report if a.report.is_absolute() else a.root/a.report
    print(json.dumps(verify(a.root,report,input_root=a.input_root),indent=2))


if __name__=='__main__':
    main()
