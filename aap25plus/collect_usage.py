#!/usr/bin/env python3
"""Read-only AAP 2.5+ platform usage collector. Python 3.8+, standard library only."""
import argparse
import collections
import csv
import datetime as dt
import json
import math
import os
from pathlib import Path
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UTC = dt.timezone.utc
JOB_FIELDS = ('id type name status created started finished elapsed launch_type '
              'job_template unified_job_template inventory execution_environment '
              'execution_node controller_node forks job_slice_number job_slice_count').split()
RESOURCE_FIELDS = ('id name hostname node_type capacity consumed_capacity percent_capacity_remaining '
                   'enabled managed cpu memory version instances is_container_group '
                   'policy_instance_percentage policy_instance_minimum policy_instance_list '
                   'total_hosts total_groups organization kind forks job_slice_count '
                   'allow_simultaneous execution_environment instance_groups next_run '
                   'unified_job_template').split()


def stamp(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(UTC) if value else None


def iso(value):
    return value.isoformat().replace('+00:00', 'Z')


def select(row, fields):
    return {key: row[key] for key in fields if key in row}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the bearer token through a redirect.


class API:
    def __init__(self, base, token, ca_file=None, validate_certs=True,
                 api_path='/api/controller/v2/', max_pages=10000):
        self.base = base.rstrip('/') + '/'
        parsed = urllib.parse.urlsplit(self.base)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.query or parsed.fragment or parsed.path != '/':
            raise ValueError('CONTROLLER_HOST must be an HTTPS origin without a path')
        self.origin = (parsed.scheme, parsed.netloc)
        self.token = token
        if not api_path.startswith('/api/') or not api_path.endswith('/') or any(x in api_path for x in ('..', '?', '#', '//')):
            raise ValueError('Component API path must be an absolute /api/.../ prefix')
        self.api_path = api_path
        self.max_pages = max_pages
        if validate_certs:
            context = ssl.create_default_context(cafile=ca_file or None)
        else:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(
            context=context))

    def get(self, path, raw=False):
        url = urllib.parse.urljoin(self.base, path)
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme, parsed.netloc) != self.origin or not parsed.path.startswith(self.api_path):
            raise ValueError('Refusing API link outside the configured controller API')
        for attempt in range(5):
            time.sleep(.1)
            request = urllib.request.Request(url, headers={
                'Authorization': 'Bearer ' + self.token,
                'Accept': 'text/plain' if raw else 'application/json'})
            try:
                with self.opener.open(request, timeout=60) as response:
                    body = response.read().decode('utf-8')
                    return body if raw else json.loads(body)
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 4:
                    raise RuntimeError('HTTP %s from %s' % (exc.code, parsed.path)) from None
                time.sleep(2 ** attempt)
            except urllib.error.URLError:
                if attempt == 4:
                    raise RuntimeError('Connection/TLS failure for ' + parsed.path) from None
                time.sleep(2 ** attempt)

    def pages(self, endpoint, params=None):
        path = self.api_path + endpoint + '/?' + urllib.parse.urlencode(params or {})
        seen, visited = set(), set()
        for _ in range(self.max_pages):
            if path in visited:
                raise RuntimeError('Pagination cycle for ' + endpoint)
            visited.add(path)
            page = self.get(path)
            if not isinstance(page, dict) or not isinstance(page.get('results'), list) or 'next' not in page:
                raise RuntimeError('Unsupported pagination schema for ' + endpoint)
            for row in page['results']:
                key = row.get('id', row.get('pulp_href'))
                if key is None:
                    raise RuntimeError('Missing record identity for ' + endpoint)
                if key not in seen:
                    seen.add(key)
                    yield row
            next_path = page.get('next')
            if not next_path:
                return
            path = urllib.parse.urljoin(urllib.parse.urljoin(self.base, path), next_path)
        raise RuntimeError('Page limit reached for ' + endpoint + '; report incomplete')


def distribution(values):
    values = sorted(values)
    if not values:
        return {'n': 0, 'p50': None, 'p95': None, 'p99': None, 'max': None}
    return dict(n=len(values), **{name: round(values[max(0, math.ceil(p * len(values)) - 1)], 3)
                                 for name, p in [('p50', .5), ('p95', .95), ('p99', .99), ('max', 1)]})


