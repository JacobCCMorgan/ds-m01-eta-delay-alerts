"""Plot every frozen scenario from retained final-evaluation metrics."""
import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def plot(report, output):
    report = Path(report)
    summary = json.loads((report / 'summary.json').read_text())
    ids = summary['scenario_ids']
    reports = [json.loads((report / 'scenarios' / sid / 'forecast_metrics.json').read_text()) for sid in ids]
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10,
                         'axes.spines.top': False, 'axes.spines.right': False})
    fig, axes = plt.subplots(1, 2, figsize=(12.8, 6.3), sharey=True)
    cases = [("eta", "trip_all_eligible_snapshots_coverage", "Whole-trip interval coverage", axes[0]),
             ("probability", "brier_score", "Trip-weighted Brier score", axes[1])]
    colors = {'raw': '#687785', 'calibrated': '#007E87'}
    for family, metric, title, ax in cases:
        for variant, offset in [('raw', -.12), ('calibrated', .12)]:
            for i, result in enumerate(reports):
                method = result[family]['methods']['pipeline/' + variant]
                value = method['metrics'][metric]
                if value is None:
                    continue
                interval = method['bootstrap_intervals'][metric]
                ax.scatter(value, i + offset, color=colors[variant], s=34,
                           label=variant.capitalize() if i == 0 else None, zorder=3)
                if interval['lower'] is not None:
                    ax.plot([interval['lower'], interval['upper']], [i + offset, i + offset],
                            color=colors[variant], linewidth=1.2)
        ax.set_title(title, loc='left', fontweight='bold', pad=18)
        ax.grid(axis='x', alpha=.16)
        ax.set_xlabel('Higher is better' if family == 'eta' else 'Lower is better')
    axes[0].axvline(.8, color='#B87521', linestyle='--', linewidth=1, label='Nominal 80%')
    axes[0].set_xlim(-.02, 1.02)
    labels = {'S00_unmodified': 'S00  Unmodified', 'S01_missing_position': 'S01  Missing position',
              'S02_stale_position': 'S02  Stale position', 'S03_unseen_route_slot': 'S03  Unknown route slot',
              'S04_natural_entity_holdouts': 'S04  Natural holdouts',
              'S05_stationary_window': 'S05  Stationary window', 'S06_distance_scale': 'S06  Distance scale',
              'S07_transport_degraded': 'S07  Degraded transport',
              'S08_missing_and_degraded': 'S08  Missing + degraded'}
    axes[0].set_yticks(range(len(ids)), [labels[sid] for sid in ids])
    axes[0].set_ylim(len(ids) - .5, -.5)
    axes[0].legend(loc='lower left', bbox_to_anchor=(0, 1.08), ncol=3, frameon=False)
    fig.suptitle('Fixed final evaluation · synthetic August–September cohort', x=.04, ha='left',
                 fontsize=15, fontweight='bold')
    fig.text(.04, .025, 'Lines: descriptive 95% stratified whole-trip bootstrap intervals. '
             'All fixed scenarios are retained; S00 remains primary.\n'
             'Stress cases test input sensitivity and synthetic transport, not causal effects or deployment acceptance.',
             fontsize=9, color='#4B5563')
    fig.subplots_adjust(left=.27, right=.97, top=.77, bottom=.17, wspace=.18)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=170, facecolor='white')
    plt.close(fig)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    plot(args.report, args.output)
