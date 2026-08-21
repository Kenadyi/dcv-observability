#!/usr/bin/env bash

set -u

REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
export DCVOBS_TESTING=1
# shellcheck disable=SC1091
. "$REPO_DIR/dcvobs"

TEST_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/dcvobs-tests.XXXXXX")
trap 'rm -rf -- "$TEST_ROOT"' EXIT
PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); printf 'ok - %s\n' "$1"; }
fail() { FAIL=$((FAIL + 1)); printf 'not ok - %s\n' "$1"; }
assert_equal() {
    local name="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then pass "$name"; else fail "$name (expected '$expected', got '$actual')"; fi
}
assert_contains() {
    local name="$1" haystack="$2" needle="$3"
    case "$haystack" in *"$needle"*) pass "$name" ;; *) fail "$name (missing '$needle')" ;; esac
}
assert_not_contains() {
    local name="$1" haystack="$2" needle="$3"
    case "$haystack" in *"$needle"*) fail "$name (unexpected '$needle')" ;; *) pass "$name" ;; esac
}

default_startup_timeout=$(env -u DCVOBS_STARTUP_TIMEOUT DCVOBS_TESTING=1 bash -c '. "$1"; printf "%s" "$DCVOBS_STARTUP_TIMEOUT"' _ "$REPO_DIR/dcvobs")
assert_equal "startup timeout defaults to 120 seconds" "120" "$default_startup_timeout"

RUNTIME_DOWNLOAD_URL=""
RUNTIME_VERSION="vtest"
RUNTIME_ARTIFACT="runtime.tar.gz"
DCVOBS_RUNTIME_URL="https://artifacts.example.com/runtime.tar.gz"
resolve_runtime_download_url
assert_equal "runtime URL environment override is supported" "$DCVOBS_RUNTIME_URL" "$RUNTIME_DOWNLOAD_URL"
unset DCVOBS_RUNTIME_URL

runtime_origin_repo="$TEST_ROOT/runtime-origin-repo"
mkdir -p "$runtime_origin_repo"
git -C "$runtime_origin_repo" init -q
git -C "$runtime_origin_repo" remote add origin git@github.com:example-org/dcv-observability.git
saved_script_dir="$SCRIPT_DIR"
SCRIPT_DIR="$runtime_origin_repo"
RUNTIME_DOWNLOAD_URL=""
resolve_runtime_download_url
assert_equal \
    "runtime URL is derived from a generic GitHub origin" \
    "https://github.com/example-org/dcv-observability/releases/download/vtest/runtime.tar.gz" \
    "$RUNTIME_DOWNLOAD_URL"
SCRIPT_DIR="$saved_script_dir"

fake_dcv_version() {
    case "$DCV_VERSION_CASE:$1" in
        nice:version)
            printf '\n\nNICE DCV 2023.1 (r17701)\nCopyright (C) 2010-2023 NICE s.r.l.\n'
            ;;
        amazon:version)
            printf 'Version information follows\nAmazon DCV 2024.0 (r19000)\n'
            ;;
        generic:version)
            printf '\n  Generic DCV release 1.2  \nAdditional details\n'
            ;;
        fallback:version|none:version)
            printf '   \n\n'
            ;;
        fallback:--version)
            printf '\nVersion information follows\nAmazon DCV 2.0\n'
            ;;
        none:--version)
            printf '\n   \n'
            ;;
        *)
            return 1
            ;;
    esac
}

DCV_VERSION_CASE=nice
assert_equal "DCV version ignores leading blank lines" "NICE DCV 2023.1 (r17701)" "$(dcv_version fake_dcv_version)"
DCV_VERSION_CASE=amazon
assert_equal "Amazon DCV version line is preferred" "Amazon DCV 2024.0 (r19000)" "$(dcv_version fake_dcv_version)"
DCV_VERSION_CASE=generic
assert_equal "DCV version uses first generic non-empty line" "Generic DCV release 1.2" "$(dcv_version fake_dcv_version)"
DCV_VERSION_CASE=fallback
assert_equal "DCV version falls back to --version" "Amazon DCV 2.0" "$(dcv_version fake_dcv_version)"
DCV_VERSION_CASE=none
assert_equal "DCV version handles unusable output" "Unable to determine" "$(dcv_version fake_dcv_version)"

