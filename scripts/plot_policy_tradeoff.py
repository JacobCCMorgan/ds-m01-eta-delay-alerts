"""Plot retained July policy-development trials without rerunning selection.

Example: python scripts/plot_policy_tradeoff.py --report reports/p06 \
    --output reports/p06/policy_tradeoff.png
"""
import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import MultipleLocator, PercentFormatter


def load_trials(report):
    trials = json.loads((report/'policy_trials.json').read_text())
    enabled = sorted((row for row in trials if row['rule']['enabled']),
                     key=lambda row: float(row['rule']['threshold']))
    disabled = [row for row in trials if not row['rule']['enabled']]
    selected = [row for row in trials if row['selected']]
    manifest = json.loads((report/'selection_manifest.json').read_text())
    if (len(enabled)!=21 or len(disabled)!=1 or len(selected)!=1
            or [float(row['rule']['threshold']) for row in enabled] != [i/20 for i in range(21)]
            or manifest['selected_trial_id'] != selected[0]['trial_id']
            or manifest['trials_retained'] != len(trials)):
        raise ValueError('Expected the complete frozen grid, one disabled reference and one retained selection')
    populations = {(row['metrics']['counts']['monitored_trips'],
                    row['metrics']['counts']['actionable_positive_trips'],
                    row['metrics']['counts']['opportunities'],
                    row['metrics']['counts']['scored_trips']) for row in trials}
    if len(populations)!=1 or any(row['metrics']['independent_policy_test'] is not False
                                 or row['metrics']['final_test_evaluated'] is not False for row in trials):
        raise ValueError('Trial denominators or July development scope differ')
    for row in trials:
        counts, values = row['metrics']['counts'],row['metrics']['metrics']
        if (abs(values['all_candidates_per_100_monitored_trips']-100*counts['all_admitted_candidates']/counts['monitored_trips'])>1e-10
                or abs(values['actionable_trip_recall']-counts['confirmed_actionable_true_alert_trips']/counts['actionable_positive_trips'])>1e-10):
            raise ValueError('Stored metrics do not match declared count denominators')
    return enabled,disabled[0],selected[0],manifest,populations.pop()


