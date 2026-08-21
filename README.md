# DCV Observability

DCV Observability is a read-only NICE DCV session observability and troubleshooting application intended to run locally on a NICE DCV Linux server. It combines DCV CLI session information, Linux host and process information, and bounded evidence from NICE DCV logs.

Its purpose is troubleshooting and evidence gathering. It does not actively measure client latency, frames per second, bandwidth, or client network quality, and it does not treat normalized log evidence as a definitive diagnosis.

## Features

- Running DCV session count, owner, type, state, and age.
- CPU and memory percentages summed from the session's `Xdcv` and `dcvagent` processes. These values do not include every process owned by the user.
- Host memory usage, 1/5/15-minute load averages, CPU count, and load per CPU.
- DCV version and local hostname.
- TCP 8443 and UDP/QUIC 8443 listener detection. A UDP listener does not prove that a particular client negotiated QUIC.
- Optional NVIDIA GPU, memory, and encoder information when `nvidia-smi` is available.
- Recent session issue counts based on bounded log evidence from the past 24 hours.
- Session-specific evidence with severity/category filters, normalized and raw messages, source filenames, source line numbers, and nearby context.
- Optional UTC date/time range filtering and case-insensitive literal log-text search.
- Sortable session and evidence tables.
- Manual refresh and non-blocking background initial collection.

## Architecture

```text
Browser
   |
   v
FastAPI / Uvicorn
   |
   +-- NICE DCV CLI
   +-- Linux host/process information
   +-- /var/log/dcv
   |
   v
Read-only troubleshooting evidence
```

The service runs on the DCV host and stores current collection results in process memory. There is no external or persistent application database. Uvicorn serves the dashboard and API. The initial synchronous collector is run in a worker thread so it does not block HTTP startup.

## Supported / Target Platforms

Validated environment:

- CentOS 7 x86_64
- NICE DCV 2023.1
- Private Python 3.11.16
- Private OpenSSL 3.5.7

The current private runtime targets Linux x86_64 with glibc 2.17 or newer. CentOS 7 is its validated build and relocation baseline.

The following systems are targeted for validation, but are not yet validated:

- Rocky Linux 8
- Rocky Linux 9
- Amazon Linux

Unknown operating systems are reported as `UNVALIDATED`; this is not a claim of compatibility.

## Why a Private Python Runtime

DCV servers can have an old system Python, a Python build without working SSL, or OS-owned Python packages that administrators should not replace. DCV Observability therefore uses a private runtime:

- `.runtime/python/` contains private Python and OpenSSL binaries/libraries.
- `.venv/` is the application virtual environment created locally from that private Python.

The launcher scopes the bundled OpenSSL library path to private-runtime and application processes. It does not replace system Python, modify system Python packages, replace system OpenSSL, edit `/etc/ld.so.conf`, run `ldconfig`, or require Conda.

## Runtime Distribution

The private runtime tarball is intentionally not stored in Git. It is distributed as a release asset. The repository retains the authoritative checksum file:

```text
runtime/dist/dcvobs-python-3.11-linux-x86_64-glibc217.tar.gz.sha256
```

When the installed runtime is absent or unhealthy, the launcher:

1. Looks for the approved tarball under `runtime/dist/`.
2. Uses a cached download under `.runtime/cache/`, or downloads the release asset when needed.
3. Verifies the tarball against the committed SHA256 metadata.
4. Rejects a checksum mismatch before extraction.
5. Rejects absolute paths, traversal components, an unexpected archive root, and unsafe symbolic-link targets.
6. Extracts into `.runtime/python/`.
7. Validates Python version and architecture, bundled OpenSSL, `ssl`, `hashlib`, `venv`, and a temporary venv with pip.

Downloads use `curl`, with `wget` as a fallback. TLS certificate verification remains enabled. Downloads use a temporary partial file, and failed or checksum-invalid downloads are removed.

By default, the GitHub Release URL is derived from the clone's `origin` remote, the runtime version, and the artifact name in `runtime/runtime.conf`. An administrator can point the same bootstrap at another approved HTTPS artifact service with `DCVOBS_RUNTIME_URL`; the committed SHA256 remains authoritative.

## Private GitHub Repository Authentication

Public release assets need no token. A private GitHub release may require a token with repository read access. No token is stored in the repository.