make_fake_python() {
    local path="$1" mode="$2"
    mkdir -p "$(dirname "$path")"
    sed "s/__MODE__/$mode/g" > "$path" <<'FAKE'
#!/usr/bin/env bash
mode="__MODE__"
if [ "${1:-}" = "-c" ]; then
    code="${2:-}"
    case "$code" in
        *'sys.version_info[:3]'*)
            [ "$mode" = "too_old" ] && echo '3.6.8' || echo '3.11.3'
            exit 0
            ;;
        *'sys.version_info >= (3, 9)'*)
            [ "$mode" = "too_old" ] && exit 1 || exit 0
            ;;
        *'import ssl'*)
            [ "$mode" = "missing_ssl" ] && exit 1 || exit 0
            ;;
        *'import venv'*)
            [ "$mode" = "missing_venv" ] && exit 1 || exit 0
            ;;
    esac
    exit 0
fi
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "venv" ]; then
    [ "$mode" = "venv_creation_fails" ] && exit 1
    target="$3"
    mkdir -p "$target/bin"
    printf '#!/usr/bin/env bash\nexit 0\n' > "$target/bin/python"
    printf '#!/usr/bin/env bash\nexit 0\n' > "$target/bin/pip"
    chmod +x "$target/bin/python" "$target/bin/pip"
    exit 0
fi
exit 0
FAKE
    chmod +x "$path"
}

fake_dir="$TEST_ROOT/fakes"
make_fake_python "$fake_dir/too-old" too_old
make_fake_python "$fake_dir/no-ssl" missing_ssl
make_fake_python "$fake_dir/no-venv" missing_venv
make_fake_python "$fake_dir/healthy" healthy

validate_python "$fake_dir/too-old" || true
assert_equal "Python too old is rejected" "Python < 3.9" "$PYTHON_REASON"

validate_python "$fake_dir/no-ssl" || true
assert_equal "Python missing SSL is rejected" "Python SSL support unavailable (_ssl)" "$PYTHON_REASON"

validate_python "$fake_dir/no-venv" || true
assert_equal "Python missing venv is rejected" "Python venv support unavailable" "$PYTHON_REASON"

if validate_python "$fake_dir/healthy"; then
    assert_equal "healthy Python is accepted" "HEALTHY" "$PYTHON_STATUS"
else
    fail "healthy Python is accepted"
fi

conda_python="$TEST_ROOT/miniconda3/bin/python3"
make_fake_python "$conda_python" healthy
validate_python "$conda_python" || true
assert_equal "Conda Python is ignored" "Conda Python paths are not allowed" "$PYTHON_REASON"
neutral_python="$TEST_ROOT/bin/python3"
mkdir -p "$(dirname "$neutral_python")"
ln -s "$conda_python" "$neutral_python"
validate_python "$neutral_python" || true
assert_equal "symlinked Conda Python is ignored" "Conda Python paths are not allowed" "$PYTHON_REASON"

make_runtime_fixture() {
    local base="$1" download_url="${2:-}" source_root artifact checksum
    mkdir -p "$base/dist" "$base/source/runtime-root/bin" "$base/source/runtime-root/openssl/lib64"
    source_root="$base/source/runtime-root"
    touch "$source_root/openssl/lib64/libssl.so.3" "$source_root/openssl/lib64/libcrypto.so.3"
    sed 's|__PYTHON_VERSION__|3.11.16|g; s|__OPENSSL_VERSION__|OpenSSL 3.5.7 test|g' > "$source_root/bin/python3.11" <<'PYTHON'
#!/usr/bin/env bash
case "${LD_LIBRARY_PATH:-}" in *openssl/lib64*) ;; *) exit 91 ;; esac
if [ "${1:-}" = "-c" ]; then
    case "${2:-}" in
        *'platform, ssl, sys, venv'*) printf '%s\n' '__PYTHON_VERSION__' '__OPENSSL_VERSION__' 'x86_64' ;;
        *'import ssl, sys'*) printf '%s\n' '__PYTHON_VERSION__' '__OPENSSL_VERSION__' ;;
    esac
    exit 0
