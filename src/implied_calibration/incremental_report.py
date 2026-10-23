"""Rebuild the matched HAR comparison from retained public inputs."""

import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np

from .historical_data import load_study, read_study
from .historical_report import alignment_checks
from .incremental import analyse_incremental
from .inference_study import simulation_report
from .research_plan import load_plan
from .store import digest


def headline_figure(result: dict, output: Path) -> None:
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'svg.hashsalt':'incremental-variance-v1'})
    records = result['records']
    dates = np.array([np.datetime64(r['origin_ms'],'ms') for r in records])
    fig, axes = plt.subplots(3,1,figsize=(9,7.6),gridspec_kw={'height_ratios':[1,.65,1.3]},constrained_layout=True)
    models = ['trailing','dvol','har','har_dvol']
    means = [result['summary']['metrics']['qlike_'+name]['mean'] for name in models]
    axes[0].bar(['trailing variance','Raw DVOL','HAR','HAR + DVOL'],means,color=['#8996a2','#7b8994','#267c91','#d18135'],width=.6)
    axes[0].set_ylabel('Mean QLIKE loss; lower is better')
    axes[0].set_title(f"Matched BTC forecasts, 2023–26 (n = {result['n']:,})",loc='left',pad=12)
    axes[0].set_ylim(0,max(means)*1.18)
    for i,value in enumerate(means):
        axes[0].annotate(f'{value:.5f}',(i,value),xytext=(0,4),textcoords='offset points',ha='center',va='bottom')
    comparisons = [('har_dvol','har','HAR + DVOL minus HAR','qlike_har_dvol_minus_har'),
                   ('dvol','har_dvol','Raw DVOL minus (HAR + DVOL)','qlike_dvol_minus_har_dvol')]
    for i,(model,base,label,metric_name) in enumerate(comparisons):
        metric = result['summary']['metrics'][metric_name]
        interval = metric['confidence_interval_95']
        color = f'C{i}'
        if interval is not None:
            axes[1].plot(interval,[i,i],color=color,lw=2,label=label+' interval')
        axes[1].plot(metric['mean'],i,'o',color=color)
        differences = np.array([r['losses'][model]-r['losses'][base] for r in records])
        axes[2].plot(dates,np.cumsum(differences),label=label,lw=1.6,color=color)
    axes[1].axvline(0,color='#888888',lw=.7)
    axes[1].set_yticks([0,1],[row[2] for row in comparisons])
    axes[1].set_ylim(1.6,-.6)
    axes[1].set_xlabel('Mean paired QLIKE difference, descriptive 95% interval')
    axes[2].axhline(0,color='#888888',lw=.7)
    axes[2].set_ylabel('Cumulative paired QLIKE difference')
    axes[2].xaxis.set_major_locator(mdates.YearLocator())
    axes[2].xaxis.set_major_formatter(mdates.DateFormatter('%Y'))
    axes[2].legend(frameon=False,loc='best')
    axes[2].set_title('Negative differences favour the first forecast',loc='left',fontsize=10)
    for ax in axes:
        ax.spines[['top','right']].set_visible(False)
        ax.grid(axis='y',alpha=.18)
        ax.set_axisbelow(True)
    fig.savefig(output/'incremental-variance.svg',metadata={'Date':None})
    fig.savefig(output/'incremental-variance.png',dpi=180,metadata={'Software':'implied-odds'})
    fig.savefig(output/'figure-1.pdf',metadata={'CreationDate':None,'ModDate':None})
    plt.close(fig)


def write_incremental_report(root: Path, output: Path, *, simulations: bool = True) -> dict:
    output.mkdir(parents=True,exist_ok=True)
    historical = load_study(root/'protocol/historical-variance.json')
    inputs = read_study(root/'recordings/historical-variance/manifest.json',historical)
    plan = load_plan(root/'protocol/incremental-variance-v1.json')
    result = analyse_incremental(inputs.prices,inputs.dvol,historical,plan)
    result['plan_hash'] = digest(plan)
    result['plan'] = plan
    result['original_alignment'] = alignment_checks(inputs,historical['alignment_check_days_ms'])
    result['audited_close_rule'] = 'The original all-OHLC check fails; the three audited DVOL closes agree, with limited date coverage.'
    if simulations:
        result['inference_simulations'] = simulation_report(plan)
    (output/'report.json').write_text(json.dumps(result,indent=2,sort_keys=True,allow_nan=False)+'\n',encoding='utf-8')
    summary = {k:v for k,v in result.items() if k != 'records'}
    (output/'summary.json').write_text(json.dumps(summary,indent=2,sort_keys=True,allow_nan=False)+'\n',encoding='utf-8')
    with (output/'forecasts.csv').open('w',newline='',encoding='utf-8') as handle:
        fields = ['origin_ms','target_end_ms','actual','latest_training_outcome_ms']
        fields += [prefix+name for prefix in ('forecast_','qlike_') for name in plan['models']]
        writer = csv.DictWriter(handle,fieldnames=fields)
        writer.writeheader()
        for row in result['records']:
            writer.writerow({'origin_ms':row['origin_ms'],'target_end_ms':row['target_end_ms'],'actual':row['actual'],
                'latest_training_outcome_ms':row['training']['latest_target_end_ms'],
                **{'forecast_'+k:v for k,v in row['forecasts'].items()},**{'qlike_'+k:v for k,v in row['losses'].items()}})
    headline_figure(result,output)
    return result
