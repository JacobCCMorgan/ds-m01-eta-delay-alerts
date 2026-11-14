"""Render retained July assessment metrics without fitting or selecting methods."""
import argparse
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--report',type=Path,default=Path('reports/m3d'))
    a=p.parse_args()
    metrics=json.loads((a.report/'metrics.json').read_text())
    eta=metrics['eta']['assessment']['overall']
    prob=metrics['probability']['assessment']['overall']
    colors=['#426B8A','#BA6D3C']
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'axes.spines.top':False,'axes.spines.right':False})
    fig,axs=plt.subplots(2,2,figsize=(12,8.5))
    x=np.arange(2);width=.32
    for j,variant in enumerate(('raw','calibrated')):
        e=eta[variant]['metrics']; m=prob[variant]['metrics']
        offset=(j-.5)*width
        vals=[100*e['trip_weighted_snapshot_coverage'],100*e['trip_all_eligible_snapshots_coverage']]
        bars=axs[0,0].bar(x+offset,vals,width,color=colors[j],label=variant.title())
        axs[0,0].bar_label(bars,labels=[f'{v:.1f}%' for v in vals],padding=3,fontsize=9)
        vals=[e['mean_width_seconds']/60,e['mean_interval_score_seconds']/60]
        bars=axs[0,1].bar(x+offset,vals,width,color=colors[j])
        axs[0,1].bar_label(bars,labels=[f'{v:.1f}' for v in vals],padding=3,fontsize=9)
        vals=[m['brier_score'],m['log_loss']]
        bars=axs[1,0].bar(x+offset,vals,width,color=colors[j])
        axs[1,0].bar_label(bars,labels=[f'{v:.3f}' for v in vals],padding=3,fontsize=9)
        bins=[b for b in prob[variant]['reliability'] if b['snapshots']]
        axs[1,1].plot([b['mean_probability'] for b in bins],[b['observed_event_rate'] for b in bins],
                       marker='o',markersize=4,color=colors[j],label=f'{variant.title()} (ECE {m["fixed_bin_ece"]:.3f})')
    axs[0,0].axhline(80,color='#777777',ls='--',lw=1)
    axs[0,0].text(.98,.82,'80% nominal trip target',transform=axs[0,0].transAxes,ha='right',fontsize=9,color='#666666')
    axs[0,0].set(xticks=x,xticklabels=['Trip-weighted snapshots','All snapshots in a trip'],ylim=(0,107),ylabel='Coverage (%)',title='Coverage improves; whole-trip target remains unmet')
    axs[0,0].legend(loc='upper right',frameon=False,ncol=2,fontsize=9)
    axs[0,1].set(xticks=x,xticklabels=['Mean interval width','Mean interval score'],ylim=(0,115),ylabel='Minutes',title='Wider bounds; interval score worsens (lower is better)')
    axs[1,0].set(xticks=x,xticklabels=['Brier score','Log loss'],ylim=(0,.76),title='Probability losses increase after calibration',ylabel='Loss (lower is better)')
    axs[1,1].plot([0,1],[0,1],ls='--',color='#999999',lw=1)
    axs[1,1].set(xlim=(0,1),ylim=(0,1.04),xlabel='Mean predicted delay probability',ylabel='Observed delay fraction',title='Fixed-bin reliability (descriptive, sparse bins)')
    axs[1,1].legend(loc='upper left',frameon=False,fontsize=9)
    for ax in axs.flat:
        ax.set_axisbelow(True);ax.grid(axis='y',alpha=.16)
    fig.suptitle('July calibration assessment',x=.07,ha='left',fontsize=20,fontweight='bold')
    fig.text(.07,.924,'Fixed protocol: no method switch or parameter change after viewing assessment outcomes.',fontsize=10,color='#444444')
    fig.text(.07,.032,'Synthetic assessment: 143 mature snapshots / 29 scored trips; 341 opportunities / 42 trips retained.\nWhole-trip coverage: 9/29 to 19/29. Median ETA MAE stays 17.23 min. August–September final test remains protected.',fontsize=9,color='#444444')
    fig.subplots_adjust(left=.07,right=.98,bottom=.14,top=.87,hspace=.43,wspace=.25)
    fig.savefig(a.report/'calibration_quality.png',dpi=160,facecolor='white')
    plt.close(fig)

if __name__=='__main__':main()