fi
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "venv" ]; then
    mkdir -p "$3/bin"
    cp "$0" "$3/bin/python"
    chmod +x "$3/bin/python"
    exit 0
fi
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip" ]; then exit 0; fi
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "uvicorn" ]; then
    trap 'exit 0' TERM INT
    while :; do sleep 1; done
fi
exit 0
PYTHON
    chmod +x "$source_root/bin/python3.11"
    artifact="$base/dist/runtime.tar.gz"
    tar -czf "$artifact" -C "$base/source" runtime-root
    checksum=$(sha256_file "$artifact")
    printf '%s  runtime.tar.gz\n' "$checksum" > "$base/dist/runtime.tar.gz.sha256"
    printf '%s\n' \
        'runtime_id=test-python311' \
        'runtime_version=vtest' \
        'python_version=3.11.16' \
        'openssl_version=3.5.7' \
        'architecture=x86_64' \
        'glibc_baseline=2.17' \
        'artifact=runtime.tar.gz' \
        'sha256_file=runtime.tar.gz.sha256' \
        'archive_root=runtime-root' \
        "download_url=$download_url" > "$base/runtime.conf"
}

use_runtime_fixture() {
    local base="$1"
    RUNTIME_MANIFEST="$base/runtime.conf"
    RUNTIME_DIST_DIR="$base/dist"
    PRIVATE_RUNTIME_PARENT="$base/installed"
    PRIVATE_RUNTIME="$PRIVATE_RUNTIME_PARENT/python"
    PRIVATE_PYTHON="$PRIVATE_RUNTIME/bin/python3.11"
    PRIVATE_OPENSSL_LIB="$PRIVATE_RUNTIME/openssl/lib64"
}

host_architecture() { printf 'x86_64'; }
valid_fixture="$TEST_ROOT/private-valid"
make_runtime_fixture "$valid_fixture"
use_runtime_fixture "$valid_fixture"
ensure_private_runtime > "$valid_fixture/install-output" 2>&1
local_runtime_rc=$?
local_runtime_output=$(cat "$valid_fixture/install-output")
if [ "$local_runtime_rc" -eq 0 ]; then
    assert_equal "valid local runtime artifact is verified" "VERIFIED" "$PRIVATE_SHA_STATUS"
else
    fail "valid local runtime artifact is verified"
fi
assert_not_contains "local runtime artifact avoids download" "$local_runtime_output" "Downloading runtime artifact"
if [ -x "$PRIVATE_RUNTIME/bin/python3.11" ]; then pass "private runtime is extracted"; else fail "private runtime is extracted"; fi
assert_equal "private Python validation reports expected version" "3.11.16" "$PRIVATE_PYTHON_VERSION"

rm -f -- "$valid_fixture/dist/runtime.tar.gz"
reuse_output=$(ensure_private_runtime 2>&1)
if [ "$?" -eq 0 ]; then pass "installed private runtime is reused without artifact"; else fail "installed private runtime is reused without artifact"; fi
assert_not_contains "healthy installed runtime is not downloaded again" "$reuse_output" "Downloading runtime artifact"

original_ld_library_path=${LD_LIBRARY_PATH-}
LD_LIBRARY_PATH="existing-test-path"
if run_private_python -c 'pass' >/dev/null && [ "$LD_LIBRARY_PATH" = "existing-test-path" ]; then
    pass "private LD_LIBRARY_PATH is scoped to invocation"
else
    fail "private LD_LIBRARY_PATH is scoped to invocation"
fi
LD_LIBRARY_PATH="$original_ld_library_path"

missing_fixture="$TEST_ROOT/private-missing"
make_runtime_fixture "$missing_fixture"
rm -f -- "$missing_fixture/dist/runtime.tar.gz"
use_runtime_fixture "$missing_fixture"

