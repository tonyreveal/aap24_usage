# AAP 2.4 workload collection for AAP 2.6 planning

Keep collect_aap_usage.yml and collect_usage.py in the same directory. Run from a Linux/WSL Ansible control machine with ansible-core 2.14+ and a Python version supported by that Ansible release. No extra Ansible collections or Python packages are needed. This runs against your existing 2.4 controller, not against the new 2.6 gateway.

## Run

Use an AAP account with installation-wide visibility (for example, a System Auditor) that can create and delete its own personal access tokens. The playbook authenticates with username/password, creates a read-scoped personal token, passes it to the collector, and attempts to revoke it in an `always` block even if collection fails. A read scope alone does not grant global visibility: restricted RBAC can silently return only part of the workload.

```bash
export CONTROLLER_HOST='https://controller.example.com'
read -rp 'AAP username: ' CONTROLLER_USERNAME
read -rsp 'AAP password: ' CONTROLLER_PASSWORD; echo
export CONTROLLER_USERNAME CONTROLLER_PASSWORD
ansible-playbook -i localhost, collect_aap_usage.yml \
  -e lookback_days=90 \
  -e growth_percent=30 \
  -e target_utilization=0.70
unset CONTROLLER_PASSWORD CONTROLLER_USERNAME
```

You can alternatively supply `controller_username` and `controller_password` through Ansible Vault variables. No pre-created token is needed for the playbook. Direct execution of the Python script still uses `CONTROLLER_OAUTH_TOKEN`; the playbook manages token creation and deletion around it.

The account must support API Basic authentication and personal-token creation. Browser-only SSO/MFA credentials cannot automatically be exchanged by this flow; use an appropriate API-capable account. The playbook does not change authentication settings. Authentication, collection and deletion tasks use `no_log` so passwords and token values are not printed.

Token revocation uses username/password, because the collection token has read scope. Cleanup failures fail the playbook and identify the token ID for manual removal. Process termination, machine loss, or a lost token-creation response can prevent cleanup; remove leftover tokens with description `Temporary AAP workload sizing collector` in the account's Tokens page. Token expiry follows the controller's configured policy. Check mode neither creates nor revokes tokens.

For an internal CA, append `-e ca_file=/path/to/ca-bundle.pem`. TLS verification is enabled by default. To disable certificate and hostname validation for a self-signed installation:

```bash
ansible-playbook -i localhost, collect_aap_usage.yml -e validate_certs=false
```

The `validate_certs` and `ca_file` settings apply to token creation, collection and token deletion. The Python collector also accepts `--no-validate-certs` when run directly (or `--validate-certs` to explicitly enable validation). When validation is disabled, `ca_file` is ignored. HTTPS is still used, but the server certificate and identity are not verified. The report records that validation was disabled.

Supply the controller origin only, without `/api/v2`. HTTP redirects are rejected to avoid forwarding credentials; use the final canonical HTTPS origin.

By default, output goes under a new timestamped `reports/` directory beside the playbook. Override with `-e output_dir=/secure/path/aap-baseline`. For an execution-environment run, mount a persistent output directory or retrieve it before the container exits. Check mode skips collection.

## Collected information

- `summary.md`: readable workload summary and growth scenario.
- `report.json`: current instance capacity/CPU/memory fields where exposed, instance groups, inventories and host totals, template settings, schedules, execution environments, and object counts. Historical summaries include daily/hourly launch volumes, launch types, status counts, runtime p50/p95/p99/max, creation-to-start delays, and exact interval-based peak concurrency. Includes per-template, per-execution-node and per-job-type breakdowns.
- `jobs.csv`: selected metadata, including forks and execution-node assignment where the API supplies them. Includes workflow, project-update, inventory-update, ad-hoc and system jobs from unified job history. Playbook fields are enriched from the jobs endpoint.
- `metrics.prom`: optional current Prometheus metrics snapshot. Disable with `-e collect_metrics=false`. A permissions/availability failure is reported in warnings without discarding job results.

Workload collection uses only GET requests. The playbook additionally POSTs a temporary token to `/api/v2/tokens/` and DELETEs that specific token afterward. No SSH, database access, workload configuration changes or job launches occur. No stdout, extra variables, credential objects, inventory variables or event payloads are saved. Reports still contain operational names/identifiers and metrics labels; keep them private. The token is passed through the process environment, not command-line arguments or report files.

