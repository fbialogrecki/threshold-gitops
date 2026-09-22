"""Offline Grafana query contracts; requires real promtool 3.5.5, never skips.

Only PromQL is evaluated here, not Grafana's scheduler/notification delivery.
Synthetic service labels/routes are fixtures, not operational measurements.
"""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'infra/helm/grafana/application-alerts.yaml'
PROBES = ('/health', '/healthz', '/readyz', '/ready', '/metrics', '/docs', '/openapi.json', '/redoc')
IDS = ('threshold-app-http-5xx', 'threshold-app-http-latency')


def rules():
    doc = yaml.safe_load(MANIFEST.read_text())
    return {r['uid']: r for g in doc['alerting']['threshold-application-rules.yaml']['groups'] for r in g['rules']}


def expression(rule):
    return next(d['model']['expr'] for d in rule['data'] if d['refId'] == 'A')


def sample(metric, service, route, increment, **labels):
    labels.update(service_name=service, http_route=route)
    selector = ','.join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return {'series': f'{metric}{{{selector}}}', 'values': f'0+{increment}x20'}


def traffic(service, route, count, errors=0, slow=False, status='2xx'):
    series = [sample('threshold_http_server_requests_total', service, route, count, http_response_status_class=status)]
    if errors:
        series.append(sample('threshold_http_server_requests_total', service, route, errors, http_response_status_class='5xx'))
    total = count + errors
    # Every observation is in either the <=1s or (2,4]s bucket.
    for le, value in [('1', 0 if slow else total), ('2', 0 if slow else total), ('4', total), ('+Inf', total)]:
        series.append(sample('threshold_http_server_request_duration_seconds_bucket', service, route, value, le=le))
    return series


def check_case(expr, expected, at='10m'):
    return {'expr': expr, 'eval_time': at, 'exp_samples': [
        {'labels': '{service_name="' + service + '"}', 'value': value}
        for service, value in expected.items()]}


def semantic_fixture(expressions):
    error, latency = expressions
    def checks(bad=(), slow=()):
        return [check_case(f'({error}) > bool 0.05', dict(bad)),
                check_case(f'({latency}) > bool 1.0', dict(slow))]
    tests = [
        {'name': 'bad service is not masked by busy healthy service or probes',
         'input_series': traffic('users', '/product', 90, 10, True)
                         + traffic('media', '/product', 100000)
                         + traffic('users', '/health', 100000),
         'promql_expr_test': checks([('users', 1), ('media', 0)], [('users', 1), ('media', 0)])
                            + [check_case(error, {'users': 0.1, 'media': 0}),
                               check_case(latency, {'users': 3.9, 'media': 0.95})]},
        {'name': 'healthy product and 4xx are not server failures',
         'input_series': traffic('users', '/product', 100, status='4xx'),
         'promql_expr_test': checks([('users', 0)], [('users', 0)])},
        {'name': 'zero traffic cannot create infinite error ratio',
         'input_series': traffic('users', '/product', 0),
         'promql_expr_test': [check_case(error, {}), check_case(f'({latency}) > 1.0', {})]},
        {'name': 'absent series', 'input_series': [],
         'promql_expr_test': [check_case(error, {}), check_case(latency, {})]},
    ]
    for route in PROBES:
        tests.append({'name': f'probe only {route}',
                      'input_series': traffic('users', route, 50, 50, True),
                      'promql_expr_test': [check_case(error, {}), check_case(latency, {})]})
        tests.append({'name': f'probe errors and latency cannot contaminate healthy product {route}',
                      'input_series': traffic('users', route, 10000, 10000, True) + traffic('users', '/product', 100),
                      'promql_expr_test': checks([('users', 0)], [('users', 0)])})
    recovery = traffic('users', '/product', 90, 10, True)
    for s in recovery:
        # Real counter reset followed by healthy traffic; old error increments stop.
        if 'http_response_status_class="5xx"' in s['series']:
            s['values'] = '0+10x10 0+0x10'
        elif '_bucket' in s['series']:
            s['values'] = '0+0x10 0+100x10' if 'le="1"' in s['series'] or 'le="2"' in s['series'] else '0+100x10 0+100x10'
        else:
            s['values'] = '0+90x10 0+100x10'
    tests.append({'name': 'recovery after counter reset', 'input_series': recovery,
                  'promql_expr_test': [check_case(f'({error}) > bool 0.05', {'users': 0}, '20m'),
                                       check_case(f'({latency}) > bool 1.0', {'users': 0}, '20m')]})
    # Ignore only last-bit floating point differences in rate/quantile arithmetic.
    return {'evaluation_interval': '1m', 'fuzzy_compare': True,
            'tests': [{'interval': '1m', **t} for t in tests]}


class AlertContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.promtool = os.environ.get('PROMTOOL_BIN', 'promtool')
        version = subprocess.run([cls.promtool, '--version'], capture_output=True, text=True, check=True)
        if not re.search(r'\bversion 3\.5\.5\b', version.stdout + version.stderr):
            raise AssertionError('promtool must be pinned to 3.5.5')

    def run_promtool(self, expressions):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / 'alerts.test.yaml'
            fixture.write_text(yaml.safe_dump(semantic_fixture(expressions), sort_keys=False))
            return subprocess.run([self.promtool, 'test', 'rules', str(fixture)], capture_output=True, text=True)

    def test_actual_grafana_queries_have_required_semantics(self):
        current = rules()
        result = self.run_promtool([expression(current[uid]) for uid in IDS])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_global_and_probe_inclusive_mutations_fail_semantically(self):
        expressions = [expression(rules()[uid]) for uid in IDS]
        mutations = [
            [re.sub(r'by\s*\(service_name\)', '', expressions[0]), expressions[1].replace('(le, service_name)', '(le)')],
            [re.sub(r',http_route!~"[^"]*"', '', expr) for expr in expressions],
            [expressions[0].replace(' > 0)', ')'), expressions[1]],
        ]
        for mutated in mutations:
            with self.subTest(expressions=mutated):
                self.assertNotEqual(mutated, expressions, 'mutation did not change actual queries')
                result = self.run_promtool(mutated)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('exp:', result.stdout + result.stderr, 'must fail semantic expectations, not syntax')
                self.assertNotIn('parse error', result.stdout + result.stderr)

    def test_grafana_policy_and_query_contract(self):
        current = rules()
        self.assertEqual(set(current), {'threshold-app-unavailable', *IDS})
        for uid, threshold in zip(IDS, (0.05, 1.0)):
            r = current[uid]
            self.assertEqual(r['for'], '10m')
            self.assertEqual(r['labels'], {'severity': 'warning'})
            self.assertEqual((r['noDataState'], r['execErrState'], r['condition']), ('OK', 'Error', 'C'))
            a, c = r['data']
            self.assertEqual(a['datasourceUid'], 'PAE45454D0EDB9216')
            self.assertTrue(a['model']['instant'])
            self.assertFalse(a['model']['range'])
            self.assertEqual(c['datasourceUid'], '__expr__')
            self.assertEqual(c['model']['type'], 'threshold')
            self.assertEqual(c['model']['expression'], 'A')
            self.assertEqual(c['model']['conditions'][0]['evaluator'], {'params': [threshold], 'type': 'gt'})
            self.assertIn('[5m]', expression(r))
        error = expression(current[IDS[0]])
        filters = re.findall(r'http_route!~"([^"]*)"', error)
        self.assertGreaterEqual(len(filters), 2)
        self.assertEqual(len(set(filters)), 1, 'numerator/denominator route filters must match')
        self.assertIn('http_response_status_class="5xx"', error)


if __name__ == '__main__':
    unittest.main()
