"""Bounded, secretless Argo Helm render checks; see ci/README.md."""
import pathlib
import re
import subprocess
import tempfile
from urllib.parse import urlsplit

import yaml

HELM_VERSION = 'v3.19.4'


class StrictLoader(yaml.SafeLoader):
    pass


def strict_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        require(isinstance(key, str) and key not in result, 'invalid or duplicate YAML key')
        result[key] = loader.construct_object(value_node)
    return result


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, strict_mapping)


def capabilities(profile, partial=False):
    require(isinstance(profile, dict) and set(profile) <= {
        'helmVersion', 'kubeVersion', 'apiVersions'}, 'invalid capability profile')
    require(profile.get('helmVersion', HELM_VERSION) == HELM_VERSION, 'Helm version mismatch')
    if not partial or 'kubeVersion' in profile:
        require(isinstance(profile.get('kubeVersion'), str) and re.fullmatch(
            r'[0-9]+\.[0-9]+\.[0-9]+', profile['kubeVersion']), 'invalid Kubernetes capability version')
    if not partial or 'apiVersions' in profile:
        apis = profile.get('apiVersions')
        require(isinstance(apis, list) and all(isinstance(api, str) and re.fullmatch(
            r'[A-Za-z0-9./-]+', api) for api in apis), 'invalid API capabilities')


def values_document(path):
    docs = documents(path)
    require(len(docs) == 1 and isinstance(docs[0], dict), 'values must be one mapping')
    return docs[0]


