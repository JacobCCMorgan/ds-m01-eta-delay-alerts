#!/usr/bin/env python3
"""Plot M3a training-period baseline metrics without replacing missing values."""

from __future__ import annotations

import argparse
from datetime import date
import json
import math
from numbers import Real
from pathlib import Path

import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter, StrMethodFormatter


INK = '#203448'
MUTED = '#52697C'
BLUE = '#0072B2'
ORANGE = '#E69F00'
GRID = '#E4EAF0'


def _number(value, name, *, upper=None):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f'{name} must be numeric or null')
    if not math.isfinite(value) or value < 0 or (upper is not None and value > upper):
        raise ValueError(f'{name} is outside its valid range')
    return float(value)


def _count(value, name):
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f'{name} must be a nonnegative integer or null')
    return value


def read_inputs(directory: Path) -> tuple[dict, list[dict], dict]:
    metrics = json.loads((directory / 'metrics.json').read_text(encoding='utf-8'))
    manifest = json.loads((directory / 'model_manifest.json').read_text(encoding='utf-8'))
    if manifest.get('source_kind') != 'synthetic':
        raise ValueError('This figure requires an explicitly synthetic source dataset')
    if manifest.get('evaluated_original_roles') != ['train']:
        raise ValueError('This figure is restricted to training-period validation')
    if manifest.get('calibration_performed') is not False or manifest.get('final_test_evaluated') is not False:
        raise ValueError('Calibration and final-test evaluation must remain unperformed for M3a')
    folds = manifest.get('folds')
    if not isinstance(folds, list) or not folds:
        raise ValueError('The model manifest must identify the evaluated folds')
    by_fold = metrics.get('slices', {}).get('fold_id', {})
    if not isinstance(by_fold, dict):
        raise ValueError('metrics.slices.fold_id must be a mapping')

    result = []
    seen = set()
    for fold in folds:
        fold_id = fold.get('fold_id')
        if not isinstance(fold_id, str) or not fold_id or fold_id in seen:
            raise ValueError('Each manifest fold needs a unique nonempty fold_id')
        seen.add(fold_id)
        summary = by_fold.get(fold_id, {})
        values = summary.get('metrics', {})
        interval = summary.get('mae_bootstrap_95pct', {})
        counts = summary.get('counts', {})
        lower = _number(interval.get('lower_minutes'), f'{fold_id}.CI.lower')
        upper = _number(interval.get('upper_minutes'), f'{fold_id}.CI.upper')
        if lower is not None and upper is not None and lower > upper:
            raise ValueError(f'{fold_id}: reversed MAE confidence interval')
        if lower is not None or upper is not None:
            if interval.get('confidence_level') != 0.95 or interval.get('method') != 'trip_cluster_percentile':
                raise ValueError(f'{fold_id}: expected a 95% trip-cluster percentile interval')
        eligible = _count(counts.get('eligible_snapshots'), f'{fold_id}.eligible_snapshots')
        supported = _count(counts.get('supported_eligible_snapshots'), f'{fold_id}.supported_eligible_snapshots')
        eligible_trips = _count(counts.get('eligible_trips'), f'{fold_id}.eligible_trips')
        scored_trips = _count(counts.get('supported_eligible_trips'), f'{fold_id}.supported_eligible_trips')
        if eligible is not None and supported is not None and supported > eligible:
            raise ValueError(f'{fold_id}: supported snapshots exceed eligible snapshots')
        if eligible_trips is not None and scored_trips is not None and scored_trips > eligible_trips:
            raise ValueError(f'{fold_id}: scored trips exceed eligible trips')
        period = fold.get('validation_departure_dates')
        month = date.fromisoformat(period[0]).strftime('%b') if period else ''
        result.append({
            'fold_id': fold_id, 'label': f'{fold_id}\n{month}' if month else fold_id,
            'mae': _number(values.get('MAE_minutes'), f'{fold_id}.MAE_minutes'),
            'ci_lower': lower, 'ci_upper': upper,
            'coverage': _number(values.get('p10_p90_empirical_coverage'), f'{fold_id}.coverage', upper=1),
            'eligible': eligible, 'supported': supported,
            'eligible_trips': eligible_trips, 'scored_trips': scored_trips,
        })
    overall = metrics.get('overall', {})
    for key in ('snapshots', 'supported_eligible_snapshots', 'supported_eligible_trips'):
        _count(overall.get('counts', {}).get(key), f'overall.{key}')
    _count(overall.get('prediction_status_counts', {}).get('available'), 'overall.available')
    return manifest, result, overall


