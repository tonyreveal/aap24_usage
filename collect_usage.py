#!/usr/bin/env python3
"""Read-only AAP 2.4 usage collector. Python 3.8+, standard library only."""
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
    def __init__(self, base, token, ca_file=None, validate_certs=True):
        self.base = base.rstrip('/') + '/'
        parsed = urllib.parse.urlsplit(self.base)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.query or parsed.fragment or parsed.path != '/':
            raise ValueError('CONTROLLER_HOST must be an HTTPS origin without a path')
        self.origin = (parsed.scheme, parsed.netloc)
        self.token = token
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
        if (parsed.scheme, parsed.netloc) != self.origin or not parsed.path.startswith('/api/v2/'):
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
        path = '/api/v2/' + endpoint + '/?' + urllib.parse.urlencode(params or {})
        seen = set()
        for _ in range(10000):
            page = self.get(path)
            for row in page['results']:
                if row['id'] not in seen:
                    seen.add(row['id'])
                    yield row
            path = page.get('next')
            if not path:
                return
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
    args = parser.parse_args()
    if not (math.isfinite(args.days) and args.days > 0 and math.isfinite(args.growth)
            and args.growth >= 0 and 0 < args.utilization <= 1):
        raise ValueError('Invalid window, growth or utilization')
    token = os.environ.get('CONTROLLER_OAUTH_TOKEN', '')
    if not token:
        raise ValueError('CONTROLLER_OAUTH_TOKEN is required')
    api = API(os.environ.get('CONTROLLER_HOST', ''), token, os.environ.get('CONTROLLER_CA_FILE'),
              validate_certs=args.validate_certs)
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
                'Controller data does not size private automation hub, EDA, gateway or database resources.']
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
    for endpoint in ('hosts', 'projects', 'organizations', 'users'):
        try:
            counts[endpoint] = api.get('/api/v2/' + endpoint + '/?page_size=1')['count']
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
            (output / 'metrics.prom').write_text(api.get('/api/v2/metrics/', raw=True), encoding='utf-8')
        except RuntimeError as exc:
            warnings.append(str(exc) + '; optional metrics snapshot unavailable')
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
    (output / 'summary.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('Report written to ' + str(output.resolve()))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        print('Collection failed: ' + str(exc), file=sys.stderr)
        sys.exit(1)