```bash
read -s -p "GitHub token: " DCVOBS_GITHUB_TOKEN
echo
export DCVOBS_GITHUB_TOKEN
sudo --preserve-env=DCVOBS_GITHUB_TOKEN ./dcvobs start
unset DCVOBS_GITHUB_TOKEN
```

The launcher puts the authorization header in a temporary mode-0600 download client configuration, does not print it, and removes the configuration after the request. `sudo --preserve-env` is explicit because ordinary sudo policy commonly removes custom environment variables.

## Prerequisites

Mandatory for the current release:

- Linux x86_64 with glibc 2.17 or newer.
- NICE DCV installed locally and its `dcv` command available for session data.
- Root or sudo access for launcher process management and complete DCV log visibility.
- `tar` and either `sha256sum` or `shasum`.

Required when the runtime is not already installed or supplied locally:

- `git` metadata with a GitHub `origin`, or an explicit `DCVOBS_RUNTIME_URL`.
- `curl` or `wget`.
- Network access to the approved runtime artifact.

Current dependency installation also requires access to the configured Python package index on first start and whenever `requirements.txt` changes. An offline wheelhouse is not included. System Python 3.9+ is not a prerequisite.

## Installation

```bash
git clone <repository-url>
cd dcv-observability

sudo ./dcvobs preflight
sudo ./dcvobs start
```

The first `start` bootstraps the runtime and application environment. Do not create or activate a venv manually.

## What First Start Does

1. Detects the OS, version, architecture, and hostname.
2. Detects the NICE DCV command/version and inspects `/var/log/dcv`.
3. Checks the installed private runtime.
4. Checks the local runtime artifact and download cache.
5. Downloads the approved runtime if required.
6. Verifies SHA256 and safely extracts the runtime.
7. Validates private Python, OpenSSL, architecture, SSL, venv, and pip.
8. Creates `.venv/` from the private runtime when needed.
9. Installs pinned requirements when their SHA256 fingerprint has changed.
10. Selects the first available port from 8900 through 8999.
11. Launches Uvicorn on `0.0.0.0`.
12. Polls `GET /api/status` until HTTP health succeeds or the startup timeout expires.

The default launcher health timeout is 120 seconds. Set a positive integer to override it:

```bash
DCVOBS_STARTUP_TIMEOUT=180 sudo --preserve-env=DCVOBS_STARTUP_TIMEOUT ./dcvobs start
```

## First Start Example

Representative output is abbreviated below; versions and paths are factual examples, while the hostname is fictional.

```text
DCV Observability Preflight
OS: CentOS Linux 7
Architecture: x86_64
Hostname: dcv-host.example.com
DCV command: /usr/bin/dcv
DCV version: NICE DCV 2023.1 (r17701)

Private runtime:
  Artifact: dcvobs-python-3.11-linux-x86_64-glibc217.tar.gz
  Installed runtime: not present
  Local artifact: not present
  Download source: GitHub Release v0.1.0
  Download required: yes

Downloading runtime artifact...
Runtime download completed.
SHA256 VERIFIED
Installing private runtime...
Private runtime HEALTHY
Selected port: 8900
DCV Observability started.
Port: 8900
HTTP: HEALTHY
```

## Operations

```bash
sudo ./dcvobs preflight  # Inspect host, DCV, runtime, and bootstrap readiness
sudo ./dcvobs start      # Bootstrap as needed and start the service
sudo ./dcvobs status     # Show process, HTTP, port, Python, and OpenSSL status
sudo ./dcvobs logs       # Follow the application log
sudo ./dcvobs restart    # Stop and start safely
sudo ./dcvobs stop       # Stop without deleting .runtime/ or .venv/
./dcvobs --help          # Show launcher usage
```

`status` distinguishes `STOPPED`, `STARTING`, and `RUNNING`. HTTP is reported as `WAITING`, `HEALTHY`, `UNHEALTHY`, or unavailable as appropriate.

## Port Selection

The preferred port is 8900. If it is unavailable, the launcher checks 8901, 8902, and subsequent ports through 8999 without stopping or changing the existing listener. The selected port is stored in:

```text
/var/run/dcv-observability/application.port
```