def _style(axis, *, grid_axis='y'):
    axis.grid(axis=grid_axis, color=GRID, linewidth=0.7)
    axis.set_axisbelow(True)
    axis.tick_params(axis='both', labelsize=9, length=0, pad=5)
    for spine in axis.spines.values():
        spine.set_visible(False)


def render(manifest: dict, folds: list[dict], output: Path, overall: dict | None = None) -> None:
    with plt.rc_context({'font.family': 'DejaVu Sans', 'text.color': INK,
                         'axes.labelcolor': INK, 'xtick.color': MUTED, 'ytick.color': MUTED}):
        figure = plt.figure(figsize=(9.3, 7.0), dpi=150)
        layout = figure.add_gridspec(2, 2, height_ratios=(1, 1.12), hspace=0.63, wspace=0.32)
        mae_axis = figure.add_subplot(layout[0, 0])
        coverage_axis = figure.add_subplot(layout[0, 1])
        support_axis = figure.add_subplot(layout[1, :])
        figure.subplots_adjust(left=0.19, right=0.95, top=0.79, bottom=0.27)
        figure.text(0.07, 0.94, 'Conditional-history ETA baseline', fontsize=18, fontweight='bold')
        figure.text(0.07, 0.895, 'Training-period validation on synthetic 2026 scenarios',
                    fontsize=11, color=MUTED)

        positions = range(len(folds))
        labels = [fold['label'] for fold in folds]
        for axis in (mae_axis, coverage_axis):
            _style(axis)
            axis.set_xticks(list(positions), labels)
            axis.set_xlim(-0.45, len(folds) - 0.55)
        mae_axis.set_title('Mean absolute ETA error', loc='left', fontsize=12, fontweight='bold', pad=14)
        mae_axis.set_ylabel('Trip-balanced MAE (minutes)', fontsize=9)
        known_mae = [value for fold in folds for value in (fold['mae'], fold['ci_upper']) if value is not None]
        mae_limit = max(known_mae, default=1) * 1.35 or 1
        mae_axis.set_ylim(0, mae_limit)
        for i, fold in enumerate(folds):
            if fold['mae'] is None:
                mae_axis.text(i, mae_limit * 0.12, 'Not\navailable', ha='center', fontsize=8, color=MUTED)
                continue
            low, high = fold['ci_lower'], fold['ci_upper']
            if low is not None and high is not None:
                mae_axis.vlines(i, low, high, color=BLUE, linewidth=1.6)
                mae_axis.hlines([low, high], i - 0.07, i + 0.07, color=BLUE, linewidth=1.6)
                anchor = max(high, fold['mae'])
            else:
                anchor = fold['mae']
                mae_axis.text(i, max(0, anchor - mae_limit * 0.15), 'CI unavailable',
                              ha='center', fontsize=7, color=MUTED)
            mae_axis.scatter(i, fold['mae'], s=34, color=BLUE, zorder=3)
            mae_axis.text(i, anchor + mae_limit * 0.055, f"{fold['mae']:.1f}",
                          ha='center', fontsize=9, color=INK)

        coverage_axis.set_title('Raw P10–P90 coverage', loc='left', fontsize=12, fontweight='bold', pad=14)
        coverage_axis.set_ylabel('Trip-balanced coverage', fontsize=9)
        coverage_axis.set_ylim(0, 1.08)
        coverage_axis.yaxis.set_major_formatter(PercentFormatter(1))
        coverage_axis.axhline(0.8, color=MUTED, linestyle=(0, (4, 3)), linewidth=1)
        coverage_axis.text(0.03, 0.97, 'Dashed line: 80% reference', transform=coverage_axis.transAxes,
                           ha='left', va='top', fontsize=8, color=MUTED)
        for i, fold in enumerate(folds):
            if fold['coverage'] is None:
                coverage_axis.text(i, 0.12, 'Not\navailable', ha='center', fontsize=8, color=MUTED)
                continue
            coverage_axis.scatter(i, fold['coverage'], s=42, marker='D', color=BLUE, zorder=3)
            coverage_axis.text(i, fold['coverage'] + 0.065, f"{fold['coverage']:.1%}",
                               ha='center', fontsize=9, color=INK)

        _style(support_axis, grid_axis='x')
        support_axis.set_title('Prediction support among eligible snapshots', loc='left',
                               fontsize=12, fontweight='bold', pad=22)
        support_axis.set_xlabel('Eligible snapshots', fontsize=9, labelpad=8)
        count_labels = []
        for fold in folds:
            eligible = 'n/a' if fold['eligible_trips'] is None else f"{fold['eligible_trips']:,}"
            scored = 'n/a' if fold['scored_trips'] is None else f"{fold['scored_trips']:,}"
            count_labels.append(f"{fold['fold_id']}\n{eligible} / {scored} trips")
        support_axis.set_yticks(list(positions), count_labels)
        support_axis.tick_params(axis='y', labelsize=8, pad=9)
        support_axis.invert_yaxis()
        maximum = max([fold['eligible'] for fold in folds if fold['eligible'] is not None], default=1) or 1
        support_axis.set_xlim(0, maximum * 1.39)
        support_axis.xaxis.set_major_formatter(StrMethodFormatter('{x:,.0f}'))
        legend_used = False
        for i, fold in enumerate(folds):
            total, supported = fold['eligible'], fold['supported']
            if total is None or supported is None:
                support_axis.text(maximum * 0.02, i, 'Counts unavailable', va='center', fontsize=9, color=MUTED)
                continue
            unsupported = total - supported
            support_axis.barh(i, supported, height=0.54, color=BLUE,
                              label='Supported' if not legend_used else None)
            support_axis.barh(i, unsupported, left=supported, height=0.54, color=ORANGE,
                              hatch='///', edgecolor='white', linewidth=0.4,
                              label='Unsupported' if not legend_used else None)
            legend_used = True
            support_axis.text(total + maximum * 0.025, i,
                              f'{supported:,} / {total:,} supported\n{unsupported:,} unsupported',
                              va='center', fontsize=8, color=INK)
        if legend_used:
            support_axis.legend(loc='lower right', bbox_to_anchor=(1.0, 1.02), frameon=False,
                                fontsize=8, ncol=2, handlelength=1.4)

        overall = overall or {}
        counts = overall.get('counts', {})
        statuses = overall.get('prediction_status_counts', {})
        def count_text(value):
            return 'n/a' if value is None else f'{value:,}'
        figure.text(0.07, 0.18,
                    f"All opportunities: {count_text(statuses.get('available'))}/{count_text(counts.get('snapshots'))} available. "
                    f"Errors use {count_text(counts.get('supported_eligible_snapshots'))} eligible supported snapshots "
                    f"from {count_text(counts.get('supported_eligible_trips'))} trips.",
                    fontsize=8, color=INK, fontweight='bold')
        figure.text(0.07, 0.135,
                    'Fold labels show eligible / scored trips. Scores weight trips equally; '
                    'MAE whiskers show 95% trip-bootstrap intervals.', fontsize=8, color=MUTED)
        figure.text(0.07, 0.09,
                    'P10–P90 intervals are uncalibrated. The 80% reference is not a coverage guarantee. '
                    'Unsupported predictions are not zeros.', fontsize=8, color=MUTED)
        figure.text(0.07, 0.045,
                    'July calibration and August–September final-test periods remain untouched. '
                    'Results do not establish operational fleet performance.', fontsize=8, color=MUTED)
        output.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(output, dpi=300, facecolor='white',
                       metadata={'Software': 'DS-M01 M3a baseline validation plot',
                                 'Description': manifest.get('batch_id', 'DS-M01 M3a')})
        plt.close(figure)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=Path('reports/m3a'),
                        help='Directory containing metrics.json and model_manifest.json')
    parser.add_argument('--output', type=Path, default=Path('reports/m3a/baseline_validation.png'))
    args = parser.parse_args()
    manifest, folds, overall = read_inputs(args.input)
    render(manifest, folds, args.output, overall)
    print(args.output)


if __name__ == '__main__':
    main()
