"""Plot trip-weighted P50 arrival error for the frozen validation cohort."""

import argparse
import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import MultipleLocator

METHODS = [('historical_baseline', 'Historical baseline', '#70808e'),
           ('linear_quantile', 'Linear quantile', '#3a9993'),
           ('gradient_boosting_quantile', 'Gradient boosting', '#183f65')]
GPS = [('valid', 'Valid GPS'), ('low_confidence', 'Low-confidence GPS'), ('no_position', 'No GPS position')]


def plot(root, output):
    directory = root / 'reports/m3b3'
    methods = json.loads((directory / 'metrics.json').read_text())['methods']
    with (directory / 'slices.csv').open(newline='', encoding='utf-8') as stream:
        groups = {(r['value'], r['method']): r for r in csv.DictReader(stream)
                  if r['cohort'] == 'baseline_anchor' and r['stratum'] == 'position_validity'}
    labels = ['All eligible, baseline-supported'] + [label for _, label in GPS]
    ref = methods['historical_baseline']['cohorts']['baseline_anchor']
    trips = [ref['scored_trips']] + [int(groups[value, 'historical_baseline']['scored_trips']) for value, _ in GPS]
    fig, ax = plt.subplots(figsize=(10, 5.8))
    fig.subplots_adjust(left=.25, right=.94, top=.75, bottom=.16)
    for idx, (key, label, color) in enumerate(METHODS):
        values = [methods[key]['cohorts']['baseline_anchor']['median_mae_seconds'] / 60]
        values += [float(groups[value, key]['median_mae_seconds']) / 60 for value, _ in GPS]
        y = [i + (idx - 1) * .23 for i in range(len(labels))]
        bars = ax.barh(y, values, height=.195, color=color, label=label, zorder=3)
        ax.bar_label(bars, labels=[f'{v:.1f}' for v in values], padding=4, fontsize=9, color='#203240')
    ax.set_yticks(range(len(labels)), [f'{label}\n{n:,} trips' for label, n in zip(labels, trips)], fontsize=10)
    ax.invert_yaxis()
    ax.set_xlabel('Trip-weighted mean absolute error for the median ETA (minutes)', labelpad=9)
    ax.xaxis.set_major_locator(MultipleLocator(10))
    lo, hi = ax.get_xlim()
    ax.set_xlim(0, hi * 1.07)
    ax.grid(axis='x', alpha=.26)
    ax.tick_params(axis='both', length=0, pad=7)
    for spine in ax.spines.values():
        spine.set_visible(False)
    fig.text(.07, .94, 'ETA error by GPS quality', fontsize=20, weight='bold', color='#183345')
    fig.text(.07, .884, f'Synthetic training-period validation  |  {ref["cohort_snapshots"]:,} eligible prediction opportunities',
             fontsize=10.5, color='#475967')
    ax.legend(loc='lower left', bbox_to_anchor=(-.27, 1.04), ncol=3, frameon=False, fontsize=10)
    fig.text(.07, .038, 'Lower values indicate smaller errors. A trip can contribute snapshots to multiple GPS groups.',
             fontsize=9, color='#475967')
    fig.savefig(output, dpi=160, facecolor='white')
    plt.close(fig)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    plot(args.root, args.output or args.root / 'reports/m3b3/eta_error_by_gps.png')