The application binds to `0.0.0.0`, so it listens on every configured network interface. The launcher does not add authentication, TLS, reverse-proxy configuration, or firewall rules. Restrict access with approved network controls before exposing it beyond a trusted administrative network.

## Startup and Collection Behavior

HTTP availability and DCV data readiness are separate states. FastAPI starts the initial DCV collection as one process-local background task. While it is running, `GET /api/status` remains available with `collection_state` set to `collecting`, and the dashboard displays:

```text
Collecting initial DCV data...
```

Once collection succeeds, the state becomes `ready` and dashboard data is populated. A failed collection changes the state to `error` without stopping Uvicorn; manual Refresh can retry. A process-local async lock prevents startup and manual refresh collectors from running concurrently. Large or numerous DCV logs may make the initial log-derived analysis take several minutes.

## Dashboard

The summary shows Running Sessions, Host Memory, 1-minute Host Load, and Recent Session Issues. The issue count is based on available bounded evidence from the past 24 hours; it is shown as unavailable when the required per-session log evidence is incomplete.

The server section shows DCV version, CPU count, UDP/QUIC 8443 listener status, optional GPU identity, and a conservative host status derived from memory and load thresholds. Its session table contains Session, Owner, Type, State, Age, Session CPU, and Session Memory. Column headings sort the table. Refresh starts a new collection; if another collection is active, a second collector is not started.

Session CPU and memory are sums for matching `Xdcv` and `dcvagent` processes, not total resource consumption for the user or all session-related processes.

## Session Evidence

Selecting a session opens its detail page. Evidence rows can include:

- Exact, contextually bracketed, approximate, or unavailable time placement.
- Severity and category.
- A normalized message for readability.
- The untouched raw source line.
- Source filename and line number.
- Nearby source context for relevant warning/error and lock evidence.

Normalization classifies recognized text patterns; it is not a diagnosis. Every normalized row remains backed by the raw line and source identity.

The implemented routes are:

```text
GET  /
GET  /api/status
POST /api/refresh
GET  /sessions/{session_id}
GET  /api/sessions/{session_id}
```

## Date and Time Filtering

The session page initially shows all evidence available within the collector's bounded read behavior; no time range is required. Optional From and To fields use `YYYY-MM-DD HH:MM:SS` in 24-hour UTC. Quick choices provide the last 15 minutes, hour, or 24 hours. From must not be after To.

With a range active, only events whose exact or contextual time placement overlaps the requested interval are returned. Important evidence with no time placement is excluded unless **Include unplaced evidence** is selected.

## Timestamp Handling

DCV log timestamps are interpreted as UTC. Browser inputs are sent with an explicit UTC suffix; timezone-less API values are also interpreted as UTC. The response reports the requested range, interpreted UTC range, detected server timezone, and available parsed coverage.

Timestamped lines retain their parsed timestamp. The server timezone is diagnostic context and does not rewrite the configured UTC log interpretation.

## Untimestamped Evidence

Some DCV messages have no timestamp. The collector considers timestamped lines in the same source file within ten source lines:

- A nearby timestamp before and after creates a bracketed interval.
- One nearby timestamp creates approximate placement.
- No nearby timestamp leaves the evidence unplaced.

This is contextual placement, not a timestamp present in the original line. The UI labels inferred/approximate values and retains the raw line, source filename, line number, and context where available.

## Log Search

Search is a case-insensitive literal text search, similar in purpose to a simple `grep` filter. It scans session-scoped source text, composes with an active date/time range, and returns matching evidence with context. It does not support regular expressions, change normalization, or modify raw evidence; it only reduces the returned/displayed evidence to matches.

## Log Sources

The collector discovers owner- and session-scoped files directly under `/var/log/dcv` matching these implemented families, including numbered rotated files:

```text
/var/log/dcv/agent.<owner>.<session>.log
/var/log/dcv/Xdcv.<owner>.<session>.log
/var/log/dcv/dcv-xsession.<owner>.<session>.log
```

It accepts exact owner segments and implemented session-name variants using the base name, `_session`, and `-session`. Actual names depend on DCV user and session naming. Direct reads are attempted first; permission-denied reads use bounded `sudo -n` commands without changing log permissions.

## Permissions

`start`, `stop`, and `restart` require root. Root operation supports complete DCV session visibility, access to protected DCV logs, creation of protected state/log directories, and process management. `preflight`, `status`, and `logs` are documented with sudo so their view matches operational permissions.

