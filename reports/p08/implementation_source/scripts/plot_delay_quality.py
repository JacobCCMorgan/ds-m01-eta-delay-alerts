"""Plot the retained raw probability checks without opening outcome sources."""

import argparse
import csv
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    args = parser.parse_args()
    folder = args.root / 'reports/m3c'
    with (folder / 'reliability.csv').open() as stream:
        bins = list(csv.DictReader(stream))
    with (folder / 'slices.csv').open() as stream:
        slices = list(csv.DictReader(stream))
    fig, (left, right) = plt.subplots(1, 2, figsize=(12.8, 4.8), layout='constrained')
    methods = [('fold_prevalence', 'Training delay rate', '#87949f'),
               ('logistic_delay', 'Logistic classifier', '#17667d')]
    left.plot([0, 1], [0, 1], color='#c2c8cc', linestyle='--', linewidth=1)
    for method, label, color in methods:
        rows = [b for b in bins if b['method_id'] == method
                and b['cohort'] == 'common_supported' and int(b['snapshots'])]
        left.plot([float(b['mean_probability']) for b in rows],
                  [float(b['observed_event_rate']) for b in rows],
                  marker='o', color=color, linewidth=1.8, label=label)
        sparse = [b for b in rows if b['sparse'] == 'True']
        left.scatter([float(b['mean_probability']) for b in sparse],
                     [float(b['observed_event_rate']) for b in sparse],
                     facecolors='white', edgecolors=color, s=40, zorder=4)
    left.set(xlim=(0, 1), ylim=(-.02, 1.02), xlabel='Mean raw probability',
             ylabel='Observed delay fraction', title='Raw probability reliability')
    handles, labels = left.get_legend_handles_labels()
    handles.append(Line2D([], [], marker='o', color='#7f898f', markerfacecolor='white', linestyle='none'))
    labels.append('Bin with fewer than 20 trips')
    left.legend(handles, labels, frameon=False, fontsize=9, loc='upper left')
    groups = [('valid', 'Valid GPS'), ('low_confidence', 'Stale GPS'),
              ('no_position', 'No GPS')]
    for index, (method, label, color) in enumerate(methods):
        selected = {r['value']: r for r in slices if r['stratum'] == 'position_validity'
                    and r['method_id'] == method and r['cohort'] == 'common_supported'}
        positions, values = [], []
        for i, (key, _) in enumerate(groups):
            row = selected.get(key)
            if row and row['brier_score']:
                positions.append(i + (index - .5) * .35)
                values.append(float(row['brier_score']))
        bars = right.bar(positions, values, width=.33, color=color, label=label)
        right.bar_label(bars, fmt='%.3f', padding=3, fontsize=9)
    right.set_xticks(range(len(groups)), [label for _, label in groups])
    right.set(ylabel='Trip-weighted Brier score (lower is better)',
              title='Probability error by GPS quality')
    right.margins(y=.18)
    for ax in (left, right):
        ax.spines[['top', 'right']].set_visible(False)
        ax.set_axisbelow(True)
        ax.grid(axis='y', color='#e9ecee', linewidth=.8)
    fig.suptitle('Delay risk against the promise known at prediction time', fontsize=15)
    fig.supxlabel('Synthetic training-period validation • Common supported opportunities • Equal trip weights • No calibration', fontsize=9)
    fig.savefig(folder / 'delay_probability_quality.png', dpi=160)
    plt.close(fig)


if __name__ == '__main__':
    main()
