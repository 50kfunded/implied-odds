"""I check readonly reporting and deterministic reproduction without network access."""

from copy import deepcopy
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import shutil
from unittest.mock import patch

from implied_calibration.cli import main
from implied_calibration.report import build_report, write_report
from implied_calibration.report_plots import report_figures
from implied_calibration.reproduction import reproduce, verify_sample
from implied_calibration.synthetic_report import synthetic_report
from test_phase2 import DatabaseFixture
from test_phase6 import COMMIT, DAY, EXPIRY, locked_fixture
from test_phase7 import extension_fixture
import test_phase6 as phase6
import test_phase7 as phase7


class ReportPipelineTests(DatabaseFixture):
    seed_chain=phase6.FrozenForecastTests.seed_chain
    seed_history=phase7.FrozenBaselineTests.seed_history
    freeze_all=phase7.FrozenBaselineTests.freeze_all
    settle_score=phase7.FrozenBaselineTests.settle_score

    def report(self,cutoff=None):
        return build_report(self.store.path,locked_fixture(),extension_fixture(),EXPIRY+300 if cutoff is None else cutoff,COMMIT)

    def test_report_reads_hash_linked_forecasts_and_exact_snapshot_without_writes(self):
        identity=self.seed_chain()
        self.seed_history()
        self.freeze_all()
        self.settle_score()
        before=hashlib.sha256(self.store.path.read_bytes()).hexdigest()
        result=self.report()
        self.assertEqual((result['n_svi_scores'],result['n_baseline_scores']),(1,3))
        self.assertEqual(result['chart']['snapshot_id'],identity)
        self.assertEqual(result['chart']['delivery_usd'],101.)
        self.assertEqual(len(result['chart']['baselines']),3)
        self.assertEqual(result['status'],'insufficient data')
        self.assertEqual(hashlib.sha256(self.store.path.read_bytes()).hexdigest(),before)
        self.assertTrue(result['checklist']['quote_reasons_counted'])

    def test_future_quotes_scores_and_issues_do_not_change_earlier_report(self):
        self.seed_chain()
        self.freeze_all()
        cutoff=EXPIRY-DAY+100
        before=self.report(cutoff)
        self.seed_chain(cutoff+1)
        self.settle_score()
        self.store.add_issue('future',cutoff+1,'future_reason','generated later')
        self.store.connection.commit()
        after=self.report(cutoff)
        self.assertEqual(before,after)

    def test_empty_report_renders_all_six_panels_and_reports_expected_cli_exit(self):
        result=self.report()
        output=self.root/'report'
        written=write_report(result,output)
        self.assertEqual(len(written['report_files']),8)
        self.assertTrue(all((output/name).is_file() for name in written['report_files']))
        html=(output/'report.html').read_text(encoding='utf-8')
        self.assertIn('insufficient data',html)
        with patch('sys.argv',['cli','report','--db',str(self.store.path),'--output',str(self.root/'cli-report'),'--as-of-ms',str(EXPIRY)]), \
             patch('implied_calibration.cli.load_locked_protocol',return_value=locked_fixture()), \
             patch('implied_calibration.cli.load_locked_baselines',return_value=extension_fixture()),redirect_stdout(io.StringIO()):
            self.assertEqual(main(),2)

    def test_figures_label_synthetic_inputs_and_do_not_treat_statistics_as_calibration(self):
        data=synthetic_report(locked_fixture(),extension_fixture(),COMMIT,n=8)
        figures=report_figures(data)
        try:
            self.assertEqual(len(figures),6)
            self.assertTrue(all(title.startswith('Synthetic illustration:') for title,_ in figures.values()))
            self.assertEqual(figures['cdf-baselines'][1].axes[0].get_ylabel(),'Cumulative probability below price')
            self.assertTrue(any('iid reference' in item.get_text() for item in figures['pit'][1].texts))
        finally:
            for _,figure in figures.values():
                figure.clear()
        self.assertFalse(data['checklist']['stored_research_forecasts_created'])

    def test_report_artifacts_are_byte_identical_on_repeated_writes(self):
        data=self.report()
        first,second=self.root/'first',self.root/'second'
        self.assertEqual(write_report(data,first),write_report(deepcopy(data),second))
        self.assertEqual({p.name:p.read_bytes() for p in first.iterdir()},{p.name:p.read_bytes() for p in second.iterdir()})

    def test_hash_mismatch_is_rejected_before_reporting(self):
        self.seed_chain()
        self.freeze_all()
        self.settle_score()
        from implied_calibration.report import _records
        def changed(connection,table,cutoff):
            rows=_records(connection,table,cutoff)
            if table=='baseline_scores':
                rows[0]['svi_score_content_hash']='0'*64
            return rows
        with patch('implied_calibration.report._records',side_effect=changed),self.assertRaisesRegex(ValueError,'provenance'):
            self.report()

    def test_unscored_baselines_are_validated_before_the_cdf_comparison(self):
        self.seed_chain()
        self.freeze_all()
        from implied_calibration.report import _records
        def changed(connection,table,cutoff):
            rows=_records(connection,table,cutoff)
            if table=='baseline_predictions':
                next(r for r in rows if r['status']=='ok')['prediction_content_hash']='0'*64
            return rows
        with patch('implied_calibration.report._records',side_effect=changed),self.assertRaisesRegex(ValueError,'provenance'):
            self.report()