def isolated_env(home):
    return {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(home),
            'TMPDIR': str(home), 'LANG': 'C.UTF-8', 'KUBECONFIG': '/dev/null',
            'HELM_CACHE_HOME': str(home / 'cache'),
            'HELM_CONFIG_HOME': str(home / 'config'),
            'HELM_DATA_HOME': str(home / 'data'),
            'HELM_PLUGINS': str(home / 'plugins'),
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0'}


def changed_paths(root, base):
    require(re.fullmatch('[0-9a-f]{40}', base or '') is not None
            and base != '0' * 40, 'base must be an exact commit SHA')
    with tempfile.TemporaryDirectory(prefix='helm-git-') as temp:
        env = isolated_env(pathlib.Path(temp))
        try:
            subprocess.run(['git', 'cat-file', '-e', base + '^{commit}'], cwd=root,
                           env=env, capture_output=True, check=True, timeout=30)
            result = subprocess.run(['git', 'diff', '--no-ext-diff', '--no-textconv',
                                     '--no-renames', '--name-only', '-z', base, '--'],
                                    cwd=root, env=env, capture_output=True, check=True,
                                    timeout=30)
        except (OSError, subprocess.SubprocessError):
            raise ContractError('base unavailable or Git diff failed') from None
    return [p for p in result.stdout.decode().split('\0') if p]


def select(apps, changed):
    require(bool(apps), 'chart inventory missing')
    selected = set()
    for path in changed:
        matches = {a['name'] for a in apps if path == a['path'] or path in a['values']}
        if len(matches) != 1:
            return apps
        selected.update(matches)
    return [a for a in apps if a['name'] in selected]


def render(root, app, helm_binary, profile, chart=None):
    """Render in a disposable, credential-free home; chart override is for tests."""
    capabilities(profile)
    import shutil
    binary = shutil.which(helm_binary)
    require(binary is not None, 'Helm executable missing')
    binary = str(pathlib.Path(binary).resolve())
    with tempfile.TemporaryDirectory(prefix='helm-check-') as temp:
        home = pathlib.Path(temp)
        env = isolated_env(home)
        def run(args, failure):
            try:
                result = subprocess.run([binary, *args], cwd=home, env=env,
                                        capture_output=True, check=True, timeout=180)
            except (OSError, subprocess.SubprocessError):
                raise ContractError(failure) from None
            return result.stdout.decode('utf-8')
        version = run(['version', '--template', '{{.Version}}'], 'Helm version check failed')
        require(version == HELM_VERSION, 'Helm version mismatch')
        helm = app['helm']
        kube = helm.get('kubeVersion', profile['kubeVersion'])
        apis = helm.get('apiVersions', profile['apiVersions'])
        require(isinstance(kube, str) and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', kube),
                'invalid Kubernetes capability version')
        require(isinstance(apis, list) and all(isinstance(api, str) and re.fullmatch(
            r'[A-Za-z0-9./-]+', api) for api in apis), 'invalid API capabilities')
        args = ['template', app['release'], str(chart) if chart else app['chart'],
                '--namespace', app['namespace'], '--kube-version', kube, '--include-crds']
        if chart is None:
            args += ['--repo', app['repo'], '--version', app['version']]
        for api in apis:
            args += ['--api-versions', api]
        for value in app['values']:
            args += ['--values', str(local_file(root, value))]
        output = run(args, 'Helm template/schema failed')
        try:
            docs = [doc for doc in yaml.load_all(output, Loader=StrictLoader) if doc is not None]
        except yaml.YAMLError:
            raise ContractError('invalid rendered YAML') from None
        require(bool(docs) and all(isinstance(doc, dict) for doc in docs),
                'empty or invalid chart render')
        return docs


def validate_workloads(docs, namespace, expected_refs):
    """Bounded workload invariants, not Kubernetes schema/admission validation."""
    found = set()
    def refs(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == 'secretKeyRef' and isinstance(value, dict):
                    found.add((value.get('name'), value.get('key')))
                if key == 'secretRef' and isinstance(value, dict):
                    found.add((value.get('name'), None))
                if key == 'secret' and isinstance(value, dict) and 'secretName' in value:
                    found.add((value['secretName'], None))
                refs(value)
        elif isinstance(node, list):
            for value in node:
                refs(value)
    try:
        for doc in docs:
            kind = doc.get('kind')
            if kind not in {'Deployment', 'StatefulSet', 'DaemonSet', 'Job', 'CronJob', 'Pod'}:
                continue
            require(doc.get('metadata', {}).get('namespace', namespace) == namespace,
                    'workload namespace mismatch')
            spec = doc['spec']
            if kind == 'CronJob':
                spec = spec['jobTemplate']['spec']
            require(isinstance(spec, dict), 'invalid workload spec')
            template = doc if kind == 'Pod' else spec['template']
            if kind in {'Deployment', 'StatefulSet', 'DaemonSet'}:
                selector = spec['selector']
                require(isinstance(selector, dict) and set(selector) <= {'matchLabels', 'matchExpressions'}
                        and bool(selector.get('matchLabels') or selector.get('matchExpressions')), 'invalid workload selector')
                labels = template.get('metadata', {}).get('labels', {})
                require(isinstance(labels, dict) and isinstance(selector.get('matchLabels', {}), dict)
                        and isinstance(selector.get('matchExpressions', []), list), 'invalid workload selector')
                require(all(labels.get(k) == v for k, v in selector.get('matchLabels', {}).items()),
                        'workload selector mismatch')
                for expr in selector.get('matchExpressions', []):
                    key, op = expr['key'], expr['operator']
                    values = expr.get('values', [])
                    require(isinstance(key, str) and isinstance(values, list)
                            and all(isinstance(v, str) for v in values)
                            and ((op in {'In', 'NotIn'} and bool(values)) or
                                 (op in {'Exists', 'DoesNotExist'} and not values)), 'invalid selector expression')
                    matches = {'In': key in labels and labels[key] in values,
                               'NotIn': key not in labels or labels[key] not in values,
                               'Exists': key in labels, 'DoesNotExist': key not in labels}
                    require(op in matches and matches[op], 'workload selector mismatch')
            require(isinstance(template['spec'], dict)
                    and isinstance(template['spec'].get('containers'), list)
                    and bool(template['spec']['containers'])
                    and all(isinstance(c, dict) for c in template['spec']['containers']), 'invalid pod spec')
            refs(template['spec'])
        require(set(expected_refs) <= found, 'expected workload Secret reference missing')
    except (KeyError, TypeError, AttributeError):
        raise ContractError('invalid workload structure') from None


def expected_refs(root, app):
    def merge(left, right):
        for key, value in right.items():
            if isinstance(value, dict) and isinstance(left.get(key), dict):
                merge(left[key], value)
            else:
                left[key] = value
        return left
    values = {}
    for path in app['values']:
        merge(values, values_document(local_file(root, path)))
    result = set()
    def name(value):
        require(isinstance(value, str) and re.fullmatch(r'[a-z0-9][a-z0-9.-]*', value),
                'invalid declared Secret reference')
        return value
    for section in ('admin', 's3', 'server'):
        require(values.get(section) is None or isinstance(values[section], dict), 'invalid reference values')
    admin = values.get('admin') or {}
    if admin.get('existingSecret'):
        secret = name(admin['existingSecret'])
        for field, default in [('userKey', 'admin-user'), ('passwordKey', 'admin-password')]:
            key = admin.get(field, default)
            require(isinstance(key, str) and re.fullmatch(r'[A-Za-z0-9._-]+', key), 'invalid Secret key')
            result.add((secret, key))
    s3 = values.get('s3') or {}
    if s3.get('existingConfigSecret'):
        result.add((name(s3['existingConfigSecret']), None))
    names = (values.get('server') or {}).get('extraSecretNamesForEnvFrom', [])
    require(isinstance(names, list), 'invalid Secret reference list')
    result.update((name(n), None) for n in names)
    return result


def affected(root, apps, base):
    if base is None:
        return apps
    try:
        return select(apps, changed_paths(root, base))
    except ContractError:
        return apps


def main(argv=None):
    import argparse
    import hashlib
    import json
    parser = argparse.ArgumentParser(description='Secretless bounded Helm contract checks')
    parser.add_argument('--root', default='.')
    parser.add_argument('--helm', default='helm')
    parser.add_argument('--base')
    parser.add_argument('--profile', default=str(pathlib.Path(__file__).with_name('helm-capabilities.yaml')))
    args = parser.parse_args(argv)
    try:
        root = pathlib.Path(args.root).resolve()
        profile_path = pathlib.Path(args.profile)
        require(not profile_path.is_symlink(), 'symlink capability profile unsupported')
        profile = values_document(profile_path)
        capabilities(profile)
        apps = inventory(root)
        references = {a['name']: expected_refs(root, a) for a in apps}
        chosen = affected(root, apps, args.base)
        for app in chosen:
            docs = render(root, app, args.helm, profile)
            validate_workloads(docs, app['namespace'], references[app['name']])
            print(json.dumps({'app': app['name'], 'status': 'pass', 'documents': len(docs),
                              'contract_sha256': hashlib.sha256(json.dumps(app, sort_keys=True).encode()).hexdigest()}), flush=True)
        print(json.dumps({'status': 'pass', 'inventory': len(apps), 'selected': len(chosen), 'passed': len(chosen)}))
        return 0
    except Exception:
        # No exception text: YAML/Helm/filesystem exceptions can contain secret input.
        print(json.dumps({'status': 'fail', 'reason': 'Helm contract check failed'}), flush=True)
        return 1


class ContractError(Exception):
    """A fixed, public-safe diagnostic, never template or values output."""


def require(condition, message):
    if not condition:
        raise ContractError(message)


def local_file(root, relative):
    require(isinstance(relative, str), 'invalid local path')
    path = pathlib.PurePosixPath(relative)
    require(bool(relative) and relative == path.as_posix() and not any(ord(c) < 32 for c in relative)
            and not path.is_absolute() and '..' not in path.parts
            and '$' not in relative and ':' not in relative and '\\' not in relative,
            'unsafe local path')
    current = root
    for part in path.parts:
        current /= part
        require(not current.is_symlink(), 'symlink input unsupported')
    require(current.is_file(), 'required input missing')
    return current


def documents(path):
    try:
        return list(yaml.load_all(path.read_text(), Loader=StrictLoader))
    except (OSError, UnicodeError, yaml.YAMLError, RecursionError):
        raise ContractError('invalid YAML input') from None


def inventory(root):
    root = pathlib.Path(root).resolve()
    root_docs = documents(local_file(root, 'infra/argocd/root.yaml'))
    try:
        git_source = root_docs[0]['spec']['source']
        apps = []
        for path in sorted((root / 'infra/argocd').rglob('*')):
            require(not path.is_symlink(), 'symlink input unsupported')
            if path.suffix not in {'.yaml', '.yml'}:
                continue
            path = local_file(root, path.relative_to(root).as_posix())
            for doc in documents(path):
                require(isinstance(doc, dict) and isinstance(doc.get('kind'), str), 'invalid manifest document')
                if doc.get('kind') != 'Application':
                    continue
                spec = doc['spec']
                require(not ('sources' in spec and 'source' in spec), 'ambiguous sources')
                sources = spec.get('sources', [spec.get('source', {})])
                require(isinstance(sources, list) and bool(sources) and all(isinstance(s, dict) for s in sources), 'invalid sources')
                charts = [s for s in sources if 'chart' in s]
                if not charts:
                    require(all('path' in s for s in sources), 'missing chart or Git path')
                    continue
                require(len(charts) == 1, 'multiple chart sources unsupported')
                source = charts[0]
                require(set(source) <= {'repoURL', 'chart', 'targetRevision', 'helm'},
                        'unsupported chart source field')
                require(re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?',
                                     source['targetRevision']) is not None,
                        'chart version must be exact')
                url = urlsplit(source['repoURL'])
                require(url.scheme == 'https' and bool(url.hostname) and not url.username
                        and not url.password and not url.query and not url.fragment,
                        'only public HTTPS chart repositories supported')
                helm = source.get('helm', {})
                require(isinstance(helm, dict) and set(helm) <= {
                    'releaseName', 'valueFiles', 'kubeVersion', 'apiVersions'},
                    'unsupported Helm field')
                capabilities({k: v for k, v in helm.items() if k in {'kubeVersion', 'apiVersions'}}, partial=True)
                refs = {}
                for other in sources:
                    if other is source:
                        continue
                    require(set(other) == {'repoURL', 'targetRevision', 'ref'}
                            and other['repoURL'] == git_source['repoURL']
                            and other['targetRevision'] == git_source['targetRevision'],
                            'only same-checkout value refs supported')
                    require(other['ref'] not in refs, 'duplicate value ref')
                    refs[other['ref']] = other
                values = helm.get('valueFiles', [])
                require(isinstance(values, list), 'invalid valueFiles')
                resolved = []
                for value in values:
                    require(isinstance(value, str) and value.startswith('$')
                            and '/' in value, 'only checkout value refs supported')
                    ref, relative = value[1:].split('/', 1)
                    require(ref in refs, 'unknown value ref')
                    values_document(local_file(root, relative))
                    resolved.append(relative)
                name = doc['metadata']['name']
                release = helm.get('releaseName', name)
                namespace = spec['destination']['namespace']
                for identity in (name, release, namespace, source['chart']):
                    require(isinstance(identity, str) and re.fullmatch(
                        r'[a-z0-9][a-z0-9.-]*', identity) is not None, 'invalid chart identity')
                apps.append({'path': path.relative_to(root).as_posix(), 'name': name,
                             'release': release, 'namespace': namespace,
                             'chart': source['chart'], 'repo': source['repoURL'],
                             'version': source['targetRevision'], 'helm': helm,
                             'values': resolved})
        require(bool(apps), 'chart inventory missing')
        require(len({a['name'] for a in apps}) == len(apps), 'duplicate chart Application')
        return apps
    except (KeyError, TypeError, AttributeError, ValueError):
        raise ContractError('invalid Application declaration') from None


if __name__ == '__main__':
    raise SystemExit(main())
