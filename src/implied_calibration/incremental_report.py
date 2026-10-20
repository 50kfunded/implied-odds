"""I rebuild the exploratory HAR comparison from retained public inputs."""

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
    fig, axes = plt.subplots(2,1,figsize=(9,6.2),gridspec_kw={'height_ratios':[1,1.3]},constrained_layout=True)
    models = ['trailing','dvol','har','har_dvol']
    means = [result['summary']['metrics']['qlike_'+name]['mean'] for name in models]
    axes[0].bar(['Trailing','DVOL','HAR','HAR + DVOL'],means,color=['#8996a2','#7b8994','#267c91','#d18135'],width=.6)
    axes[0].set_ylabel('Mean QLIKE; lower is better')
    axes[0].set_title(f"Real BTC history: {result['n']:,} matched forecasts, 2023–26",loc='left',fontweight='bold')
    for i,value in enumerate(means):
        axes[0].text(i,value,f'{value:.4f}',ha='center',va='bottom')
    for model,base,label in [('har_dvol','har','HAR + DVOL minus HAR'),('dvol','trailing','DVOL minus trailing')]:
        differences = np.array([r['losses'][model]-r['losses'][base] for r in records])
        axes[1].plot(dates,np.cumsum(differences),label=label,lw=1.6)
    axes[1].axhline(0,color='#888888',lw=.7)
    axes[1].set_ylabel('Cumulative paired QLIKE difference')
    axes[1].xaxis.set_major_locator(mdates.YearLocator())
    axes[1].xaxis.set_major_formatter(mdates.DateFormatter('%Y'))
    axes[1].legend(frameon=False,loc='best')
    axes[1].set_title('Negative cumulative differences favour the first forecast',loc='left',fontsize=10)
    for ax in axes:
        ax.spines[['top','right']].set_visible(False)
        ax.grid(axis='y',alpha=.18)
        ax.set_axisbelow(True)
    fig.suptitle('Does DVOL add information beyond HAR?',x=.02,ha='left',fontweight='bold',fontsize=15)
    fig.supxlabel('Exploratory historical comparison. Original source gate remains failed.\nI make no formal superiority claim.',fontsize=9)
    fig.savefig(output/'incremental-variance.svg',metadata={'Date':None})
    fig.savefig(output/'incremental-variance.png',dpi=180,metadata={'Software':'implied-odds'})
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
    result['audited_close_rule'] = 'I preserve the original all-OHLC failure; the three audited DVOL closes agree, with limited date coverage.'
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