# Explicitly simulate:
# - no local runtime artifact
# - no configured runtime URL
# - no Git remote from which a release URL can be derived
RUNTIME_DOWNLOAD_URL=""

_fake_git_dir="$TEST_ROOT/no-git-bin"
mkdir -p "$_fake_git_dir"

cat > "$_fake_git_dir/git" <<'EOF'
#!/bin/sh
exit 1
EOF

chmod +x "$_fake_git_dir/git"

_saved_path="$PATH"
PATH="$_fake_git_dir:$PATH"

missing_output=$(ensure_private_runtime 2>&1 || true)

PATH="$_saved_path"
unset _saved_path _fake_git_dir
assert_contains "missing private runtime artifact fails clearly" "$missing_output" "Private runtime artifact is missing and no download URL is configured."

bad_checksum_fixture="$TEST_ROOT/private-bad-checksum"
make_runtime_fixture "$bad_checksum_fixture"
printf 'corrupt\n' >> "$bad_checksum_fixture/dist/runtime.tar.gz"
use_runtime_fixture "$bad_checksum_fixture"
checksum_output=$(ensure_private_runtime 2>&1 || true)
assert_contains "private runtime checksum failure is rejected" "$checksum_output" "Private runtime checksum verification failed"

download_bin="$TEST_ROOT/download-bin"
mkdir -p "$download_bin"
cat > "$download_bin/curl" <<'CURL'
#!/usr/bin/env bash
output="" config=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --output) output="$2"; shift 2 ;;
        --config) config="$2"; shift 2 ;;
        *) shift ;;
    esac
done
auth=no
if [ -n "$config" ] && grep -q '^header = "Authorization: Bearer ' "$config"; then auth=yes; fi
printf 'curl auth=%s\n' "$auth" >> "$FAKE_DOWNLOAD_RECORD"
case "$FAKE_DOWNLOAD_MODE" in
    failure) printf 'partial' > "$output"; exit 22 ;;
    badsha) printf 'not-the-approved-runtime' > "$output" ;;
    success) cp "$FAKE_REMOTE_ARTIFACT" "$output" ;;
esac
CURL
cat > "$download_bin/wget" <<'WGET'
#!/usr/bin/env bash
output="" config=""
for argument in "$@"; do
    case "$argument" in
        --output-document=*) output=${argument#*=} ;;
        --config=*) config=${argument#*=} ;;
    esac
done
auth=no
if [ -n "$config" ] && grep -q '^header = Authorization: Bearer ' "$config"; then auth=yes; fi
printf 'wget auth=%s\n' "$auth" >> "$FAKE_DOWNLOAD_RECORD"
case "$FAKE_DOWNLOAD_MODE" in
    failure) printf 'partial' > "$output"; exit 8 ;;
    badsha) printf 'not-the-approved-runtime' > "$output" ;;
    success) cp "$FAKE_REMOTE_ARTIFACT" "$output" ;;
esac
WGET
chmod +x "$download_bin/curl" "$download_bin/wget"

TEST_CURL_PATH="$download_bin/curl"
TEST_WGET_PATH="$download_bin/wget"
find_curl() { printf '%s' "$TEST_CURL_PATH"; }
find_wget() { printf '%s' "$TEST_WGET_PATH"; }

download_fixture="$TEST_ROOT/private-download"
make_runtime_fixture "$download_fixture" "https://github.example/runtime.tar.gz"
cp "$download_fixture/dist/runtime.tar.gz" "$download_fixture/remote.tar.gz"
rm -f -- "$download_fixture/dist/runtime.tar.gz"
export FAKE_REMOTE_ARTIFACT="$download_fixture/remote.tar.gz"
export FAKE_DOWNLOAD_RECORD="$download_fixture/download-record"
export FAKE_DOWNLOAD_MODE=success
unset DCVOBS_GITHUB_TOKEN 2>/dev/null || true
use_runtime_fixture "$download_fixture"
download_output=$(ensure_private_runtime 2>&1)
if [ "$?" -eq 0 ] && [ -x "$PRIVATE_PYTHON" ]; then pass "missing local runtime triggers valid download and extraction"; else fail "missing local runtime triggers valid download and extraction"; fi
assert_contains "curl is preferred for runtime download" "$(cat "$FAKE_DOWNLOAD_RECORD")" "curl auth=no"
assert_contains "valid downloaded SHA permits extraction" "$download_output" "SHA256 VERIFIED"