def concurrency(jobs, start, end):
    # Half-open intervals, merged at ties to avoid phantom peaks.
    events = collections.defaultdict(int)
    for job in jobs:
        a, b = stamp(job.get('started')), stamp(job.get('finished'))
        if a is None or (b is None and job.get('status') != 'running'):
            continue
        a, b = max(a, start), min(b or end, end)
        if b > a:
            events[a] += 1
            events[b] -= 1
    current = peak = 0
    area = 0.0
    previous = start
    peak_at = None
    for moment, delta in sorted(events.items()):
        area += current * (moment - previous).total_seconds()
        current += delta
        if current > peak:
            peak, peak_at = current, iso(moment)
        previous = moment
    return {'peak': peak, 'first_peak_at_utc': peak_at,
            'time_weighted_mean': round(area / (end - start).total_seconds(), 4)}


def summarize(jobs, start, end):
    completed = [j for j in jobs if j.get('finished') and start <= stamp(j['finished']) < end]
    durations = [(stamp(j['finished']) - stamp(j['started'])).total_seconds()
                 for j in completed if j.get('started') and stamp(j['finished']) >= stamp(j['started'])]
    delays = [(stamp(j['started']) - stamp(j['created'])).total_seconds()
              for j in jobs if j.get('started') and j.get('created')
              and start <= stamp(j['started']) < end and stamp(j['started']) >= stamp(j['created'])]
    launched = [j for j in jobs if j.get('created') and start <= stamp(j['created']) < end]
    daily = collections.Counter(iso(stamp(j['created']))[:10] for j in launched)
    hourly = collections.Counter(iso(stamp(j['created']))[:13] + ':00Z' for j in launched)
    return {'records': len(jobs), 'created_in_window': len(launched), 'finished_in_window': len(completed),
            'statuses_at_collection': dict(collections.Counter(j['status'] for j in jobs)),
            'launch_types': dict(collections.Counter(j.get('launch_type', 'unknown') for j in launched)),
            'completed_runtime_seconds': distribution(durations),
            'created_to_started_seconds': distribution(delays), 'concurrency': concurrency(jobs, start, end),
            'daily_created_utc': dict(sorted(daily.items())), 'hourly_created_utc': dict(sorted(hourly.items())),
            'peak_hour_created': max(hourly.values(), default=0), 'peak_day_created': max(daily.values(), default=0)}


def grouped(jobs, key, start, end):
    groups = collections.defaultdict(list)
    for job in jobs:
        groups[str(key(job))].append(job)
    return {name: summarize(rows, start, end) for name, rows in sorted(groups.items())}


# Fields are allowlisted: never persist rulebooks, event payloads, credentials,
# extra variables, task errors/arguments, or remote URLs.
EDA_FIELDS = ('id name status is_enabled restart_count restart_policy rules_count '
              'rules_fired_count decision_environment_id project_id rulebook_id '
              'organization_id created_at modified_at started_at ended_at '
              'activation_id queue_name log_level skip_audit_events').split()
HUB_FIELDS = ('pulp_href name pulp_created pulp_last_updated state started_at '
              'finished_at worker latest_version_href retain_repo_versions '
              'base_path repository repository_version').split()


def failure_status(exc):
    message = str(exc)
    for prefix, status in [('HTTP 401 ', 'authentication_error'), ('HTTP 403 ', 'forbidden'),
                           ('HTTP 404 ', 'not_found'), ('HTTP 405 ', 'unsupported')]:
        if message.startswith(prefix):
            return status
    return 'error'


def read_list(api, endpoint, fields, params):
    rows = []
    try:
        for row in api.pages(endpoint, params):
            # Scalar metadata only: future nested fields must be explicitly reviewed.
            rows.append({key: value for key, value in select(row, fields).items()
                         if value is None or isinstance(value, (str, int, float, bool))})
        return {'status': 'collected', 'records': rows, 'retrieved_count': len(rows)}
    except (RuntimeError, ValueError, KeyError) as exc:
        return {'status': 'partial' if rows else failure_status(exc),
                'error': str(exc), 'records': rows, 'retrieved_count': len(rows)}


