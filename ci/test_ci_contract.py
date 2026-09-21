"""Required local/CI gate enrollment contracts (no network required)."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


class EnrollmentTests(unittest.TestCase):
    def test_required_job_calls_shared_gate_with_pinned_tools(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())
        infra = workflow['jobs']['infra']
        self.assertNotIn('if', infra)
        self.assertNotIn('continue-on-error', infra)
        steps = infra['steps']
        run = '\n'.join(step.get('run', '') for step in steps)
        self.assertIn('task infra:check', run)
        self.assertIn('python3 -m pip install --require-hashes --only-binary=:all: -r ci/requirements.txt', run)
        helm = next((s for s in steps if s.get('name') == 'Install Helm'), {})
        self.assertEqual(helm.get('env', {}).get('HELM_VERSION'), '3.19.4')
        self.assertEqual(helm.get('env', {}).get('HELM_SHA256'), '759c656fbd9c11e6a47784ecbeac6ad1eb16a9e76d202e51163ab78504848862')
        self.assertIn('sha256sum --check --strict', helm.get('run', ''))
        kustomize = next(s for s in steps if s.get('name') == 'Install kustomize')
        self.assertEqual(kustomize['env']['KUSTOMIZE_VERSION'], '5.7.1')
        self.assertEqual(workflow['permissions'], {'contents': 'read'})
        security = workflow['jobs']['security']
        self.assertNotIn('if', security)
        self.assertIn('gitleaks git --redact --verbose', str(security))
        tasks = yaml.safe_load((ROOT / 'Taskfile.yml').read_text())['tasks']
        self.assertIn('helm:check', tasks['infra:check']['deps'])
        self.assertIn('ci/run_helm_tests.py', str(tasks['helm:check']))
        self.assertIn('ci/helm_checks.py', str(tasks['helm:check']))
        self.assertIn('no inline Secret data found', str(tasks['infra:check']))
        self.assertIn('rendered $count kustomizations', str(tasks['infra:check']))

    def test_runner_rejects_missing_or_empty_test_modules(self):
        runner = ROOT / 'ci/run_helm_tests.py'
        self.assertTrue(runner.exists(), 'mandatory test runner missing')
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / runner.name
            shutil.copyfile(runner, target)
            for content in (None, '# empty suite\n'):
                for name in ('test_helm_checks.py', 'test_ci_contract.py'):
                    if content is not None:
                        (Path(temp) / name).write_text(content)
                result = subprocess.run([sys.executable, str(target)], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)

    def test_missing_pyyaml_fails(self):
        result = subprocess.run([sys.executable, '-S', str(ROOT / 'ci/helm_checks.py')], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('yaml', result.stderr)

    def test_missing_helm_fails(self):
        result = subprocess.run([sys.executable, str(ROOT / 'ci/helm_checks.py'), '--helm', '/nonexistent/helm'], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Helm contract check failed', result.stdout)


if __name__ == '__main__':
    unittest.main()
