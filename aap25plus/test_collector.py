import importlib.util
import unittest
import tempfile
import os
import sys
import json
from pathlib import Path
from unittest.mock import patch
spec = importlib.util.spec_from_file_location('collector', Path(__file__).with_name('collect_usage.py'))
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
S=c.stamp('2026-01-01T00:00:00Z')
E=c.stamp('2026-01-01T01:00:00Z')
def job(a,b,status='successful'):
    return dict(started=a,finished=b,status=status)
class Tests(unittest.TestCase):
    def test_tls_defaults_and_override(self):
        api=c.API('https://example.com','fake')
        handler=next(h for h in api.opener.handlers if isinstance(h,c.urllib.request.HTTPSHandler))
        self.assertEqual(handler._context.verify_mode,c.ssl.CERT_REQUIRED)
        self.assertTrue(handler._context.check_hostname)
        api=c.API('https://example.com','fake',ca_file='nonexistent.pem',validate_certs=False)
        handler=next(h for h in api.opener.handlers if isinstance(h,c.urllib.request.HTTPSHandler))
        self.assertEqual(handler._context.verify_mode,c.ssl.CERT_NONE)
        self.assertFalse(handler._context.check_hostname)

    def test_boundaries(self):
        rows=[job('2025-12-31T23:00:00Z','2026-01-01T00:30:00Z'),
              job('2026-01-01T00:30:00Z','2026-01-01T02:00:00Z')]
        self.assertEqual(c.concurrency(rows,S,E)['peak'],1)
        self.assertEqual(c.concurrency(rows,S,E)['time_weighted_mean'],1)
    def test_running_and_unstarted(self):
        rows=[job('2026-01-01T00:30:00Z',None,'running'),job(None,None,'pending'),
              job('2026-01-01T00:00:00Z',None,'error')]
        self.assertEqual(c.concurrency(rows,S,E)['time_weighted_mean'],.5)
    def test_empty_and_percentiles(self):
        self.assertEqual(c.concurrency([],S,E)['peak'],0)
        self.assertEqual(c.distribution([])['p95'],None)
        self.assertEqual(c.distribution(range(1,101))['p95'],95)
    def test_pagination(self):
        api=c.API('https://example.com','fake')
        with patch.object(api,'get',side_effect=[{'results':[{'id':1}],'next':'/api/controller/v2/jobs/?page=2'},
                                               {'results':[{'id':1},{'id':2}],'next':None}]):
            self.assertEqual(list(api.pages('jobs')),[{'id':1},{'id':2}])
    def test_origin(self):
        api=c.API('https://example.com','fake')
        with self.assertRaises(ValueError): api.get('https://elsewhere.example/api/v2/jobs/')
        with self.assertRaises(ValueError): c.API('http://example.com','fake')
    def test_end_to_end(self):
        now=c.dt.datetime.now(c.UTC)
        row=dict(id=1,type='job',status='successful',name='example',created=c.iso(now-c.dt.timedelta(hours=3)),
                 started=c.iso(now-c.dt.timedelta(hours=2)),finished=c.iso(now-c.dt.timedelta(hours=1)),job_template=7)
        def pages(api,endpoint,params=None):
            return iter([dict(row,forks=20,execution_node='exec1')] if endpoint in ('jobs','unified_jobs') else [])
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,{'CONTROLLER_HOST':'https://example.com','CONTROLLER_OAUTH_TOKEN':'fake'}), patch.object(c.API,'pages',pages), patch.object(c.API,'get',return_value={'count':0}), patch.object(sys,'argv',['collector','--output',temp,'--no-metrics']):
            c.main()
            report=json.loads(next(Path(temp).glob('*/report.json')).read_text())
            self.assertEqual(report['growth_scenario']['target_concurrent_equivalent_playbook_jobs'],2)
            self.assertEqual(report['by_execution_node']['exec1']['concurrency']['peak'],1)
            self.assertTrue(next(Path(temp).glob('*/summary.md')).exists())
class ComponentTests(unittest.TestCase):
    def test_pulp_pagination_identity(self):
        api=c.API('https://example.com','fake',api_path='/api/galaxy/pulp/api/v3/')
        with patch.object(api,'get',side_effect=[{'results':[{'pulp_href':'a'}],'next':'?offset=1'},
                                               {'results':[{'pulp_href':'b'}],'next':None}]):
            self.assertEqual(len(list(api.pages('tasks'))),2)
    def test_missing_component_is_not_zero_workload(self):
        api=c.API('https://example.com','fake',api_path='/api/eda/v1/')
        with patch.object(api,'get',side_effect=RuntimeError('HTTP 404 from /api/eda/v1/activations/')):
            report=c.collect_eda(api,S,E)
        self.assertEqual(report['status'],'unavailable')
        self.assertIsNone(report['summary']['activation_execution_history'])
        self.assertEqual(report['endpoints']['activations']['status'],'not_found')
    def test_forbidden_component(self):
        api=c.API('https://example.com','fake',api_path='/api/eda/v1/')
        with patch.object(api,'get',side_effect=RuntimeError('HTTP 403 from /api/eda/v1/activations/')):
            report=c.collect_eda(api,S,E)
        self.assertEqual(report['endpoints']['activations']['status'],'forbidden')
    def test_partial_page_limit(self):
        api=c.API('https://example.com','fake',max_pages=1)
        with patch.object(api,'get',return_value={'results':[{'id':1,'name':'safe','extra_var':'SECRET'}],'next':'?page=2'}):
            report=c.read_list(api,'activations',c.EDA_FIELDS,{})
        self.assertEqual(report['status'],'partial')
        self.assertNotIn('extra_var',report['records'][0])
    def test_history_carry_in_and_capitalization(self):
        rows=[{'id':1,'started_at':'2025-12-31T23:00:00Z','ended_at':None,'status':'Running'},
              {'id':2,'started_at':'2025-12-31T22:00:00Z','ended_at':'2025-12-31T23:00:00Z','status':'completed'}]
        records, invalid=c.history(rows,S,E,'ended_at')
        self.assertEqual(len(records),1)
        self.assertEqual(c.history_summary(records,S,E)['execution_concurrency']['peak'],1)
        self.assertEqual(invalid,0)
    def test_hub_content_counts_and_artifact_sizes(self):
        api=c.API('https://example.com','fake',api_path='/api/galaxy/pulp/api/v3/')
        def pages(endpoint,params):
            return iter([{'pulp_href':'a','size':100},{'pulp_href':'b','size':200}] if endpoint=='artifacts' else [])
        def get(path,raw=False):
            return {'versions':[{'component':'pulpcore','version':'test'}]} if 'status/' in path else {'count':2}
        with patch.object(api,'pages',side_effect=pages),patch.object(api,'get',side_effect=get):
            report=c.collect_hub(api,S,E,True)
        self.assertEqual(report['status'],'collected')
        self.assertEqual(report['summary']['artifact_sizes']['observed_logical_bytes'],300)
        self.assertTrue(report['summary']['artifact_sizes']['complete'])
    def test_incompatible_list_schema(self):
        api=c.API('https://example.com','fake')
        with patch.object(api,'get',return_value={'data':[]}):
            with self.assertRaisesRegex(RuntimeError,'Unsupported pagination'):
                list(api.pages('jobs'))
    def test_cross_component_and_external_links_rejected(self):
        api=c.API('https://example.com','fake',api_path='/api/eda/v1/')
        for url in ('https://other.example/api/eda/v1/activations/', '/api/gateway/v1/tokens/'):
            with self.assertRaises(ValueError): api.get(url)

unittest.main()