def draw(report, output):
    enabled,disabled,selected,manifest,population = load_trials(report)
    monitored,actionable,opportunities,scored = population
    thresholds = [float(row['rule']['threshold']) for row in enabled]
    burden = [row['metrics']['metrics']['all_candidates_per_100_monitored_trips'] for row in enabled]
    recall = [100*row['metrics']['metrics']['actionable_trip_recall'] for row in enabled]
    budget = manifest['budget_max_candidates_per_100_monitored_trips']
    selected_counts = selected['metrics']['counts']
    selected_threshold = float(selected['rule']['threshold']) if selected['rule']['enabled'] else None
    navy,green,orange,gray = '#50677D','#15736D','#C45B25','#8C949D'
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'axes.spines.top':False,
                         'axes.spines.right':False,'axes.labelcolor':'#334155','text.color':'#203047',
                         'xtick.color':'#475569','ytick.color':'#475569'})
    fig,axes = plt.subplots(1,2,figsize=(12.6,7.2))
    for ax,values in zip(axes,(burden,recall)):
        ax.plot(thresholds,values,color=navy,lw=1.5,zorder=2)
        for selectable,marker,color in ((True,'o',green),(False,'x',gray)):
            positions = [i for i,row in enumerate(enabled) if row['feasibility']['selectable']==selectable]
            ax.scatter([thresholds[i] for i in positions],[values[i] for i in positions],
                       marker=marker,color=color,s=34,zorder=3,linewidths=1.3)
        if selected_threshold is not None:
            index=thresholds.index(selected_threshold)
            ax.axvline(selected_threshold,color=orange,lw=1,ls=(0,(3,4)),alpha=.75)
            ax.scatter([selected_threshold],[values[index]],s=125,marker='D',color=orange,
                       edgecolor='white',linewidth=1.5,zorder=5)
        ax.set(xlim=(-.025,1.025),xlabel='Canonical probability threshold')
        ax.xaxis.set_major_locator(MultipleLocator(.1))
        ax.set_axisbelow(True)
        ax.grid(axis='y',color='#CBD5E1',alpha=.65,lw=.6)
        ax.tick_params(axis='both',length=3)
    axes[0].axhline(budget,color='#64748B',lw=1.2,ls='--')
    axes[0].text(.98,budget+1.4,f'Research budget: {budget:g} / 100 trips',ha='right',
                 color='#64748B',fontsize=9.5)
    axes[0].set(title='All-candidate burden',ylabel=f'Candidates per 100 monitored trips (n = {monitored})',
                ylim=(-1,max(budget*1.18,max(burden)*1.15)))
    axes[0].yaxis.set_major_locator(MultipleLocator(10))
    axes[1].set(title='Actionable trip recall',
                ylabel=f'Confirmed true-alert trips / {actionable} actionable trips (%)',
                ylim=(-1,max(30,max(recall)*1.35)))
    axes[1].yaxis.set_major_formatter(PercentFormatter(xmax=100,decimals=0))
    axes[1].yaxis.set_major_locator(MultipleLocator(5))
    for ax in axes:
        ax.title.set_fontsize(13)
        ax.title.set_fontweight('bold')
        ax.title.set_position((.5,1.04))
    if selected_threshold is not None:
        chosen_burden = selected['metrics']['metrics']['all_candidates_per_100_monitored_trips']
        chosen_recall = 100*selected['metrics']['metrics']['actionable_trip_recall']
        axes[0].annotate(f"Selected {selected_threshold:.2f}\n{selected_counts['all_admitted_candidates']} candidates / {monitored} trips\n{chosen_burden:.1f} per 100 trips",
                         xy=(selected_threshold,chosen_burden),xytext=(.63,35),fontsize=9.5,color=orange,
                         arrowprops={'arrowstyle':'-','color':orange,'lw':1},ha='left',va='center')
        axes[1].annotate(f"Selected {selected_threshold:.2f}\n{selected_counts['confirmed_actionable_true_alert_trips']}/{actionable} trips ({chosen_recall:.1f}%)",
                         xy=(selected_threshold,chosen_recall),xytext=(.63,26),fontsize=9.5,color=orange,
                         arrowprops={'arrowstyle':'-','color':orange,'lw':1},ha='left',va='center')
    else:
        fig.text(.075,.795,'The retained selection disables candidate generation.',fontsize=10,color=orange)
    handles = [Line2D([0],[0],marker='o',ls='',color=green,label='Selectable under frozen gates',markersize=6),
               Line2D([0],[0],marker='x',ls='',color=gray,label='Not selectable',markersize=6),
               Line2D([0],[0],marker='D',ls='',color=orange,label='Retained selection',markersize=7)]
    fig.suptitle('July policy-development tradeoff',x=.075,y=.967,ha='left',fontsize=20,fontweight='bold')
    fig.text(.075,.905,'Synthetic research: 21 frozen thresholds + one disabled reference. Candidates are not delivered notifications.',fontsize=10,color='#475569')
    fig.legend(handles=handles,loc='upper left',bbox_to_anchor=(.065,.875),ncol=3,frameon=False,fontsize=9.5)
    disabled_counts = disabled['metrics']['counts']
    fig.text(.075,.185,f"Disabled reference (separate trial): {disabled_counts['all_admitted_candidates']} candidates; "
             f"{disabled_counts['confirmed_actionable_true_alert_trips']}/{actionable} actionable trips captured.",fontsize=10,color='#475569')
    fig.text(.075,.14,'Selection order: most actionable true-alert trips, then fewest candidates, then higher threshold.',fontsize=9.5,color='#475569')
    fig.text(.075,.10,f'{opportunities} opportunities / {monitored} monitored trips; {scored} mature-label trips. '
             f'Online cap: {manifest["daily_candidate_cap_per_tenant"]} candidates per tenant/local day.',fontsize=9.5,color='#475569')
    fig.text(.075,.06,'July assessment is reused for policy development. These are not independent policy-test or operational-acceptance results.',fontsize=9.5,color='#475569')
    fig.subplots_adjust(left=.075,right=.97,bottom=.30,top=.765,wspace=.30)
    output.parent.mkdir(parents=True,exist_ok=True)
    fig.savefig(output,dpi=180,facecolor='white',metadata={'Software':'DS-M01 plot_policy_tradeoff.py'})
    plt.close(fig)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,default=Path('reports/p06'))
    parser.add_argument('--output',type=Path,default=Path('reports/p06/policy_tradeoff.png'))
    args=parser.parse_args()
    draw(args.report,args.output)


if __name__=='__main__':
    main()
