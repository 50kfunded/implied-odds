"""Scientific report panels display missing and synthetic results explicitly."""

from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from .baselines import BaselineCDF
from .scoring import FrozenCDF
from .svi import SVIParams, total_variance


def report_figures(data: dict) -> dict[str, tuple[str, Figure]]:
    prefix = 'Synthetic illustration: ' if data['synthetic'] else ''
    chart, stats = data['chart'], data['statistics']
    figures = {}

    def panel(name: str, title: str, note: str = '', height: float = 5.4):
        figure = Figure(figsize=(9.5,height),layout='constrained',facecolor='white')
        FigureCanvasAgg(figure)
        axes = figure.subplots()
        axes.set_title(prefix+title,loc='left',fontweight='bold',pad=16)
        axes.spines[['top','right']].set_visible(False)
        axes.grid(alpha=.18)
        if note:
            figure.supxlabel(note,fontsize=8,color='#435d6f')
        figures[name] = (prefix+title,figure)
        return axes

    def missing(axes, reason: str):
        axes.set_axis_off()
        axes.text(.5,.55,'insufficient data',ha='center',va='center',fontsize=22,transform=axes.transAxes,color='#435d6f')
        axes.text(.5,.4,reason,ha='center',va='center',fontsize=11,transform=axes.transAxes,wrap=True)

    axes = panel('smile','Smile against bid and ask','')
    if chart and chart['k']:
        k,bid,ask = (np.asarray(chart[name]) for name in ('k','iv_bid','iv_ask'))
        order = np.argsort(k)
        axes.scatter(k,bid*100,marker='v',s=22,label='Bid IV')
        axes.scatter(k,ask*100,marker='^',s=22,label='Ask IV')
        axes.fill_between(k[order],bid[order]*100,ask[order]*100,alpha=.15)
        dense = np.linspace(float(k.min()),float(k.max()),501)
        axes.plot(dense,100*np.sqrt(total_variance(dense,SVIParams(**chart['params']))/chart['t_years']),label='Frozen SVI')
        axes.set(xlabel='Log-moneyness ln(K/F)',ylabel='Implied volatility, percent')
        axes.legend()
    else:
        missing(axes,'An accepted frozen forecast and its quote observations are required.')

    axes = panel('cdf-baselines','Implied CDF against baselines','')
    if chart:
        svi = FrozenCDF.from_record(chart)
        models = {'SVI risk-neutral':svi,**{name:BaselineCDF(**record) for name,record in sorted(chart['baselines'].items())}}
        lower = min(model.quantile(.005) for model in models.values())
        upper = max(model.quantile(.995) for model in models.values())
        prices = np.geomspace(lower,upper,1001)
        for name,model in models.items():
            axes.plot(prices,model.probabilities(prices)[0],label=name.replace('_',' '))
        observed_lower = svi.model.forward_usd*np.exp(svi.model.observed_k_min)
        observed_upper = svi.model.forward_usd*np.exp(svi.model.observed_k_max)
        if lower < observed_lower:
            axes.axvspan(lower,min(upper,observed_lower),alpha=.13,color='#c98e36')
        if upper > observed_upper:
            axes.axvspan(max(lower,observed_upper),upper,alpha=.13,color='#c98e36')
        if chart.get('delivery_usd') is not None:
            axes.axvline(chart['delivery_usd'],color='#535f68',linestyle=':',label='Official delivery' if not data['synthetic'] else 'Generated outcome')
        axes.set(xlabel='Delivery price, USD',ylabel='Cumulative probability below price',ylim=(0,1),xlim=(lower,upper))
        axes.legend(fontsize=8)
    else:
        missing(axes,'An accepted frozen forecast is required for the CDF comparison.')

    hist = stats['pit']
    axes = panel('pit','PIT histogram','95% binomial iid reference range')
    if hist['n']:
        lower,upper = hist['reference_count_range_95']
        axes.axhspan(lower,upper,color='#d8b16a',alpha=.3,label='95% binomial iid reference range')
        axes.bar(hist['edges'][:-1],hist['counts'],width=.1,align='edge',edgecolor='white',color='#337b9b',alpha=.8)
        axes.axhline(hist['n']/10,color='#5e6d74',linestyle=':',label='Uniform iid expected count')
        axes.set(xlabel='Raw PIT',ylabel='Count',xlim=(0,1))
        state = 'ESS gate passed; descriptive' if hist['status'] == 'ok' else 'insufficient data'
        axes.text(.02,.96,f"n={hist['n']}; {state}",transform=axes.transAxes,va='top')
        axes.legend(fontsize=8,loc='upper right')
    else:
        missing(axes,'Scored frozen forecasts and official delivery observations are required.')

    axes = panel('reliability','Tail reliability','')
    if any(row['n'] for row in stats['reliability']):
        axes.plot([0,.25],[0,.25],linestyle=':',color='#657984',label='Nominal reference')
        for side,color in (('left','#267797'),('right','#b26a35')):
            rows = [row for row in stats['reliability'] if row['side']==side]
            axes.plot([r['nominal_probability'] for r in rows],[r['frequency'] for r in rows],marker='o',color=color,label=side.title()+' tail')
            for row in rows:
                if row['confidence_interval_95']:
                    axes.vlines(row['nominal_probability'],*row['confidence_interval_95'],color=color,linewidth=2)
        low = sum(row['status']=='insufficient data' for row in stats['reliability'])
        axes.text(.02,.97,f'{low} of 8 tail estimates: insufficient data' if low else 'Descriptive estimates only',transform=axes.transAxes,va='top')
        upper = max(r['confidence_interval_95'][1] if r['confidence_interval_95'] else r['frequency'] for r in stats['reliability'])
        axes.set(xlabel='Nominal risk-neutral tail probability',ylabel='Realised frequency',xlim=(0,.27),ylim=(0,min(1.,max(.3,upper+.03))))
        axes.legend(loc='lower right',fontsize=8)
    else:
        missing(axes,'Realised outcomes paired with frozen tail probabilities are required.')

    temporal = stats['crps']
    axes = panel('crps-differences','Cumulative paired CRPS difference','')
    if temporal['points']:
        points = temporal['points']
        times = [datetime.fromtimestamp(p['expiry_ms']/1000,timezone.utc) for p in points]
        axes.axhline(0,color='#74818a',linestyle=':')
        axes.plot(times,[p['mean_difference_usd'] for p in points],color='#267797',label='SVI minus ATM lognormal')
        lower = [p['confidence_interval_95'][0] if p['confidence_interval_95'] else np.nan for p in points]
        upper = [p['confidence_interval_95'][1] if p['confidence_interval_95'] else np.nan for p in points]
        axes.fill_between(times,lower,upper,alpha=.22,color='#267797',label='95% pointwise bootstrap interval')
        state = 'ESS gate passed; pointwise intervals' if temporal['status'] == 'ok' else 'insufficient data'
        axes.text(.02,.97,state,transform=axes.transAxes,va='top')
        axes.set(xlabel='Expiry, UTC',ylabel='Mean CRPS difference, USD')
        axes.legend(fontsize=8,loc='lower left')
        axes.tick_params(axis='x',labelrotation=20)
    else:
        missing(axes,'Paired SVI and ATM CRPS scores at the declared primary lead are required.')

    groups = defaultdict(dict)
    for row in stats['coverage']:
        groups[(row['model'],row['lead_ms'],row['expiry_class'])][row['level']] = row
    axes = panel('coverage','Central interval coverage','',height=max(5.4,1.9+.3*len(groups)))
    if groups:
        cells = []
        for (model,lead,classification), levels in sorted(groups.items()):
            first = levels[50]
            cells.append([model.replace('_',' '),f'{lead/3600000:g}h',classification,str(first['n']),
                *[f"{levels[level]['frequency']:.1%}" for level in (50,80,90)],
                f"{min(r['effective_sample_size'] for r in levels.values()):.1f}",
                'insufficient data' if any(r['status']=='insufficient data' for r in levels.values()) else 'descriptive'])
        axes.set_axis_off()
        table = axes.table(cellText=cells,colLabels=['Model','Lead','Class','n','50%','80%','90%','Min ESS','Status'],loc='center',cellLoc='left',
                           colWidths=[.25,.055,.08,.045,.065,.065,.065,.07,.2])
        table.auto_set_font_size(False)
        table.set_fontsize(7)
        table.scale(1,1.6)
        for (row,col),cell in table.get_celld().items():
            cell.set_edgecolor('#dbe3e8')
            cell.set_facecolor('#e5edf2' if row==0 else '#f4f7f9' if row%2 else 'white')
    else:
        missing(axes,'Realised interval coverage is unavailable.')
    return figures
