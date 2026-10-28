# AAP 2.5+ workload baseline: Controller, EDA and private Automation Hub

This is a separate gateway-based edition of the AAP 2.4 collector. It targets the component APIs introduced in AAP 2.5 and used in newer releases. API availability, permissions and schemas vary by installed release; optional endpoint failures are explicitly reported. It does not promise compatibility with every future release. It has been tested offline with synthetic responses, not on a live AAP cluster.

## Run

Keep `collect_aap_usage.yml` and `collect_usage.py` together. Use Linux/WSL with ansible-core 2.14+ and a Python version supported by that Ansible release. No additional collections or Python packages are required.

```bash
# Edit secrets.yml with your gateway URL, username and password.
ansible-vault encrypt secrets.yml
ansible-playbook -i localhost, collect_aap_usage.yml --ask-vault-pass \
  -e validate_certs=false
```

Use the **platform gateway** origin for `controller_url`, e.g. `https://aap.example.com`, without an API path. The existing `controller_*` variable names are retained for convenience; do not use a legacy direct Controller URL. Configure these lowercase YAML keys in secrets.yml:

```yaml
controller_url: https://aap.example.com
controller_username: your-api-user
controller_password: your-password
```

Validation defaults to true. Omit `-e validate_certs=false` to validate certificates, or supply `-e ca_file=/path/to/ca-bundle.pem` for an internal CA. The setting applies consistently to authentication, collection and cleanup. When false, the CA file is ignored and both certificate-chain and hostname checks are disabled; HTTPS remains in use.

Use an API-capable account with visibility across all required services and permission to create/delete its own personal tokens. Controller auditor access alone does not establish EDA/Hub access. The playbook creates a temporary **read-scoped gateway token** using username/password at `/api/gateway/v1/tokens/`, passes it to the collector, and attempts deletion in an `always` block, including after collection failure. Basic authentication and token creation must be allowed; browser-only SSO/MFA is not automated by this flow. Sensitive tasks use `no_log`.

Cleanup uses username/password, not the read-scoped token. Failed cleanup fails the playbook and prints only the token ID for manual removal. Abrupt termination or a lost token-creation response can leave a token behind; inspect the account's Tokens page for `Temporary AAP 2.5+ platform sizing collector`. Token expiration follows platform policy. Check mode skips the token lifecycle and collection. The only API mutations are creating and deleting this token.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `lookback_days` | `90` | Controller history window; local clipping window for EDA/Hub history |
| `growth_percent` | `30` | Controller representative job-load growth scenario |
| `target_utilization` | `0.70` | Controller spare-capacity scenario |
| `collect_eda` | `true` | Attempt optional EDA collection |
| `collect_hub` | `true` | Attempt optional private Hub collection |
| `collect_hub_artifact_sizes` | `false` | Enumerate Hub artifacts and sum logical sizes |
| `collect_metrics` | `true` | Save current Controller Prometheus metrics |
| `eda_api_path` | `/api/eda/v1/` | EDA prefix on the same gateway |
| `hub_api_path` | `/api/galaxy/pulp/api/v3/` | Hub Pulp prefix on the same gateway |
| `max_pages` | `10000` | Per-list pagination guardrail |
| `output_dir` | `reports/` beside playbook | Parent of timestamped output directories |

Example:

```bash
ansible-playbook -i localhost, collect_aap_usage.yml --ask-vault-pass \
  -e lookback_days=30 -e growth_percent=50 -e target_utilization=0.65 \
  -e collect_hub_artifact_sizes=true
```

To skip a component deliberately, use `-e collect_eda=false` or `-e collect_hub=false`. Hub prefixes are deployment-dependent; override `hub_api_path` only with the actual Pulp API prefix exposed by your gateway. Both overrides must be paths on that gateway, not URLs to other servers. The collector rejects redirects and pagination links outside the configured origin/component prefix. It does not forward gateway credentials to direct component hosts.

## Outputs and coverage

Each run creates a private timestamped directory with:

- `summary.md`: Controller workload table, growth scenario, warnings, EDA/Hub summaries and endpoint availability.
- `report.json`: detailed Controller baseline plus `eda` and `hub` sections containing endpoint status, selected records, counts and summaries.
- `jobs.csv`: selected Controller job metadata.
- `metrics.prom`: optional current Controller metrics snapshot.

Controller uses `/api/controller/v2/`. Collection includes job volume by hour/day, outcomes, launch types, duration and creation-to-start percentiles, interval-based concurrent execution peaks, per-template/node/type summaries, inventories, instance capacity and group topology, schedules, templates and execution environments. Job-specific fields are enriched from the jobs endpoint. Platform-wide users/organizations are not counted through deprecated Controller endpoints in this edition.

EDA uses `/api/eda/v1/`. Collection includes activations, activation instances, projects, rulebook metadata, decision environments and event streams. Summaries include enabled activation counts, current status distribution, available restart/rules-fired counter snapshots, execution duration, daily starts and peak simultaneous retained activation processes. Available queue/activation identifiers are retained for inspection. Fields absent in a release are omitted, and counter coverage is reported.

Hub uses the Pulp API behind the gateway. Collection includes Ansible repositories, container sync/push repositories, distributions, background tasks, collection-version/manifest/tag/artifact counts, component versions and online worker/content-app metadata. If exposed, Pulp status storage figures are saved. Task summaries show outcomes, duration, daily starts, concurrency and task-name counts. Optional artifact scanning sums catalogued logical bytes and reports its completeness; it does not measure filesystem or object-store usage.