DCV Observability does not modify NICE DCV configuration, restart DCV, change permissions under `/var/log/dcv`, install OS packages, or alter firewall rules. Running as root is operationally privileged and does not itself make network exposure safe.

## Repository / Runtime Layout

```text
app/                    FastAPI application, host collector, log evidence
tests/                  Python and launcher tests
runtime/runtime.conf    Runtime identity, versions, and release coordinates
runtime/dist/*.sha256   Tracked authoritative runtime checksum metadata
dcvobs                  Deployment and operations launcher
requirements.txt        Pinned application dependencies
requirements-test.txt   Pinned application and test-only dependencies
config.example.json     Generic legacy configuration example
```

Generated locally and ignored by Git:

```text
.runtime/               Installed private runtime and download cache
.venv/                  Application virtual environment
```

`runtime/dist/*.tar.gz`, `config.json`, `.env`, Python cache files, and logs must remain untracked. `runtime/dist/*.sha256` remains tracked.

## Configuration

The current application always collects from the local DCV server, detects its hostname dynamically, and does not load `config.json`. `config.example.json` is retained as a generic reference for an earlier remote-collector shape; it is not required for installation and its SSH fields do not affect the current local collector.

Runtime artifact metadata is configured in `runtime/runtime.conf`. The release URL is normally derived from a GitHub clone's `origin`. To use another approved HTTPS artifact endpoint without editing the manifest:

```bash
DCVOBS_RUNTIME_URL=https://artifacts.example.com/path/runtime.tar.gz \
  sudo --preserve-env=DCVOBS_RUNTIME_URL ./dcvobs start
```

## Application Logs

The launcher/application log is:

```text
/var/log/dcv-observability/application.log
```

View it with `sudo ./dcvobs logs`. Each new launch appends a timestamped startup separator without truncating earlier output. This file is separate from NICE DCV logs under `/var/log/dcv`.

## Preflight

`sudo ./dcvobs preflight` reports:

- OS, version, validation state, architecture, and hostname.
- DCV command, DCV version, DCV log directory, and discovered log count.
- Runtime artifact, installed/local state, download source/requirement, and SHA256/runtime health.
- Selected private Python and diagnostic host Python candidates.
- Preferred or persisted application port.

Rejected host Python candidates are expected and harmless when the private runtime is healthy or available for installation. For example, a host Python 3.6 may be rejected as too old and another host Python may be rejected because `_ssl` is unavailable while `Selected Python` remains the private runtime. Preflight inspection does not itself download or install the runtime.

## Security Model

- The application has read-only observability intent toward NICE DCV.
- System Python, system packages, system OpenSSL, loader configuration, DCV configuration, and DCV log permissions are not modified.
- Conda is not required.
- No credentials are embedded in the repository.
- Optional GitHub authentication is supplied through an environment variable; the token is not logged and is removed from the private Python/Uvicorn environment.
- Runtime bytes are executed only after SHA256 verification.
- Archive paths and symbolic-link targets are checked before installation.
- Root may be required to read DCV logs, but the web service has no built-in authentication or TLS and binds to all interfaces.

This release has not been represented as security-audited, penetration-tested, or certified.

## Runtime and Release Versioning

Application source version, Git tag/release, and private runtime version are separate:

- Application stage: DCV Observability v0.1.
- Git release/tag: identifies a reviewed source snapshot.
- Private runtime: Python 3.11.16, OpenSSL 3.5.7, Linux x86_64, glibc 2.17 baseline.

The manifest identifies the expected runtime release and artifact. The repository owner is obtained from the clone's GitHub `origin`, so documentation and bootstrap do not depend on a personal repository URL. Every published runtime artifact must have matching committed SHA256 metadata.

## Updating

```bash
sudo ./dcvobs stop
git pull
sudo ./dcvobs preflight
sudo ./dcvobs start
```

`.runtime/` and `.venv/` are generated local state. Do not commit them or delete the private runtime during an ordinary application update. Dependency installation reruns only when the `requirements.txt` fingerprint changes.

## Troubleshooting

### Host Python rejected

This is expected when the private runtime is selected. Do not replace or modify system Python for DCV Observability.