failure_fixture="$TEST_ROOT/private-download-failure"
make_runtime_fixture "$failure_fixture" "https://github.example/runtime.tar.gz"
cp "$failure_fixture/dist/runtime.tar.gz" "$failure_fixture/remote.tar.gz"
rm -f -- "$failure_fixture/dist/runtime.tar.gz"
export FAKE_REMOTE_ARTIFACT="$failure_fixture/remote.tar.gz"
export FAKE_DOWNLOAD_RECORD="$failure_fixture/download-record"
export FAKE_DOWNLOAD_MODE=failure
use_runtime_fixture "$failure_fixture"
failure_output=$(ensure_private_runtime 2>&1 || true)
assert_contains "download HTTP failure is clear" "$failure_output" "Authentication may be required"
if ! find "$PRIVATE_RUNTIME_PARENT/cache" -name '*.part.*' -o -name 'runtime.tar.gz' 2>/dev/null | grep -q .; then pass "failed download removes partial artifact"; else fail "failed download removes partial artifact"; fi

bad_download_fixture="$TEST_ROOT/private-download-badsha"
make_runtime_fixture "$bad_download_fixture" "https://github.example/runtime.tar.gz"
cp "$bad_download_fixture/dist/runtime.tar.gz" "$bad_download_fixture/remote.tar.gz"
rm -f -- "$bad_download_fixture/dist/runtime.tar.gz"
export FAKE_REMOTE_ARTIFACT="$bad_download_fixture/remote.tar.gz"
export FAKE_DOWNLOAD_RECORD="$bad_download_fixture/download-record"
export FAKE_DOWNLOAD_MODE=badsha
use_runtime_fixture "$bad_download_fixture"
bad_download_output=$(ensure_private_runtime 2>&1 || true)
assert_contains "downloaded incorrect SHA is rejected" "$bad_download_output" "Private runtime checksum verification failed"
if [ ! -e "$PRIVATE_RUNTIME_PARENT/cache/runtime.tar.gz" ] && ! find "$PRIVATE_RUNTIME_PARENT/cache" -name '*.part.*' 2>/dev/null | grep -q .; then pass "bad downloaded artifact is deleted"; else fail "bad downloaded artifact is deleted"; fi

wget_fixture="$TEST_ROOT/private-download-wget"
make_runtime_fixture "$wget_fixture" "https://github.example/runtime.tar.gz"
cp "$wget_fixture/dist/runtime.tar.gz" "$wget_fixture/remote.tar.gz"
rm -f -- "$wget_fixture/dist/runtime.tar.gz"
export FAKE_REMOTE_ARTIFACT="$wget_fixture/remote.tar.gz"
export FAKE_DOWNLOAD_RECORD="$wget_fixture/download-record"
export FAKE_DOWNLOAD_MODE=success
TEST_CURL_PATH=""
use_runtime_fixture "$wget_fixture"
if ensure_private_runtime >/dev/null 2>&1; then pass "wget is used when curl is unavailable"; else fail "wget is used when curl is unavailable"; fi
assert_contains "wget fallback performed download" "$(cat "$FAKE_DOWNLOAD_RECORD")" "wget auth=no"

no_tool_fixture="$TEST_ROOT/private-download-no-tool"
make_runtime_fixture "$no_tool_fixture" "https://github.example/runtime.tar.gz"
rm -f -- "$no_tool_fixture/dist/runtime.tar.gz"
TEST_CURL_PATH="" TEST_WGET_PATH=""
use_runtime_fixture "$no_tool_fixture"
no_tool_output=$(ensure_private_runtime 2>&1 || true)
assert_contains "missing curl and wget fails clearly" "$no_tool_output" "Neither curl nor wget"

