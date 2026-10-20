"""I rebuild the historical experiment, figure and five-page research paper."""

import argparse
from html import escape
import json
from pathlib import Path
import shutil

import matplotlib
from reportlab import rl_config
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from implied_calibration.incremental_report import write_incremental_report


def paper(result: dict, output: Path) -> None:
    rl_config.invariant = 1
    fonts = Path(matplotlib.get_data_path())/'fonts/ttf'
    pdfmetrics.registerFont(TTFont('ResearchSans',str(fonts/'DejaVuSans.ttf')))
    pdfmetrics.registerFont(TTFont('ResearchBold',str(fonts/'DejaVuSans-Bold.ttf')))
    pdfmetrics.registerFontFamily('ResearchSans',normal='ResearchSans',bold='ResearchBold',italic='ResearchSans',boldItalic='ResearchBold')
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='Research',fontName='ResearchSans',fontSize=9.2,leading=13.6,spaceAfter=8))
    styles.add(ParagraphStyle(name='Heading',fontName='ResearchBold',fontSize=14,leading=18,spaceAfter=11,textColor=colors.HexColor('#183b50')))
    styles.add(ParagraphStyle(name='TitleResearch',fontName='ResearchBold',fontSize=23,leading=28,spaceAfter=14))
    styles.add(ParagraphStyle(name='SmallResearch',parent=styles['Research'],fontSize=8,leading=11))
    story = []
    def p(text,style='Research'):
        story.append(Paragraph(text,styles[style]))
    def table(rows,widths=None):
        data = [[Paragraph(escape(str(cell)),styles['SmallResearch']) for cell in row] for row in rows]
        item = Table(data,colWidths=widths,repeatRows=1,hAlign='LEFT')
        item.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e7eef2')),
            ('VALIGN',(0,0),(-1,-1),'TOP'),('LINEBELOW',(0,0),(-1,0),.5,colors.HexColor('#72909e')),
            ('LINEBELOW',(0,1),(-1,-1),.25,colors.HexColor('#d9e2e7')),
            ('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6)]))
        story.append(item)
        story.append(Spacer(1,10))
    def page(title):
        story.append(PageBreak())
        p(title,'Heading')
    metrics = result['summary']['metrics']
    delta = metrics['qlike_har_dvol_minus_har']
    improvement = -delta['mean']/metrics['qlike_har']['mean']*100
    interval = delta['confidence_interval_95']
    p('Does DVOL add information beyond HAR?','TitleResearch')
    p('A retrospective BTC variance forecast study | 50kfunded | 19 October 2026','SmallResearch')
    p('<b>Abstract.</b> I compare direct 30-day HAR forecasts with the same regression augmented by BTC DVOL, '
      f'using {result["n"]:,} matched daily origins from 2023 to 2026. Augmentation lowers mean QLIKE by {improvement:.2f}%, '
      f'but its paired difference is {delta["mean"]:.5f}, with descriptive 95% interval [{interval[0]:.5f}, {interval[1]:+.5f}] '
      f'and estimated ESS {delta["effective_sample_size"]:.2f}, below the fixed threshold of 60. '
      'I find no clear improvement in a fixed variance-budget decision. My inference stress tests also show that one year of overlapping '
      'daily forecasts has no demonstrated useful confirmatory power under the declared gate, so I lock the future study as evidence collection '
      'and keep formal superiority claims disabled.')
    story.append(Image(str(output/'incremental-variance.png'),width=6.55*inch,height=4.51*inch))
    p('Figure 1. I use real retained historical inputs. Both panels use the same 1,357 origins. '
      'The lower panel shows error accumulation, rather than trading profit. All historical findings remain exploratory; '
      'the original vendor all-OHLC validation gate remains failed.','SmallResearch')
    page('1. Question, inputs and matched design')
    p('I ask whether option-implied variance adds forecasting information beyond daily, weekly and monthly realised-variance components. '
      'I use Corsi\'s HAR construction as a parsimonious price-history benchmark, adapted to a direct 30-day target. '
      'I compare the benchmark with the same model plus previous completed daily DVOL variance. I also retain raw DVOL and trailing variance.')
    p('I downloaded 48,626 hourly BTC-PERPETUAL candles and 1,996 daily BTC DVOL candles on 19 October 2026. '
      'My matched evaluation uses daily midnight origins from 1 January 2023 through 18 September 2026. '
      'I use the next 720 hourly log-return squares, annualised by 8,760, as the target. '
      'Each 720-return window requires 721 completed closes. Historical downloads do not establish original unrevised vintages.')
    p('At each origin I fit both regressions using exactly the latest 365 labels whose full 30-day targets have ended. '
      'My HAR features are prior 1-, 7- and 30-day annualised variances; the augmented model adds squared DVOL/100. '
      'I use level OLS with an intercept. I centre and scale features using the training rows only. '
      'I fix a 1e-6 annual-variance floor, retain raw predictions, and count every floor hit. Neither model hits it in this sample.')
    p('I score QLIKE = realised/forecast - log(realised/forecast) - 1. I compute every comparison on identical origins. '
      'The additional historical work follows inspection of these data, so chronological training does not make this an untouched experiment.')
    table([['Forecast','Mean QLIKE','Role']]+[[name,f'{metrics["qlike_"+key]["mean"]:.5f}',role]
        for key,name,role in [('trailing','Trailing 30-day','Reference'),('dvol','Raw DVOL','Reference'),
                             ('har','HAR','Benchmark'),('har_dvol','HAR + DVOL','Augmentation')]], [160,110,195])
    p(f'I estimate an augmentation difference of {delta["mean"]:.5f}, with interval [{interval[0]:.5f}, {interval[1]:+.5f}]. '
      'The interval includes zero and the ESS gate fails. Raw DVOL has the lowest point loss, which also exposes estimation risk in the fitted augmentation.')
    p('I include level Mincer-Zarnowitz regressions with HAC uncertainty, a joint HAR calibration/encompassing restriction, '
      'and a separate incremental-coefficient restriction with the HAR slope and intercept unrestricted. These generated-forecast diagnostics '
      'are exploratory; I do not treat their p-values as additional primary discoveries. Coefficients and uncertainty are in summary.json.')
    page('2. Loss concentration and a concrete decision')
    concentration = result['concentration']['har_dvol']
    top = concentration['top_absolute_contributions']
    p(f'I find that the largest 1% and 5% of absolute augmentation loss differences account for '
      f'{100*top["1"]["share_absolute_loss_difference"]:.1f}% and {100*top["5"]["share_absolute_loss_difference"]:.1f}% of total absolute differences. '
      'Their signed contributions are positive, so these largest observations hurt augmentation. '
      'The overall mean is still negative. This does not support a simple story that augmentation wins only by avoiding the most extreme losses.')
    table([['Year left out','Remaining origins','Mean augmentation difference']]+
          [[year,row['n'],f'{row["mean"]:.5f}'] for year,row in concentration['leave_one_year_out'].items()], [120,130,215])
    p('I retain contributions for every fixed calendar quarter, extreme-origin dates, and all 30 non-overlapping starting phases. '
      'The phase samples contain only 45 or 46 outcomes and are dependent. I use these displays to expose concentration, '
      'not to select a favourable crisis window or claim an ex-ante regime rule.')
    p('For decision relevance I fix an illustrative annual volatility budget of 20% and weight '
      'w = min(1, 0.20 / sqrt(forecast variance)). I measure the squared error '
      '(w² × realised variance - 0.04)². This assumes constant fractional exposure for the variance calculation. '
      'I do not implement a fixed-unit portfolio, rebalancing, funding or profit, and this rule is not claimed optimal for squared budget error.')
    table([['Forecast','Mean weight','Mean squared budget error','Budget exceeded']]+
          [[name,f'{result["decision"][key]["weight"]:.3f}',f'{result["decision"][key]["squared_variance_budget_error"]:.7f}',
            f'{100*result["decision"][key]["budget_exceeded"]:.1f}%'] for key,name in [('trailing','Trailing'),('dvol','DVOL'),('har','HAR'),('har_dvol','HAR + DVOL')]], [105,95,155,110])
    risk = metrics['risk_budget_har_dvol_minus_har']
    p(f'I get augmented-minus-HAR risk loss {risk["mean"]:+.7f}, with interval '
      f'[{risk["confidence_interval_95"][0]:+.7f}, {risk["confidence_interval_95"][1]:+.7f}]. '
      'Lower mean QLIKE does not establish a better decision under this loss. '
      'I report entry-plus-exit fee amounts separately at 0, 5, 10 and 20 basis points per side; I do not subtract costs from a risk error or infer trading returns.')
    page('3. Dependence and finite-sample inference')
    p('I use 5,000 paired stationary-bootstrap draws with a fixed seed, 60-day expected blocks and a 59-lag HAC check. '
      'I retain 30-, 90-, 180- and 365-day block sensitivities. The overlapping targets and rolling fits can create dependence '
      'beyond the 30-day target horizon. ESS is a diagnostic estimate, not an observed number of independent episodes.')
    p('I simulate 200 independent samples per declared scenario at n=365 and n=1,357, using 2,000 bootstrap draws each. '
      'My population null is exactly mean zero without centring each realised sample. I use a 30-day moving-average loss component, '
      'a stationary slow component with persistence exp(-1/365), and symmetric rare shocks spread across overlapping windows. '
      'The synthetic alternative is a -0.25 marginal-standard-deviation shift, not -0.25 QLIKE. '
      'These stress tests approximate loss dependence and do not simulate the entire rolling HAR estimation pipeline.')
    nulls = [r for r in result['inference_simulations']['rows'] if r['population_mean'] == 0]
    labels = {'overlap_30':'Overlap','overlap_30_plus_slow_365':'Overlap + slow','overlap_30_plus_clustered_tails':'Overlap + tails'}
    table([['n / synthetic process','Ungated rejection','95% MC interval','ESS gate passes']]+
          [[f'{r["n"]} / {labels[r["dgp"]]}',f'{100*r["ungated_rejection_rate"]:.1f}%',
            f'{100*r["ungated_mc_interval_95"][0]:.1f}% to {100*r["ungated_mc_interval_95"][1]:.1f}%',
            f'{100*r["gate_pass_rate"]:.1f}%'] for r in nulls], [155,100,125,85])
    p('At n=365 I observe ungated null rejection rates of 16% to 49%, rather than nominal 5%. '
      'The ESS gate passes zero of 200 replications under every one-year null and alternative, giving a Wilson 95% upper bound around 1.9% '
      'for the pass probability. I therefore have no demonstrated useful confirmatory power for the planned year. '
      'At n=1,357, conditional estimates sometimes use only one to four passing replications; I retain their wide Monte Carlo intervals.')
    p('I committed a separate inference-validation amendment that disables formal superiority claims for the new HAR period. '
      'I preserve its fixed terminal date and primary contrast, collect timestamped evidence, and accept an inconclusive terminal result. '
      'A later inference design needs its own prospective declaration; I will not change this cohort to recover significance.')
    page('4. Prospective collection, limits and reproduction')
    p('I locked 365 future HAR origins from 21 October 2026 through 20 October 2027, each at 01:00 UTC. '
      'Features end at midnight; I freeze between 00:10 and 00:50, then start the outcome at 01:00. '
      'I train on the correspondingly shifted historical 30-day targets completed by midnight. '
      'This differs from the midnight target alignment in the retrospective comparison. Final maturity is 19 November 2027; '
      'I fix the terminal analysis at 21 November 2027, 01:00 UTC.')
    p('I require a matching hourly and daily DVOL close for each new forecast, preserve raw capture segments, attach code and input hashes, '
      'and check fitting and durable-write completion against the freeze deadline. I count missed scheduled days and do not backfill. '
      'I withhold terminal intervals unless all 365 planned pairs and source checks exist; late-arriving scores after the fixed analysis cutoff stay excluded.')
    p('My separate SVI study still has zero settled research forecasts. Its original protocol and baseline locks remain unchanged. '
      'A versioned amendment fixes its expiry cutoff at 20 October 2027, 08:00 UTC and final decision at 22 October 2027, 08:00 UTC. '
      'Both reporting and CLI inference keep earlier output descriptive and pin terminal data availability. '
      'I retain calendar-grid and extrapolated-tail limitations. Local forecast hash chains are not independent public timestamps.')
    p('My collector retries recoverable delivery pagination in fresh segments and keeps conflicting immutable prices fatal. '
      'I cache cleaning by input, protocol, code and runtime fingerprints, and render reports outside the forecast loop. '
      'I publish counts, rejection reasons, local hashes and heartbeat age. A fresh heartbeat does not prove process health. '
      'Continuous collection needs an awake online host; short validation recordings do not establish sustained operation.')
    p('The original all-OHLC audit remains failed because one unused DVOL open differs. All three audited DVOL closes match, '
      'but these dates do not validate the entire history. I use perpetual returns as an index proxy, a single asset and venue, '
      'retrieved vintages and overlapping outcomes. I do not identify a causal variance premium or executable edge.')
    p('<b>Reproduction.</b> I use Python 3.12 with requirements-research.txt, then run '
      '<font name="ResearchBold">python scripts/build_research.py</font>. '
      'I retain the full report, matched forecasts, coefficients, training cutoffs, diagnostics, simulation rates and plan hashes. '
      'The command reads the checked-in public recordings and makes no market requests.')
    p('<b>References.</b> Corsi (2009), A Simple Approximate Long-Memory Model of Realized Volatility, '
      '<link href="https://doi.org/10.1093/jjfinec/nbp001" color="#246477">doi:10.1093/jjfinec/nbp001</link>. '
      'Patton (2011), Volatility Forecast Comparison Using Imperfect Volatility Proxies, '
      '<link href="https://doi.org/10.1016/j.jeconom.2010.03.034" color="#246477">doi:10.1016/j.jeconom.2010.03.034</link>. '
      'Politis and Romano (1994), The Stationary Bootstrap, '
      '<link href="https://doi.org/10.1080/01621459.1994.10476870" color="#246477">doi:10.1080/01621459.1994.10476870</link>. '
      'Deribit, DVOL methodology, '
      '<link href="https://insights.deribit.com/exchange-updates/dvol-deribit-implied-volatility-index/" color="#246477">official methodology</link>.','SmallResearch')
    def footer(canvas,doc):
        canvas.setFont('ResearchSans',8)
        canvas.setFillColor(colors.HexColor('#607582'))
        canvas.drawString(44,25,'implied-odds | exploratory historical evidence')
        canvas.drawRightString(A4[0]-44,25,str(doc.page))
    doc = SimpleDocTemplate(str(output/'research-paper.pdf'),pagesize=A4,rightMargin=44,leftMargin=44,
                            topMargin=42,bottomMargin=43,title='Does DVOL add information beyond HAR?',author='50kfunded',pageCompression=1)
    doc.build(story,onFirstPage=footer,onLaterPages=footer)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=Path('results/research'))
    parser.add_argument('--publish',action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    result = write_incremental_report(root,args.output)
    paper(result,args.output)
    if args.publish:
        for name in ('research-paper.pdf','incremental-variance.svg'):
            shutil.copyfile(args.output/name,root/'report'/name)
        shutil.copyfile(args.output/'summary.json',root/'report/incremental-results.json')
    print(json.dumps({'n':result['n'],'paper':str(args.output/'research-paper.pdf'),'formal_claim_eligible':False}))


if __name__ == '__main__':
    main()