Endpoint states distinguish `collected`, `partial`, `forbidden` (403), `authentication_error` (401), `not_found` (404), `unsupported` (405), and other `error` results. Component status is `collected`, `partial`, `unavailable` or explicitly `disabled`. **Unavailable does not mean not installed**: a missing gateway route, API change or access policy can produce the same symptom. A successful empty list means zero permission-visible records, not necessarily zero installation-wide activity. Missing history produces a null summary rather than zero concurrency. Review endpoint status and RBAC before using the numbers.

## Collection cost and boundaries

Requests are serial and paced at 0.1 seconds, with bounded retries for transient failures. Controller queries use server-side time filters and 200-row pages. EDA uses 100-row pages; Hub uses 100-row limit/offset pages. EDA/Hub execution histories are scanned across available retention and then filtered locally, avoiding reliance on date-filter syntax that differs by release and preserving carry-in executions. **Reducing lookback_days does not reduce EDA/Hub history scan cost.** For a large installation, lower max_pages initially or disable optional components for the first run. Page-limit/cycle/schema failures mark optional data partial rather than silently treating it as complete; Controller core-history failure stops the run. Records and aggregation are held in memory.

The UTC window ends at collection start. Execution intervals are clipped to that window, with equal-time starts/finishes merged; unfinished executions count only when their reported state is running. History with invalid/inverted timestamps is excluded and counted for EDA/Hub. Records lacking both execution timestamps cannot contribute to duration/concurrency. Old unfinished records may remain visible in status totals. EDA/Hub raw history sections retain only overlapping or unfinished records after scanning. The retrieved_count records the pre-filter scan count.

Controller completed-runtime percentiles include all outcomes with valid timestamps finishing in the window. Creation-to-start delays include dependencies, workflow waits and scheduling; they are not precise capacity-queue measurements. Workflow parents are included in all-unified figures and separated from execution jobs; do not sum those concurrency figures. Historical page reads are not transactional. Avoid simultaneous cleanup, and account for deleted history and changing RBAC.

No stdout, extra variables, credential objects, rulebook bodies, event payloads, task arguments/errors or remote configuration URLs are written to reports. EDA/Hub record fields are allowlisted. Names, IDs, queue names, node metadata and metrics labels remain operational data. Keep reports private. The token is passed in the collector's environment; the inherited CONTROLLER_PASSWORD environment variable is cleared for the collector process. Direct execution of Python still expects CONTROLLER_HOST, CONTROLLER_OAUTH_TOKEN and optional CONTROLLER_CA_FILE; the playbook handles password-to-token exchange.

## Using the baseline for sizing

The Controller scenario is `ceil(observed_peak_playbook_jobs * (1 + growth_percent/100) / target_utilization)`. It is a representative load-test concurrency target, not a CPU/RAM or node-count recommendation. Preserve inventory size, fork counts, task complexity and schedule bursts when testing. Queueing on a saturated source installation can make observed concurrency understate demand. Size individual execution pools and control capacity separately, and test the required workload with a node unavailable (N-1).

EDA activations are long-running processes: peak simultaneous instances and current enabled activations help plan process capacity, but cannot establish events/second or per-activation memory. Restart and rules-fired counters have reset lifetimes; they are not lookback-window totals. Measure ingress rates, event sizes, rule complexity, processing lag, Redis/database load, per-process CPU/RAM and activation failover behavior over representative peaks. Controller jobs launched by EDA already appear in Controller history; do not add EDA activations to Controller job counts.

Hub background tasks measure sync/import/maintenance processing, not registry image pulls or collection downloads. Measure gateway/registry request rates, response bytes, network saturation, cache effectiveness, sync/import bursts and storage IOPS. Count image architectures/layers, deduplication, retention, replication and backups separately. Manifest/tag counts are not distinct image counts; logical artifact bytes do not include database/WAL, temporary import space or all storage overhead.

For every component, combine retained API history with time-series CPU/RAM/disk/network measurements and a representative load test on the target installation. The one-time Controller metrics and Hub status snapshots cannot establish historical utilization peaks. Plan gateway, database, Redis, load-balancer and backup resources independently and follow the supported topology/minimum requirements for the exact target release.

## References and validation

- [Red Hat: platform gateway and component API changes since 2.5](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.6/upgrade-assembly_upgrade_api_changes)
- [Red Hat: AAP 2.5 automation execution API overview](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.5/pdf/automation_execution_api_overview/Red_Hat_Ansible_Automation_Platform-2.5-Automation_execution_API_overview-en-US.pdf)
- [EDA upstream API routes](https://github.com/ansible/eda-server/blob/main/src/aap_eda/api/urls.py) and [activation serializers](https://github.com/ansible/eda-server/blob/main/src/aap_eda/api/serializers/activation.py); the installed version's API documentation is authoritative.
- [Galaxy NG: Galaxy and Pulp API layout](https://docs.ansible.com/projects/galaxy-ng/en/latest/dev/developer_guide/rest_api.html)
- [Automation Hub API reference](https://galaxy.ansible.com/api/v3/swagger-ui/); use your installation's Pulp OpenAPI for its specific version and prefix.
- [AAP 2.6 capacity planning](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.6/optimize-ref_controller_capacity_planning)

Run the included offline tests with `python3 test_collector.py`. Fifteen offline tests passed, covering Controller calculations, TLS defaults/override, pagination and duplicates, Pulp IDs/relative links, unavailable/forbidden components, partial page limits, metadata filtering, EDA carry-in executions, Hub artifact aggregation, schema failures and same-origin restrictions. Ansible is unavailable in the authoring environment; live Ansible execution, gateway token exchange and component compatibility remain unverified on your installation.