def read_count(api, endpoint):
    try:
        data = api.get(api.api_path + endpoint + '/?limit=1')
        if not isinstance(data, dict) or not isinstance(data.get('count'), int):
            raise RuntimeError('Unsupported count schema for ' + endpoint)
        return {'status': 'collected', 'count': data['count']}
    except (RuntimeError, ValueError, KeyError) as exc:
        return {'status': failure_status(exc), 'error': str(exc), 'count': None}


def component_status(endpoints):
    states = [entry['status'] for entry in endpoints.values()]
    if all(state == 'collected' for state in states):
        return 'collected'
    if any(state in ('collected', 'partial') for state in states):
        return 'partial'
    return 'unavailable'  # Does not assert that the service is not installed.


def history(rows, start, end, finished_field):
    """Normalize only retained executions overlapping the requested window."""
    result = []
    invalid = 0
    for row in rows:
        a, b = row.get('started_at'), row.get(finished_field)
        try:
            start_at, finish_at = stamp(a), stamp(b)
            if start_at and start_at >= end:
                continue
            if finish_at and finish_at < start:
                continue
            if start_at and finish_at and finish_at < start_at:
                invalid += 1
                continue
        except (ValueError, TypeError):
            invalid += 1
            continue
        result.append({'id': row.get('id', row.get('pulp_href')), 'name': row.get('name'),
                       'status': str(row.get('status', row.get('state', 'unknown'))).lower(),
                       'started': a, 'finished': b,
                       'activation_id': row.get('activation_id'), 'worker': row.get('worker')})
    return result, invalid


def history_summary(rows, start, end):
    # Unlike Controller jobs, these APIs don't always expose a reliable queued timestamp.
    finished = [row for row in rows if row['started'] and row['finished']
                and start <= stamp(row['finished']) < end]
    started = [row for row in rows if row['started'] and start <= stamp(row['started']) < end]
    return {'retained_records_overlapping_window_or_unfinished': len(rows),
            'started_in_window': len(started), 'completed_with_timestamps_in_window': len(finished),
            'statuses_at_collection': dict(collections.Counter(row['status'] for row in rows)),
            'completed_runtime_seconds': distribution([(stamp(row['finished']) - stamp(row['started'])).total_seconds()
                                                       for row in finished]),
            'execution_concurrency': concurrency(rows, start, end),
            'daily_starts_utc': dict(sorted(collections.Counter(iso(stamp(row['started']))[:10] for row in started).items()))}


def collect_eda(api, start, end):
    endpoints = {}
    for name in ('activations', 'activation-instances', 'projects', 'rulebooks', 'decision-environments', 'event-streams'):
        endpoints[name] = read_list(api, name, EDA_FIELDS, {'page_size': 100})
    activations = endpoints['activations']['records']
    instances, invalid = history(endpoints['activation-instances']['records'], start, end, 'ended_at')
    endpoints['activation-instances']['records'] = instances
    known = lambda name: endpoints[name]['status'] == 'collected'
    summary = {
        'activation_count': len(activations) if known('activations') else None,
        'enabled_activations': sum(row.get('is_enabled') is True for row in activations) if known('activations') else None,
        'activation_status_counts': dict(collections.Counter(row.get('status', 'unknown') for row in activations)),
        'restart_counters_snapshot_sum': sum(row.get('restart_count') or 0 for row in activations) if any('restart_count' in row for row in activations) else None,
        'rules_fired_counters_snapshot_sum': sum(row.get('rules_fired_count') or 0 for row in activations) if any('rules_fired_count' in row for row in activations) else None,
        'counter_field_coverage': {key: sum(key in row for row in activations) for key in ('restart_count', 'rules_fired_count')},
        'activation_execution_history': history_summary(instances, start, end) if endpoints['activation-instances']['status'] in ('collected', 'partial') else None,
        'invalid_timestamp_records_excluded': invalid,
        'notes': ['Counters are snapshots with component-specific reset lifetimes, not totals for the lookback period.',
                  'Execution history reflects retained activation processes, not incoming event counts or rates.',
                  'Incomplete endpoints make historical peaks lower bounds; missing fields are not evidence of zero usage.']}
    return {'status': component_status(endpoints), 'api_path': api.api_path, 'summary': summary, 'endpoints': endpoints}