token_fixture="$TEST_ROOT/private-download-token"
make_runtime_fixture "$token_fixture" "https://github.example/runtime.tar.gz"
cp "$token_fixture/dist/runtime.tar.gz" "$token_fixture/remote.tar.gz"
rm -f -- "$token_fixture/dist/runtime.tar.gz"
export FAKE_REMOTE_ARTIFACT="$token_fixture/remote.tar.gz"
export FAKE_DOWNLOAD_RECORD="$token_fixture/download-record"
export FAKE_DOWNLOAD_MODE=success
export DCVOBS_GITHUB_TOKEN='github_pat_TEST_SECRET_123'
TEST_CURL_PATH="$download_bin/curl" TEST_WGET_PATH="$download_bin/wget"
use_runtime_fixture "$token_fixture"
token_output=$(ensure_private_runtime 2>&1)
assert_contains "GitHub token adds authorization header" "$(cat "$FAKE_DOWNLOAD_RECORD")" "curl auth=yes"
assert_not_contains "GitHub token is never printed" "$token_output" "$DCVOBS_GITHUB_TOKEN"
unset DCVOBS_GITHUB_TOKEN

find_curl() { command -v curl 2>/dev/null || true; }
find_wget() { command -v wget 2>/dev/null || true; }

unsafe_bin="$TEST_ROOT/unsafe-bin"
mkdir -p "$unsafe_bin"
printf '#!/usr/bin/env bash\nprintf "runtime-root/../../escape\\n"\n' > "$unsafe_bin/tar"
chmod +x "$unsafe_bin/tar"
saved_path="$PATH"
PATH="$unsafe_bin:$PATH"
RUNTIME_ARCHIVE_ROOT=runtime-root
if archive_paths_are_safe ignored >/dev/null 2>&1; then fail "unsafe tar traversal path is rejected"; else pass "unsafe tar traversal path is rejected"; fi
PATH="$saved_path"

use_runtime_fixture "$valid_fixture"
VENV_DIR="$valid_fixture/venv"
FINGERPRINT_FILE="$VENV_DIR/.dcvobs-requirements.sha256"
REQUIREMENTS_FILE="$valid_fixture/requirements.txt"
printf 'fastapi==0.116.1\n' > "$REQUIREMENTS_FILE"
if ensure_runtime >/dev/null && [ -x "$VENV_DIR/bin/python" ]; then pass "application venv is created by private Python"; else fail "application venv is created by private Python"; fi

discover_python_candidates() {
    DISCOVERED_PYTHONS=("$fake_dir/too-old" "$fake_dir/no-ssl" "$conda_python")
}
if select_python; then
    fail "unsupported candidates produce no selection"
else
    assert_equal "unsupported candidates produce no selection" "" "$SELECTED_PYTHON"
fi

port_is_available() { [ "$2" -eq 8900 ]; }
PORT_MIN=8900 PORT_MAX=8999
assert_equal "port 8900 available" "8900" "$(select_port ignored 2>/dev/null)"

port_is_available() { [ "$2" -eq 8901 ]; }
assert_equal "occupied 8900 selects 8901" "8901" "$(select_port ignored 2>/dev/null)"

port_is_available() { [ "$2" -eq 8903 ]; }
assert_equal "multiple occupied ports select next port" "8903" "$(select_port ignored 2>/dev/null)"

RUNTIME_DIR="$TEST_ROOT/run"
PID_FILE="$RUNTIME_DIR/application.pid"
PORT_FILE="$RUNTIME_DIR/application.port"
STARTED_FILE="$RUNTIME_DIR/application.started"
STATE_FILE="$RUNTIME_DIR/application.state"
mkdir -p "$RUNTIME_DIR"
printf '99999999\n' > "$PID_FILE"
running_pid >/dev/null 2>&1 || true
if [ ! -e "$PID_FILE" ]; then pass "stale PID state is removed"; else fail "stale PID state is removed"; fi