Requests are serial, paced at 0.1 seconds, paginated at 200 rows, with bounded retries for transient HTTP/network failures. Both unified jobs and playbook jobs are queried; for very large installations begin with 7 days, then expand. Selected records are held in memory for sorting and aggregation. A hard 10,000-page limit per endpoint causes failure rather than a silently truncated report. Authentication/query errors and exhausted core-data retries fail the run. Optional topology endpoints returning 403/404 produce explicit warnings. A failed run can leave a timestamped directory; only a successful Ansible task and summary.md indicate completion.

## Reading the results

Default scenario: `ceil(observed_peak_playbook_jobs * 1.30 / 0.70)`. For an observed peak of 20, this gives a representative load-test target of 38 simultaneous jobs. The 30% growth and 30% spare capacity are separate assumptions. This is neither a node-count recommendation nor an estimate of fork capacity. Job mix, inventories, forks and event verbosity materially change resource needs. Growth of inventory size or task complexity can increase demand even if job count stays flat.

Use these steps for the 2.6 design:

1. Select a window covering month-end, patch cycles and other peaks; 90 days is a starting point. Check Cleanup Job Details retention and installation-wide account visibility. Deleted history cannot be recovered. Retain monitoring over a full representative cycle.
2. Review per-template/node peak concurrency, runtime and start-delay percentiles. Inspect busy schedules and high-fork templates in jobs.csv. Global concurrency can hide constrained instance groups or isolated execution pools; size each routing/failure domain separately. Container groups need separate pod requests, limits, quotas and startup-time measurements.
3. Gather historical controller/execution-node CPU, RSS, swap, disk latency/IOPS and network measurements from your monitoring system. Scrape `/api/v2/metrics/` repeatedly to measure event processing, queues and utilization; a one-time snapshot cannot establish their peaks. Measure database size, table/index growth, WAL, I/O and job-event retention with your DBA. Job count is not an event-volume or storage estimate.
4. Replay a representative mix on 2.6 at the growth target, retaining large inventories, fork counts, verbosity and schedule bursts. Confirm queue latency, job runtime, event ingestion and UI/API responsiveness. Observed concurrency on a saturated 2.4 cluster is constrained supply, not unconstrained demand; include backlog and service-level requirements in testing.
5. Validate the same workload with a required node unavailable (N-1). For an independently measured homogeneous pool with sustainable per-node capacity C and design demand D, choose N so `(N-1)*C >= D`; do not substitute raw job counts for C without representative benchmarking. Size control and execution planes separately. Account independently for platform gateway, private automation hub/image storage, EDA, PostgreSQL, load balancers and backups, and satisfy supported 2.6 topology/minimum requirements.

## Calculation boundaries

The UTC window ends when collection starts. Queries include jobs created before that boundary whose finish is inside/after the window, or whose finish is null; this preserves jobs that started before the window. Concurrency clips execution intervals to the window, treats equal-time finishes/starts together and excludes unstarted jobs. Running jobs end at the window boundary for this calculation. Terminal jobs missing finish times are excluded from concurrency. Null-finish historical records can appear in the CSV/status counts even if they did not execute in the window.

Created counts use creation timestamps within the window. Completed-runtime percentiles use jobs finishing in the window, across all outcomes with valid start/finish timestamps; they are not limited to successful jobs. Delay percentiles use jobs starting in the window. Creation-to-start includes workflow/dependency/scheduling delays and is not a precise measure of capacity queue time. Zero-volume hours/days are omitted from series. Workflow parents are reported separately by type; all-unified concurrency must not be added to execution concurrency. Forks are saved, but no unsupported universal conversion from forks to CPU/RAM is made.

Pagination is not a transaction. Jobs can change while collecting, and cleanup can remove records; avoid running cleanup concurrently. The report states observed retained history, not proof that the full requested period is present. Current instance/group membership and capacity do not reconstruct historical configuration. Missing API fields are omitted, not interpreted as zero.

## References

- [AAP 2.4 token-based authentication and personal access tokens](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.4/html/automation_controller_administration_guide/assembly-controller-token-based-authentication)

- [AAP 2.4 controller API overview: authentication, filtering and pagination](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.4/html-single/automation_controller_api_overview/automation_controller_api_overview)
- [AAP 2.4 controller administration: capacity, metrics and retention](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.4/html-single/automation_controller_administration_guide/automation_controller_administration_guide)
- [AAP 2.6 capacity planning](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.6/optimize-ref_controller_capacity_planning)
- [AAP 2.6 system requirements](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.6/install-assembly_system_requirements)

Validation: synthetic offline tests cover interval boundaries, running/carry-in jobs, pagination, duplicate IDs and cross-origin rejection. This package has not been run against your controller. Ansible itself was not available in the authoring environment for a live syntax/execution check.
