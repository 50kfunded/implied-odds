"""I generate a separate method illustration without creating research database rows."""

from dataclasses import asdict
from datetime import datetime, timezone
import math

import numpy as np

from .baseline_lock import LockedBaselines
from .baselines import atm_baseline, garch_baseline, realised_baseline
from .density import CDFSettings, ImpliedCDF
from .fitting import svi_settings
from .locking import LockedProtocol
from .report import report_versions
from .report_stats import ReportObservation, report_statistics, tail_events
from .scoring import FrozenCDF, ScoreSettings, expiry_class, score_distribution
from .svi import SVIParams, fit_svi, total_variance


def synthetic_report(parent: LockedProtocol, extension: LockedBaselines, git_commit: str,
                     *, n: int = 240, seed: int = 314159) -> dict:
    extension.validate(parent)
    if type(n) is not int or n < 1 or type(seed) is not int or seed < 0:
        raise ValueError('I need a positive synthetic sample size and nonnegative seed')
    choices = parent.protocol.content['evaluation']
    lead = choices['primary_lead_ms']
    maturity = lead/(365*86400000)
    forward = 80000.
    original = SVIParams(.0002,.0015,-.35,.03,.25)
    k = np.linspace(-.12,.12,17)
    mid = np.sqrt(total_variance(k,original)/maturity)
    fit = fit_svi(k,mid-.003,mid+.003,mid,maturity,svi_settings(parent.protocol))
    if fit.status != 'ok':
        raise ValueError(f'Synthetic smile did not qualify: {fit.status}')
    model = ImpliedCDF(fit.params,forward,maturity,float(k.min()),float(k.max()),CDFSettings(**parent.protocol.content['cdf']))
    frozen = FrozenCDF.for_model(model)
    prediction = {'forward_usd':forward,'t_years':maturity,'params':asdict(fit.params),
                  'scoring_support':frozen.support_record()}
    rng = np.random.default_rng(seed)
    variance,residual = .0001,0.
    returns = []
    for _ in range(extension.settings.garch_window_hours):
        variance = .000002+.08*residual**2+.9*variance
        residual = math.sqrt(variance)*rng.standard_t(8)*math.sqrt(6/8)
        returns.append(residual)
    history = np.asarray(returns)
    models = {'svi':frozen,'same_snapshot_atm_lognormal':atm_baseline(prediction),
              'trailing_realised_lognormal':realised_baseline(prediction,history[-extension.settings.realised_window_hours:]),
              'garch11_student_t':garch_baseline(prediction,history,extension.settings)}
    true_variance = .55**2*maturity
    outcomes = forward*np.exp(-true_variance/2+math.sqrt(true_variance)*rng.normal(size=n))
    first = int(datetime(2027,1,1,8,tzinfo=timezone.utc).timestamp()*1000)
    observations = []
    scoring = ScoreSettings(**parent.protocol.content['scoring'])
    for index, outcome in enumerate(outcomes):
        expiry = first+index*86400000
        for name,distribution in models.items():
            score = score_distribution(distribution,float(outcome),expiry,scoring)
            if score.status != 'ok':
                raise ValueError(f'Synthetic score did not resolve: {name} {score.status}')
            diagnostics = dict(score.diagnostics)
            if name == 'garch11_student_t':
                diagnostics['simulation_approximation'] = distribution.parameters['log_loss_warning']
            observations.append(ReportObservation(f'synthetic-{index:04d}',expiry,lead,expiry_class(expiry),name,
                score.metrics,diagnostics,tail_events(distribution,float(outcome))))
    chart = {'prediction_id':'synthetic-0000','snapshot_id':'generated; no captured snapshot', 'expiry_ms':first,
             'lead_ms':lead,**prediction,'distribution':model.to_record(),
             'k':k.tolist(),'iv_bid':(mid-.003).tolist(),'iv_ask':(mid+.003).tolist(),'delivery_usd':float(outcomes[0]),
             'baselines':{name:distribution.to_record() for name,distribution in models.items() if name != 'svi'}}
    statistics = report_statistics(observations,choices,parent.protocol.content['scoring']['pit_transform_epsilon'])
    return {'schema_version':1,'synthetic':True,'dataset_label':'Generated independent lognormal outcomes; method illustration only',
            'status':statistics['status'],'as_of_ms':first+(n-1)*86400000,'protocol_hash':parent.protocol.content_hash,
            'extension_hash':extension.content_hash,'provenance':{'git_commit':git_commit,'versions':report_versions(),
                'generator_seed':seed,'n_generated_outcomes':n,'physical_lognormal_annual_volatility':.55},
            'n_svi_scores':n,'n_baseline_scores':3*n,'capture':{'n_quotes':0,'reason_counts':{}},
            'statistics':statistics,'chart':chart,'observations':[asdict(r) for r in observations],
            'checklist':{'stored_research_forecasts_created':False,'market_outcomes_used':False,
                         'note':'I generated fixed-distribution examples. I have not run a rolling market study.'}}