running_pid() { printf '4242\n'; }
printf '8900\n' > "$PORT_FILE"
duplicate_output=$(start_app 2>&1)
assert_contains "duplicate start is prevented" "$duplicate_output" "already running"
unset -f running_pid

fingerprint_dir="$TEST_ROOT/fingerprint"
mkdir -p "$fingerprint_dir/venv"
REQUIREMENTS_FILE="$fingerprint_dir/requirements.txt"
FINGERPRINT_FILE="$fingerprint_dir/venv/fingerprint"
printf 'fastapi==1\n' > "$REQUIREMENTS_FILE"
sha256_file "$REQUIREMENTS_FILE" > "$FINGERPRINT_FILE"
if requirements_changed; then fail "unchanged requirements fingerprint skips install"; else pass "unchanged requirements fingerprint skips install"; fi
printf 'uvicorn==1\n' >> "$REQUIREMENTS_FILE"
if requirements_changed; then pass "changed requirements fingerprint triggers install"; else fail "changed requirements fingerprint triggers install"; fi

DCV_LOG_DIR="$TEST_ROOT/does-not-exist"
PORT_FILE="$TEST_ROOT/no-port"
read_os_release() { OS_ID=test; OS_VERSION_ID=1; OS_NAME='Test OS'; OS_VERSION=1; }
print_python_candidates() { SELECTED_PYTHON=""; printf '  /usr/bin/python3\n    status: REJECTED\n    reason: Python < 3.9\n'; return 1; }
find_dcv_command() { return 0; }
preflight_output=$(preflight 2>&1 || true)
assert_contains "missing /var/log/dcv is reported" "$preflight_output" "DCV log directory: $DCV_LOG_DIR (NOT FOUND)"
assert_contains "missing dcv command is reported" "$preflight_output" "DCV command: NOT FOUND"
assert_contains "broken host Python does not fail healthy private runtime" "$preflight_output" "Preflight: PASS"
assert_contains "preflight selects Tuple private runtime" "$preflight_output" "Selected Python: Tuple private runtime"

preflight_download_fixture="$TEST_ROOT/preflight-download"
make_runtime_fixture "$preflight_download_fixture" "https://github.example/runtime.tar.gz"
rm -f -- "$preflight_download_fixture/dist/runtime.tar.gz"
use_runtime_fixture "$preflight_download_fixture"
preflight_download_output=$(preflight 2>&1 || true)
assert_contains "preflight reports missing installed runtime" "$preflight_download_output" "Installed runtime: not present"
assert_contains "preflight reports missing local artifact" "$preflight_download_output" "Local artifact: not present"
assert_contains "preflight reports release source" "$preflight_download_output" "Download source: GitHub Release vtest"
assert_contains "preflight reports download required" "$preflight_download_output" "Download required: yes"
if [ ! -d "$PRIVATE_RUNTIME_PARENT/cache" ]; then pass "preflight inspection does not download runtime"; else fail "preflight inspection does not download runtime"; fi
use_runtime_fixture "$valid_fixture"

startup_sleep() { :; }
startup_process_alive() {
    [ "$TEST_EXIT_AT" -lt 0 ] || [ "$STARTUP_ELAPSED" -lt "$TEST_EXIT_AT" ]
}
http_health() {
    [ "$TEST_HEALTH_AT" -ge 0 ] && [ "$STARTUP_ELAPSED" -ge "$TEST_HEALTH_AT" ]
}
assert_delayed_health() {
    local delay="$1" output_file="$TEST_ROOT/health-$1.out"
    TEST_HEALTH_AT="$delay"
    TEST_EXIT_AT=-1
    if wait_for_startup_health 1234 8900 120 > "$output_file"; then
        assert_equal "health available after ${delay} seconds" "$delay" "$STARTUP_ELAPSED"
    else
        fail "health available after ${delay} seconds"
    fi
}
assert_delayed_health 1
assert_delayed_health 20
assert_delayed_health 60
assert_delayed_health 110
assert_contains "startup polling reports 10-second progress" "$(cat "$TEST_ROOT/health-20.out")" "Waiting for application startup... 10s"

