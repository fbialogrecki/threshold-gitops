import copy
import importlib.util
import pathlib
import tempfile
import unittest

import yaml

MODULE = pathlib.Path(__file__).with_name('helm_checks.py')
if MODULE.exists():
    spec = importlib.util.spec_from_file_location('helm_checks', MODULE)
    hc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hc)
else:
    hc = None


def application():
    return {'kind': 'Application', 'metadata': {'name': 'example'}, 'spec': {
        'destination': {'namespace': 'example'}, 'sources': [
            {'repoURL': 'https://charts.example.org', 'chart': 'example',
             'targetRevision': '1.2.3', 'helm': {'releaseName': 'release',
                 'valueFiles': ['$values/values/first.yaml', '$values/values/second.yaml']}},
            {'repoURL': 'https://example.org/git.git', 'targetRevision': 'main', 'ref': 'values'}]}}


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        (self.root / 'infra/argocd/applications').mkdir(parents=True)
        (self.root / 'values').mkdir()
        for name in ('first', 'second'):
            (self.root / f'values/{name}.yaml').write_text('key: value\n')
        self.path = self.root / 'infra/argocd/applications/example.yaml'
        self.doc = application()
        self.save()
        (self.root / 'infra/argocd/root.yaml').write_text(yaml.safe_dump({
            'kind': 'Application', 'spec': {'source': {
                'repoURL': 'https://example.org/git.git', 'targetRevision': 'main', 'path': 'infra/argocd'}}}))

    def save(self):
        self.path.write_text(yaml.safe_dump(self.doc))

    def test_inventory_preserves_declared_identity_and_value_order(self):
        self.assertIsNotNone(hc, 'Helm contract module must exist')
        apps = hc.inventory(self.root)
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0]['release'], 'release')
        self.assertEqual(apps[0]['namespace'], 'example')
        self.assertEqual(apps[0]['version'], '1.2.3')
        self.assertEqual(apps[0]['values'], ['values/first.yaml', 'values/second.yaml'])

    def test_inventory_fails_closed_for_unsupported_or_unsafe_input(self):
        variants = []
        for key, value in [('targetRevision', '*'), ('targetRevision', 'latest'),
                           ('plugin', {}), ('path', 'chart')]:
            doc = application()
            doc['spec']['sources'][0][key] = value
            variants.append(doc)
        for key in ('parameters', 'fileParameters', 'values', 'valuesObject',
                    'ignoreMissingValueFiles', 'skipSchemaValidation', 'version'):
            doc = application()
            doc['spec']['sources'][0]['helm'][key] = 'unsupported'
            variants.append(doc)
        for value in ('$unknown/values/first.yaml', '$values/../outside.yaml',
                      '/absolute.yaml', 'https://example.org/value.yaml'):
            doc = application()
            doc['spec']['sources'][0]['helm']['valueFiles'] = [value]
            variants.append(doc)
        doc = application()
        doc['spec']['sources'][1]['repoURL'] = 'https://other.example.org/git'
        variants.append(doc)
        for doc in variants:
            with self.subTest(variant=variants.index(doc)):
                self.doc = doc
                self.save()
                with self.assertRaises(hc.ContractError):
                    hc.inventory(self.root)

    def test_inventory_rejects_missing_and_symlink_values(self):
        value = self.root / 'values/first.yaml'
        value.unlink()
        with self.assertRaises(hc.ContractError):
            hc.inventory(self.root)
        value.symlink_to(self.root / 'values/second.yaml')
        with self.assertRaises(hc.ContractError):
            hc.inventory(self.root)

    def test_strict_inventory_yaml_paths_and_capabilities(self):
        value = self.root / 'values/first.yaml'
        for text in ('key: one\nkey: two\n', '[not, mapping]\n', 'key: [\n'):
            value.write_text(text)
            with self.subTest(text=text), self.assertRaises(hc.ContractError):
                hc.inventory(self.root)
        value.write_text('key: value\n')
        for key, val in [('kubeVersion', 123), ('apiVersions', 'bad')]:
            self.doc = application()
            self.doc['spec']['sources'][0]['helm'][key] = val
            self.save()
            with self.assertRaises(hc.ContractError):
                hc.inventory(self.root)
        self.doc = application()
        self.save()
        self.path.rename(self.path.with_suffix('.yml'))
        self.assertEqual(len(hc.inventory(self.root)), 1)
        for path in ('values//first.yaml', 'values/./first.yaml', 'values/first.yaml\n'):
            with self.assertRaises(hc.ContractError):
                hc.local_file(self.root, path)

    def test_cli_reference_contract_and_missing_base_fallback(self):
        self.assertTrue(hasattr(hc, 'main'), 'executable CLI missing')
        self.assertTrue(hasattr(hc, 'expected_refs'), 'values reference derivation missing')
        first = self.root / 'values/first.yaml'
        second = self.root / 'values/second.yaml'
        first.write_text('admin: {existingSecret: old, userKey: user, passwordKey: password}\ns3: {existingConfigSecret: s3}\nserver: {extraSecretNamesForEnvFrom: [old]}\n')
        second.write_text('admin: {existingSecret: new}\nserver: {extraSecretNamesForEnvFrom: [new]}\n')
        app = hc.inventory(self.root)[0]
        self.assertEqual(hc.expected_refs(self.root, app), {('new', 'user'), ('new', 'password'), ('s3', None), ('new', None)})
        self.assertEqual(hc.affected(self.root, [app], 'a' * 40), [app])
        second.write_text('server: {extraSecretNamesForEnvFrom: wrong}\n')
        with self.assertRaises(hc.ContractError):
            hc.expected_refs(self.root, app)
        import contextlib, io
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(hc.main(['--root', str(self.root), '--helm', '/missing']), 1)
        self.assertNotIn('Traceback', output.getvalue())
        self.assertNotIn('wrong', output.getvalue())

    def test_empty_selector_malformed_workloads_and_shared_selection(self):
        for doc in ({'kind': 'Deployment', 'metadata': {}, 'spec': {'selector': {'matchLabels': {}}, 'template': {'spec': {}}}},
                    {'kind': 'Pod', 'metadata': {}, 'spec': None},
                    {'kind': 'CronJob', 'metadata': {}, 'spec': {}}):
            with self.subTest(kind=doc['kind']), self.assertRaises(hc.ContractError):
                hc.validate_workloads([doc], 'example', set())
        pod = {'kind': 'Pod', 'metadata': {}, 'spec': {'containers': [{'name': 'ok'}]}}
        hc.validate_workloads([pod], 'example', set())
        cron = {'kind': 'CronJob', 'metadata': {}, 'spec': {'jobTemplate': {'spec': {'template': pod}}}}
        hc.validate_workloads([cron], 'example', set())
        apps = hc.inventory(self.root)
        apps += [dict(apps[0], name='other'), dict(apps[0], name='third', values=[])]
        self.assertEqual(hc.select(apps, ['values/first.yaml']), apps)

    def test_render_capability_pin_duplicate_yaml_and_download_failure(self):
        import os
        helm = os.environ['HELM_BIN']
        chart = self.root / 'control'
        (chart / 'templates').mkdir(parents=True)
        (chart / 'Chart.yaml').write_text('apiVersion: v2\nname: control\nversion: 1.0.0\n')
        template = chart / 'templates/control.yaml'
        template.write_text('apiVersion: v1\nkind: ConfigMap\nmetadata: {name: control}\ndata:\n  enabled: {{ .Capabilities.APIVersions.Has "example.org/v1" | quote }}\n')
        app = hc.inventory(self.root)[0]
        profile = {'helmVersion': 'v3.19.4', 'kubeVersion': '1.34.0', 'apiVersions': ['example.org/v1']}
        self.assertEqual(hc.render(self.root, app, helm, profile, chart)[0]['data']['enabled'], 'true')
        for bad in (dict(profile, apiVersions='bad'), dict(profile, helmVersion='v0.0.0')):
            with self.assertRaises(hc.ContractError):
                hc.render(self.root, app, helm, bad, chart)
        template.write_text('apiVersion: v1\nkind: ConfigMap\nmetadata: {name: control}\ndata: {key: first, key: second}\n')
        with self.assertRaises(hc.ContractError):
            hc.render(self.root, app, helm, profile, chart)
        with self.assertRaisesRegex(hc.ContractError, '^Helm template/schema failed$'):
            hc.render(self.root, dict(app, repo='https://127.0.0.1:1'), helm, profile)

    def test_malformed_application_cannot_disappear(self):
        other = application()
        other['metadata']['name'] = 'other'
        (self.path.parent / 'other.yaml').write_text(yaml.safe_dump(other))
        for mutation in ('chart', 'sources', 'scalar', 'root'):
            self.doc = application()
            if mutation == 'chart':
                del self.doc['spec']['sources'][0]['chart']
            elif mutation == 'sources':
                self.doc['spec']['sources'] = ['invalid']
            elif mutation == 'scalar':
                self.doc = 'invalid'
            else:
                (self.root / 'infra/argocd/root.yaml').write_text('[]\n')
            self.save()
            with self.subTest(mutation=mutation), self.assertRaises(hc.ContractError):
                hc.inventory(self.root)

    def test_inventory_rejects_directory_symlink_with_normal_chart(self):
        target = self.root / 'hidden-apps'
        target.mkdir()
        other = application()
        other['metadata']['name'] = 'hidden'
        (target / 'hidden.yaml').write_text(yaml.safe_dump(other))
        self.assertEqual(len(hc.inventory(self.root)), 1)
        (self.path.parent / 'linked-apps').symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(hc.ContractError, 'symlink input unsupported'):
            hc.inventory(self.root)

    def test_cli_rejects_directory_symlink_before_selection(self):
        import contextlib, io
        from unittest.mock import patch
        target = self.root / 'hidden-apps'
        target.mkdir()
        (self.path.parent / 'linked-apps').symlink_to(target, target_is_directory=True)
        with patch.object(hc, 'affected', return_value=[]) as selection:
            with contextlib.redirect_stdout(io.StringIO()):
                result = hc.main(['--root', str(self.root), '--base', 'a' * 40])
        self.assertEqual(result, 1)
        selection.assert_not_called()

    def test_inventory_cannot_be_empty(self):
        self.path.unlink()
        with self.assertRaises(hc.ContractError):
            hc.inventory(self.root)

    def test_select_affected_and_unknown_fallback(self):
        self.assertTrue(hasattr(hc, 'select'), 'selection contract missing')
        apps = hc.inventory(self.root)
        other = dict(apps[0], name='other', path='infra/argocd/applications/other.yaml',
                     values=['values/other.yaml'])
        apps.append(other)
        self.assertEqual(hc.select(apps, ['values/first.yaml']), apps[:1])
        self.assertEqual(hc.select(apps, [other['path']]), [other])
        for path in ('infra/argocd/root.yaml', 'ci/check.py', 'README.md',
                     'values/deleted.yaml', 'infra/argocd/applications/deleted.yaml'):
            self.assertEqual(hc.select(apps, [path]), apps)
        self.assertEqual(hc.select(apps, []), [])
        with self.assertRaises(hc.ContractError):
            hc.select([], [])

    def test_git_base_validation_and_deleted_renamed_paths(self):
        self.assertTrue(hasattr(hc, 'changed_paths'), 'Git selection missing')
        import subprocess
        def git(*args, **kwargs):
            return subprocess.run(['git', *args], cwd=self.root, check=True,
                                  capture_output=True, text=True, **kwargs).stdout.strip()
        git('init', '-q')
        git('add', '.')
        tree = git('write-tree')
        # Synthetic fixture objects only; never commits in the working repository.
        import os
        env = dict(os.environ, GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.org',
                   GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.org')
        base = git('commit-tree', tree, '-m', 'fixture', env=env)
        git('update-ref', 'HEAD', base)
        (self.root / 'values/first.yaml').rename(self.root / 'values/renamed.yaml')
        (self.root / 'values/second.yaml').unlink()
        git('add', '-A')
        changed = hc.changed_paths(self.root, base)
        self.assertTrue({'values/first.yaml', 'values/renamed.yaml',
                         'values/second.yaml'} <= set(changed))
        for bad in ('HEAD', '', '0' * 40, '--help', 'a' * 39):
            with self.assertRaises(hc.ContractError):
                hc.changed_paths(self.root, bad)

    def test_real_helm_control_then_schema_failure_is_sanitized(self):
        self.assertTrue(hasattr(hc, 'render'), 'isolated renderer missing')
        import os
        import shutil
        helm = os.environ.get('HELM_BIN') or shutil.which('helm')
        self.assertTrue(helm, 'HELM_BIN or pinned helm required for real control')
        chart = self.root / 'chart'
        (chart / 'templates').mkdir(parents=True)
        (chart / 'Chart.yaml').write_text('apiVersion: v2\nname: control\nversion: 1.0.0\n')
        (chart / 'values.schema.json').write_text(
            '{"type":"object","properties":{"count":{"type":"integer"}}}')
        (chart / 'templates/deployment.yaml').write_text('''apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Release.Name }}
  namespace: {{ .Release.Namespace }}
spec:
  replicas: {{ .Values.count }}
  selector:
    matchLabels: {app: control}
  template:
    metadata:
      labels: {app: control}
    spec:
      containers:
        - name: control
          image: example.invalid/control:1
''')
        first = self.root / 'values/first.yaml'
        second = self.root / 'values/second.yaml'
        first.write_text('count: 1\n')
        second.write_text('count: 2\n')
        app = hc.inventory(self.root)[0]
        profile = {'helmVersion': 'v3.19.4', 'kubeVersion': '1.34.0', 'apiVersions': []}
        rendered = hc.render(self.root, app, helm, profile, chart=chart)
        self.assertEqual(rendered[0]['spec']['replicas'], 2)
        self.assertEqual(rendered[0]['metadata']['name'], 'release')
        self.assertEqual(rendered[0]['metadata']['namespace'], 'example')
        second.write_text('count: PRIVATE_SENTINEL\n')
        with self.assertRaisesRegex(hc.ContractError, '^Helm template/schema failed$'):
            hc.render(self.root, app, helm, profile, chart=chart)
        second.write_text('count: [\n')
        with self.assertRaisesRegex(hc.ContractError, '^Helm template/schema failed$'):
            hc.render(self.root, app, helm, profile, chart=chart)
        with self.assertRaisesRegex(hc.ContractError, 'Helm version mismatch'):
            hc.render(self.root, app, helm, dict(profile, helmVersion='v0.0.0'), chart=chart)

    def test_renderer_environment_is_an_allowlist(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {'AWS_SECRET_ACCESS_KEY': 'PRIVATE',
                                     'KUBECONFIG': '/secret', 'HELM_TOKEN': 'PRIVATE'}):
            env = hc.isolated_env(self.root)
        self.assertNotIn('AWS_SECRET_ACCESS_KEY', env)
        self.assertNotIn('HELM_TOKEN', env)
        self.assertEqual(env['KUBECONFIG'], '/dev/null')
        self.assertEqual(env['HOME'], str(self.root))

    def test_workload_selector_namespace_and_expected_secret_references(self):
        self.assertTrue(hasattr(hc, 'validate_workloads'), 'workload checks missing')
        doc = {'kind': 'Deployment', 'metadata': {'namespace': 'example'}, 'spec': {
            'selector': {'matchLabels': {'app': 'control'}},
            'template': {'metadata': {'labels': {'app': 'control'}}, 'spec': {
                'containers': [{'name': 'control', 'env': [{'name': 'PASSWORD',
                    'valueFrom': {'secretKeyRef': {'name': 'credentials', 'key': 'password'}}}]}]}}}}
        hc.validate_workloads([doc], 'example', {('credentials', 'password')})
        for change in ('selector', 'namespace', 'secret'):
            bad = copy.deepcopy(doc)
            if change == 'selector':
                bad['spec']['template']['metadata']['labels']['app'] = 'wrong'
            elif change == 'namespace':
                bad['metadata']['namespace'] = 'wrong'
            else:
                bad['spec']['template']['spec']['containers'][0]['env'] = []
            with self.subTest(change=change), self.assertRaises(hc.ContractError):
                hc.validate_workloads([bad], 'example', {('credentials', 'password')})
        bad = copy.deepcopy(doc)
        bad['spec']['selector'] = {'matchExpressions': [{
            'key': 'app', 'operator': 'In', 'values': ['wrong']}]}
        with self.assertRaises(hc.ContractError):
            hc.validate_workloads([bad], 'example', set())


if __name__ == '__main__':
    unittest.main()