def collect_hub(api, start, end, artifact_sizes=False):
    endpoints = {}
    for name in ('repositories/ansible/ansible', 'repositories/container/container',
                 'repositories/container/container-push', 'distributions/ansible/ansible',
                 'distributions/container/container', 'tasks'):
        endpoints[name] = read_list(api, name, HUB_FIELDS, {'limit': 100})
    for name in ('content/ansible/collection_versions', 'content/container/manifests',
                 'content/container/tags', 'artifacts'):
        endpoints[name] = read_count(api, name)
    # Pulp status reports component versions, workers and optional storage figures.
    try:
        status = api.get(api.api_path + 'status/')
        if not isinstance(status, dict) or 'versions' not in status:
            raise RuntimeError('Unsupported Pulp status schema')
        filtered = {}
        for key in ('versions', 'online_workers', 'online_content_apps'):
            if isinstance(status.get(key), list):
                filtered[key] = [select(row, ('component', 'version', 'name', 'last_heartbeat'))
                                 for row in status[key] if isinstance(row, dict)]
        if isinstance(status.get('storage'), dict):
            filtered['storage'] = select(status['storage'], ('total', 'used', 'free'))
        endpoints['status'] = {'status': 'collected', 'data': filtered}
    except (RuntimeError, ValueError, KeyError) as exc:
        endpoints['status'] = {'status': failure_status(exc), 'error': str(exc)}
    tasks, invalid = history(endpoints['tasks']['records'], start, end, 'finished_at')
    endpoints['tasks']['records'] = tasks
    summary = {'background_tasks': history_summary(tasks, start, end) if endpoints['tasks']['status'] in ('collected', 'partial') else None,
               'tasks_by_name': dict(collections.Counter(row.get('name') or 'unknown' for row in tasks)),
               'invalid_timestamp_records_excluded': invalid,
               'content_counts': {name: endpoints[name].get('count') for name in
                                  ('content/ansible/collection_versions', 'content/container/manifests', 'content/container/tags', 'artifacts')},
               'notes': ['Background tasks represent sync/import/maintenance work, not collection downloads or image pulls.',
                         'Manifest counts include image indexes/architectures; tags and manifests are not distinct image counts.',
                         'Artifact bytes are logical catalogued sizes, not disk usage, database size, registry traffic or backup requirements.',
                         'Task history is scanned across retention, then clipped locally to preserve carry-in executions.']}
    if artifact_sizes:
        sizes = read_list(api, 'artifacts', ('pulp_href', 'size'), {'limit': 100})
        valid = [row['size'] for row in sizes['records'] if isinstance(row.get('size'), (int, float)) and row['size'] >= 0]
        summary['artifact_sizes'] = {'status': sizes['status'], 'records_scanned': len(sizes['records']),
                                     'records_with_size': len(valid), 'observed_logical_bytes': sum(valid),
                                     'complete': sizes['status'] == 'collected' and len(valid) == len(sizes['records'])}
        endpoints['artifact_size_scan'] = {key: value for key, value in sizes.items() if key != 'records'}
    return {'status': component_status(endpoints), 'api_path': api.api_path, 'summary': summary, 'endpoints': endpoints}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--days', type=float, default=90)
    parser.add_argument('--growth', type=float, default=30)
    parser.add_argument('--utilization', type=float, default=.70)
    parser.add_argument('--metrics', dest='metrics', action='store_true')
    parser.add_argument('--no-metrics', dest='metrics', action='store_false')
    tls = parser.add_mutually_exclusive_group()
    tls.add_argument('--validate-certs', dest='validate_certs', action='store_true',
                     help='Validate TLS certificates and hostnames (default)')
    tls.add_argument('--no-validate-certs', dest='validate_certs', action='store_false',
                     help='Disable TLS certificate and hostname validation; ignore CA file')
    parser.set_defaults(validate_certs=True)
    for option in ('eda', 'hub', 'hub-artifact-sizes'):
        group = parser.add_mutually_exclusive_group()
        group.add_argument('--' + option, dest=option.replace('-', '_'), action='store_true')
        group.add_argument('--no-' + option, dest=option.replace('-', '_'), action='store_false')
    parser.set_defaults(eda=True, hub=True, hub_artifact_sizes=False)
    parser.add_argument('--eda-api-path', default='/api/eda/v1/')
    parser.add_argument('--hub-api-path', default='/api/galaxy/pulp/api/v3/')
    parser.add_argument('--max-pages', type=int, default=10000)
    args = parser.parse_args()
    if args.max_pages < 1:
        raise ValueError('max-pages must be positive')
    if not (math.isfinite(args.days) and args.days > 0 and math.isfinite(args.growth)
            and args.growth >= 0 and 0 < args.utilization <= 1):
        raise ValueError('Invalid window, growth or utilization')
    token = os.environ.get('CONTROLLER_OAUTH_TOKEN', '')
    if not token:
        raise ValueError('CONTROLLER_OAUTH_TOKEN is required')
    api = API(os.environ.get('CONTROLLER_HOST', ''), token, os.environ.get('CONTROLLER_CA_FILE'),
              validate_certs=args.validate_certs, max_pages=args.max_pages)
    end = dt.datetime.now(UTC)
    start = end - dt.timedelta(days=args.days)
    os.umask(0o077)
    output = Path(args.output) / end.strftime('%Y%m%dT%H%M%S.%fZ')
    output.mkdir(parents=True, exist_ok=False)
    warnings = ['Retained, permission-visible history only; cleanup/deletion can understate demand.',
                'Pagination is not a transactional snapshot; avoid cleanup during collection.',
                'Created-to-started includes workflow/dependency/scheduling delays, not just capacity waits.',
                'Topology and metrics are current snapshots, not historical utilization.',
                'Running intervals are censored at window end; terminal records without finish times are excluded.',
                'API history does not measure historical CPU/RAM, event ingress rates, registry downloads or database I/O.']
    if not args.validate_certs:
        warnings.append('TLS certificate and hostname validation was disabled for this collection.')
    resources = {}
    for endpoint in ('instances', 'instance_groups', 'inventories', 'job_templates',
                     'workflow_job_templates', 'execution_environments', 'schedules'):
        try:
            resources[endpoint] = [select(row, RESOURCE_FIELDS) for row in api.pages(
                endpoint, {'page_size': 200, 'order_by': 'id'})]
        except RuntimeError as exc:
            if str(exc).startswith(('HTTP 403 ', 'HTTP 404 ')):
                resources[endpoint] = None
                warnings.append(str(exc) + '; topology unavailable')
            else:
                raise
    counts = {}
    for endpoint in ('hosts', 'projects'):
        try:
            counts[endpoint] = api.get(api.api_path + endpoint + '/?page_size=1')['count']
        except RuntimeError as exc:
            if str(exc).startswith(('HTTP 403 ', 'HTTP 404 ')):
                counts[endpoint] = None
                warnings.append(str(exc) + '; count unavailable')
            else:
                raise
    params = {'page_size': 200, 'order_by': 'id', 'created__lt': iso(end),
              'or__finished__gte': iso(start), 'or__finished__isnull': 'true'}
    jobs = [select(row, JOB_FIELDS) for row in api.pages('unified_jobs', params)]
    # Unified job serializers can omit playbook-specific fields. Enrich from jobs.
    details = {row['id']: select(row, JOB_FIELDS) for row in api.pages('jobs', params)}
    for job in jobs:
        if job['type'] == 'job' and job['id'] in details:
            job.update(details[job['id']])
    if not jobs:
        warnings.append('No jobs returned: verify RBAC, retention and selected window.')
    playbook = [j for j in jobs if j['type'] == 'job']
    execution = [j for j in jobs if j['type'] in ('job', 'project_update', 'inventory_update', 'ad_hoc_command', 'system_job')]
    baseline = concurrency(playbook, start, end)['peak']
    target = math.ceil(baseline * (1 + args.growth / 100) / args.utilization)
    report = {'window_start_utc': iso(start), 'window_end_utc': iso(end),
              'collected_at_utc': iso(dt.datetime.now(UTC)), 'warnings': warnings,
              'current_object_counts': counts, 'current_resources': resources,
              'all_unified_jobs': summarize(jobs, start, end), 'execution_jobs': summarize(execution, start, end),
              'playbook_jobs': summarize(playbook, start, end),
              'by_job_type': grouped(jobs, lambda j: j['type'], start, end),
              'by_template': grouped(playbook, lambda j: j.get('job_template') or j.get('unified_job_template') or 'deleted/unknown', start, end),
              'by_execution_node': grouped(execution, lambda j: j.get('execution_node') or 'unknown', start, end),
              'growth_scenario': {'growth_percent': args.growth, 'target_utilization': args.utilization,
                                  'observed_peak_playbook_jobs': baseline,
                                  'target_concurrent_equivalent_playbook_jobs': target,
                                  'meaning': 'Load-test target assuming unchanged job mix/timing; not node count or fork capacity.'}}
    if args.metrics:
        try:
            (output / 'metrics.prom').write_text(api.get(api.api_path + 'metrics/', raw=True), encoding='utf-8')
        except RuntimeError as exc:
            warnings.append(str(exc) + '; optional metrics snapshot unavailable')
    def component_api(path):
        return API(os.environ.get('CONTROLLER_HOST', ''), token,
                   os.environ.get('CONTROLLER_CA_FILE'), args.validate_certs,
                   api_path=path, max_pages=args.max_pages)
    report['eda'] = collect_eda(component_api(args.eda_api_path), start, end) if args.eda else {'status': 'disabled'}
    report['hub'] = collect_hub(component_api(args.hub_api_path), start, end, args.hub_artifact_sizes) if args.hub else {'status': 'disabled'}
    for component in ('eda', 'hub'):
        if report[component]['status'] not in ('collected', 'disabled'):
            warnings.append(component.upper() + ' collection is ' + report[component]['status'] + '; inspect endpoint results.')
    (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    with (output / 'jobs.csv').open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=JOB_FIELDS)
        writer.writeheader()
        for job in jobs:
            writer.writerow({k: "'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@', '\t', '\r')) else v
                             for k, v in job.items()})
    lines = ['# AAP workload baseline', '', 'UTC window: %s to %s' % (iso(start), iso(end)), '',
             '| Workload | Created | Finished | Peak concurrent | Runtime p95 (s) | Start delay p95 (s) |',
             '|---|---:|---:|---:|---:|---:|']
    for key in ('all_unified_jobs', 'execution_jobs', 'playbook_jobs'):
        row = report[key]
        lines.append('| %s | %s | %s | %s | %s | %s |' % (key, row['created_in_window'], row['finished_in_window'],
                     row['concurrency']['peak'], row['completed_runtime_seconds']['p95'], row['created_to_started_seconds']['p95']))
    lines += ['', 'All-unified concurrency includes workflow parents; do not add it to execution concurrency.', '',
              'Growth scenario: %s%% growth at %s%% target utilization gives **%s equivalent concurrent playbook jobs**.' % (
                  args.growth, args.utilization * 100, target),
              'This is a load-test target, not a hardware recommendation. Validate N-1 capacity separately.', '',
              'See report.json for per-type, per-template, per-node and daily detail; jobs.csv contains selected metadata.', '',
              '## Limitations', ''] + ['- ' + warning for warning in warnings]
    lines += ['', '## Optional components', '']
    for component in ('eda', 'hub'):
        section = report[component]
        lines += ['### ' + component.upper(), '', 'Collection status: ' + section['status'], '']
        if 'summary' in section:
            lines += ['```json', json.dumps(section['summary'], indent=2), '```', '']
        for name, result in section.get('endpoints', {}).items():
            lines.append('- ' + name + ': ' + result['status'] + (' (' + result['error'] + ')' if 'error' in result else ''))
    (output / 'summary.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('Report written to ' + str(output.resolve()))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        print('Collection failed: ' + str(exc), file=sys.stderr)
        sys.exit(1)