class ReproductionTests(DatabaseFixture):
    seed_chain=phase6.FrozenForecastTests.seed_chain

    def test_included_market_sample_retains_its_manifest_bytes_on_this_platform(self):
        path=Path(__file__).resolve().parents[1]/'recordings'/'manifest.json'
        manifest=verify_sample(path)
        self.assertEqual(sum(info['bytes'] for info in manifest['files'].values()),420802)
        self.assertEqual(manifest['files']['smoke-01/raw/2026-10-16.jsonl.gz']['sha256'],
                         '6a399d0bccd85d81c25070cae593de4ac111859f67e2bb68bebd4cc49df51f77')

    def sample(self):
        self.seed_chain()
        sample=self.root/'recordings'
        shutil.copytree(self.recording,sample/'smoke-01')
        files={p.relative_to(sample).as_posix():{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
               for p in (sample/'smoke-01').rglob('*') if p.is_file()}
        manifest={'schema_version':1,'kind':'public_market_capture','description':'I use generated raw responses for this test.',
                  'as_of_ms':EXPIRY-DAY,'files':files}
        path=sample/'manifest.json'
        path.write_text(json.dumps(manifest)+'\n',encoding='utf-8',newline='\n')
        return path

    def test_rebuild_from_verified_recording_is_deterministic_and_leaves_no_database(self):
        path=self.sample()
        a,b=self.root/'a',self.root/'b'
        first=reproduce(a,manifest_path=path,include_synthetic=False)
        second=reproduce(b,manifest_path=path,include_synthetic=False)
        self.assertEqual(first,second)
        self.assertEqual(first['reports']['market']['status'],'insufficient data')
        self.assertFalse(list(a.rglob('*.sqlite*')))
        self.assertEqual((a/'reproduction.json').read_bytes(),(b/'reproduction.json').read_bytes())

    def test_changed_and_unlisted_sample_files_are_rejected(self):
        path=self.sample()
        self.assertTrue(verify_sample(path))
        for name in ('raw/extra.jsonl.gz','heartbeat/extra.jsonl'):
            extra=path.parent/'smoke-01'/name
            extra.parent.mkdir(parents=True,exist_ok=True)
            extra.write_bytes(b'extra')
            with self.assertRaisesRegex(ValueError,'unlisted'):
                verify_sample(path)
            extra.unlink()
        selected=next((path.parent/'smoke-01').rglob('*.jsonl.gz'))
        selected.write_bytes(selected.read_bytes()+b'changed')
        with self.assertRaisesRegex(ValueError,'differs'):
            verify_sample(path)

    def test_sample_paths_cannot_escape_or_become_report_output(self):
        path=self.sample()
        with self.assertRaisesRegex(ValueError,'outside'):
            reproduce(path.parent/'reports',manifest_path=path,include_synthetic=False)
        manifest=json.loads(path.read_text(encoding='utf-8'))
        manifest['files']={'../outside.gz':{'sha256':'0'*64,'bytes':1}}
        path.write_text(json.dumps(manifest),encoding='utf-8',newline='\n')
        with self.assertRaisesRegex(ValueError,'within'):
            verify_sample(path)