TEST_HEALTH_AT=-1
TEST_EXIT_AT=5
wait_for_startup_health 1234 8900 120 >/dev/null 2>&1 || true
assert_equal "startup stops waiting when process exits at 5 seconds" "EXITED:5" "$STARTUP_RESULT:$STARTUP_ELAPSED"

TEST_HEALTH_AT=-1
TEST_EXIT_AT=-1
wait_for_startup_health 1234 8900 7 >/dev/null 2>&1 || true
assert_equal "alive unhealthy process times out at configured value" "TIMEOUT:7" "$STARTUP_RESULT:$STARTUP_ELAPSED"

DCVOBS_STARTUP_TIMEOUT=9
if validate_startup_timeout; then
    wait_for_startup_health 1234 8900 "$DCVOBS_STARTUP_TIMEOUT" >/dev/null 2>&1 || true
    assert_equal "startup timeout override is honored" "TIMEOUT:9" "$STARTUP_RESULT:$STARTUP_ELAPSED"
else
    fail "startup timeout override is honored"
fi
DCVOBS_STARTUP_TIMEOUT=invalid
if validate_startup_timeout >/dev/null 2>&1; then fail "non-integer startup timeout is rejected"; else pass "non-integer startup timeout is rejected"; fi
DCVOBS_STARTUP_TIMEOUT=0
if validate_startup_timeout >/dev/null 2>&1; then fail "zero startup timeout is rejected"; else pass "zero startup timeout is rejected"; fi
DCVOBS_STARTUP_TIMEOUT=120

RUNTIME_DIR="$TEST_ROOT/service-run"
LOG_DIR="$TEST_ROOT/service-log"
APP_LOG="$LOG_DIR/application.log"
PID_FILE="$RUNTIME_DIR/application.pid"
PORT_FILE="$RUNTIME_DIR/application.port"
STARTED_FILE="$RUNTIME_DIR/application.started"
STATE_FILE="$RUNTIME_DIR/application.state"
mkdir -p "$RUNTIME_DIR" "$LOG_DIR"
running_pid() {
    local service_pid
    [ -r "$PID_FILE" ] || return 1
    service_pid=$(sed -n '1p' "$PID_FILE")
    kill -0 "$service_pid" 2>/dev/null || return 1
    printf '%s\n' "$service_pid"
}
preflight() { return 0; }
ensure_runtime() { return 0; }
select_port() { printf '8900\n'; }
http_health() { return 0; }
startup_process_alive() { kill -0 "$1" 2>/dev/null; }
startup_sleep() { sleep "$1"; }
start_output=$(start_app 2>&1)
assert_contains "start launches with private-runtime venv" "$start_output" "DCV Observability started"
assert_contains "start reports healthy HTTP and elapsed time" "$start_output" "HTTP: HEALTHY"
assert_contains "application log receives startup separator" "$(cat "$APP_LOG")" "DCV Observability startup"
status_output=$(status_app 2>&1)
assert_contains "status reports running service" "$status_output" "DCV Observability: RUNNING"
assert_contains "status reports private Python source" "$status_output" "Python source: Tuple private runtime"
printf 'STARTING\n' > "$STATE_FILE"
starting_status_output=$(status_app 2>&1)
assert_contains "status distinguishes STARTING" "$starting_status_output" "DCV Observability: STARTING"
assert_contains "STARTING status reports HTTP waiting" "$starting_status_output" "HTTP: WAITING"
if stop_app >/dev/null 2>&1; then pass "stop terminates private-runtime service"; else fail "stop terminates private-runtime service"; fi
if [ ! -e "$STATE_FILE" ]; then pass "stop clears STARTING state"; else fail "stop clears STARTING state"; fi
if [ -d "$PRIVATE_RUNTIME" ] && [ -d "$VENV_DIR" ]; then pass "stop preserves private runtime and venv"; else fail "stop preserves private runtime and venv"; fi

printf '1..%s\n' "$((PASS + FAIL))"
printf '%s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