### Python `_ssl` unavailable

The private Python/OpenSSL runtime avoids reliance on host Python SSL. Confirm that preflight reports the private runtime as healthy or available for installation; do not repair this by modifying system Python or OpenSSL.

### Runtime download returns 404 or 403

Verify that the release and named artifact exist for the clone's GitHub repository. A private repository may require `DCVOBS_GITHUB_TOKEN` and `sudo --preserve-env=DCVOBS_GITHUB_TOKEN`. Public assets should not need a token. Do not disable TLS verification.

### SHA256 mismatch

Do not bypass verification. Remove the cached/local tarball, obtain it again from the approved release, and confirm that release metadata matches the committed `.sha256` file.

### Port 8900 already in use

The launcher reports the collision and selects the next available port through 8999. Use `sudo ./dcvobs status` for the selected URL.

### Collecting initial DCV data

The HTTP service is running while the first collection proceeds in the background. Large log sets can require several minutes. Check `collection_state` through `/api/status`; this message alone is not an error.

### Permission denied reading DCV logs

Operate the launcher with sudo and confirm the administrator is authorized to read the relevant files. Do not broadly change `/var/log/dcv` permissions.

### DCV version unavailable

Confirm the detected executable and test the supported command forms:

```bash
/usr/bin/dcv version
/usr/bin/dcv --version
```

Version parsing failure is non-fatal when the executable exists.

### Application does not start

```bash
sudo ./dcvobs status
sudo ./dcvobs logs
sudo ./dcvobs preflight
```

Inspect `/var/log/dcv-observability/application.log`. The launcher waits up to 120 seconds by default for `/api/status`, reports a process exit immediately, and shows the last 40 log lines on startup failure.

## Clean Installation Validation

A fresh clone should initially contain no `.runtime/`, no `.venv/`, and no runtime tarball:

```bash
git clone <repository-url>
cd dcv-observability

test ! -e .runtime
test ! -e .venv
test ! -e runtime/dist/dcvobs-python-3.11-linux-x86_64-glibc217.tar.gz

sudo ./dcvobs preflight
sudo ./dcvobs start
sudo ./dcvobs status
```

Expected first-run events are runtime download, SHA256 verification, safe extraction, private Python validation, venv creation, requirements installation, port selection, Uvicorn startup, HTTP health success, and background DCV collection. Open the URL reported by `start` or `status` and confirm that `/api/status` changes from `collection_state: collecting` to `ready`.

## Development and Tests

After the launcher has created `.runtime/`, create a separate ignored test environment from the private runtime and run the Python tests without activation:

```bash
env LD_LIBRARY_PATH="$PWD/.runtime/python/openssl/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
  "$PWD/.runtime/python/bin/python3.11" -m venv "$PWD/.test-venv"
env LD_LIBRARY_PATH="$PWD/.runtime/python/openssl/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
  "$PWD/.test-venv/bin/python" -m pip install -r requirements-test.txt
env LD_LIBRARY_PATH="$PWD/.runtime/python/openssl/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
  "$PWD/.test-venv/bin/python" -m unittest discover -s tests -p 'test_*.py'
```

Run launcher tests with Bash:

```bash
bash -n dcvobs tests/test_dcvobs.sh
bash tests/test_dcvobs.sh
```

The unit tests mock NICE DCV and log access where appropriate; an installed DCV server is not required for those cases. Conda is not required.

## Known Limitations

- The distributed private runtime is Linux x86_64 only.
- CentOS 7 is the only currently validated OS baseline.
- Rocky Linux 8, Rocky Linux 9, and Amazon Linux validation is pending.
- Initial log analysis may take several minutes with large log sets.
- Evidence is limited to records present in available, readable NICE DCV logs.
- Absence of matching evidence does not prove that an issue did not occur.
- Normalized evidence is for readability and remains dependent on raw evidence.
- Untimestamped lines may require contextual placement that is not an original source timestamp.
- The application does not actively measure latency, FPS, bandwidth, or client network quality.
- There is no external persistent database.
- First dependency installation currently requires Python package index access.
- The HTTP service has no built-in authentication or TLS and binds to `0.0.0.0`.

## Version / Status

DCV Observability v0.1 is an initial operational and validation release.

## Ownership

Maintained by Tuple.
