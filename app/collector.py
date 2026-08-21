from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any


SESSION_RE = re.compile(
    r"Session:\s+'(?P<id>[^']+)'\s+\(owner:(?P<owner>\S+)\s+type:(?P<type>\S+)\)"
)


def run(cmd: list[str], timeout: int = 8) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def get_sessions() -> list[dict[str, Any]]:
    rc, out, err = run(["sudo", "-n", "/usr/bin/dcv", "list-sessions"])

    if rc != 0:
        return []

    sessions = []

    for line in out.splitlines():
        m = SESSION_RE.search(line)

        if m:
            sessions.append({
                "id": m.group("id"),
                "owner": m.group("owner"),
                "type": m.group("type"),
                "state": "running",
            })

    return sessions


def get_process_metrics() -> dict[str, dict[str, Any]]:
    rc, out, _ = run([
        "sudo", "-n",
        "ps",
        "ww",
        "-eo",
        "user=,pid=,etimes=,%cpu=,%mem=,args="
    ])

    result = {}

    if rc != 0:
        return result

    for line in out.splitlines():

        if "dcvagent" not in line and "Xdcv" not in line:
            continue

        parts = line.split(None, 5)

        if len(parts) < 6:
            continue

        user, pid, elapsed, cpu, mem, cmd = parts

        session = None

        m = re.search(r"--session-id\s+(\S+)", cmd)

        if not m:
            m = re.search(r"-sessionid\s+(\S+)", cmd)

        if m:
            session = m.group(1)

        if not session:
            continue

        item = result.setdefault(session, {
            "cpu_pct": 0.0,
            "mem_pct": 0.0,
            "age_seconds": 0,
        })

        try:
            item["cpu_pct"] += float(cpu)
            item["mem_pct"] += float(mem)
            item["age_seconds"] = max(
                item["age_seconds"],
                int(elapsed)
            )
        except ValueError:
            pass

    return result


def get_memory():
    data = {}

    try:
        with open("/proc/meminfo") as f:
            for line in f:

                if ":" not in line:
                    continue

                key, val = line.split(":", 1)

                m = re.search(r"\d+", val)

                if m:
                    data[key] = int(m.group()) * 1024

    except Exception:
        pass

    total = data.get("MemTotal", 0)
    available = data.get("MemAvailable", 0)

    if not total:
        return 0, 0, None

    used = total - available

    return total, used, round((used / total) * 100, 1)


def get_load():
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()

        return (
            float(parts[0]),
            float(parts[1]),
            float(parts[2]),
        )

    except Exception:
        return None, None, None


def get_cpu_count():
    try:
        return int(subprocess.check_output(
            ["getconf", "_NPROCESSORS_ONLN"],
            text=True
        ).strip())

    except Exception:
        return None


def get_hostname() -> str:
    rc, hostname, _ = run(["hostname"])
    return hostname if rc == 0 and hostname else "dcv-host"


def get_dcv_version():
    rc, out, _ = run(["/usr/bin/dcv", "version"])

    return out.splitlines()[0] if out else "unknown"


def get_quic():
    rc, out, _ = run(["ss", "-lntu"])

    if rc != 0:
        return False, False

    tcp = bool(re.search(r"\btcp\b.*:8443\b", out))
    udp = bool(re.search(r"\budp\b.*:8443\b", out))

    return tcp, udp


def get_gpu():
    rc, out, err = run([
        "nvidia-smi",
        "--query-gpu=name,utilization.gpu,memory.used,memory.total,utilization.encoder",
        "--format=csv,noheader,nounits",
    ])

    if rc != 0:
        return {
            "available": False,
            "error": err or out or "nvidia-smi unavailable"
        }

    gpus = []

    for line in out.splitlines():

        cols = [x.strip() for x in line.split(",")]

        if len(cols) < 5:
            continue

        try:
            gpus.append({
                "name": cols[0],
                "gpu_pct": float(cols[1]),
                "memory_used_mb": float(cols[2]),
                "memory_total_mb": float(cols[3]),
                "encoder_pct": float(cols[4]),
            })

        except ValueError:
            pass

    return {
        "available": True,
        "gpus": gpus,
    }


def collect_local() -> dict[str, Any]:

    started = time.time()
    detected_hostname = get_hostname()

    sessions = get_sessions()

    processes = get_process_metrics()

    for session in sessions:

        p = processes.get(session["id"], {})

        # These values are the summed Xdcv + dcvagent process usage for this
        # DCV session. They do not represent every process owned by the user.

        session["dcv_process_cpu_pct"] = round(
            p.get("cpu_pct", 0),
            2
        )

        session["dcv_process_mem_pct"] = round(
            p.get("mem_pct", 0),
            2
        )

        session["age_seconds"] = p.get(
            "age_seconds",
            0
        )

    mem_total, mem_used, mem_pct = get_memory()

    load1, load5, load15 = get_load()

    cpu_count = get_cpu_count()

    tcp, udp = get_quic()

    gpu = get_gpu()

    health = "healthy"
    reasons = []

    if mem_pct is not None:

        if mem_pct >= 90:
            health = "critical"
            reasons.append("memory >= 90%")

        elif mem_pct >= 80:
            health = "warning"
            reasons.append("memory >= 80%")

    load_per_cpu = None

    if load1 is not None and cpu_count:

        load_per_cpu = round(load1 / cpu_count, 2)

        if load_per_cpu >= 1.5:
            health = "critical"
            reasons.append("high load")

        elif load_per_cpu >= 1:
            if health != "critical":
                health = "warning"

            reasons.append("elevated load")

    return {
        "name": detected_hostname,
        "hostname": detected_hostname,
        "reachable": True,

        "dcv_version": get_dcv_version(),

        "session_count": len(sessions),

        "unique_users": len(
            set(s["owner"] for s in sessions)
        ),

        "multi_session_users": len([
            u for u in set(s["owner"] for s in sessions)
            if sum(
                1 for s in sessions
                if s["owner"] == u
            ) > 1
        ]),

        "sessions": sorted(
            sessions,
            key=lambda x: x["owner"].lower()
        ),

        "memory_total_bytes": mem_total,
        "memory_used_bytes": mem_used,
        "memory_used_pct": mem_pct,

        "cpu_count": cpu_count,

        "load_1m": load1,
        "load_5m": load5,
        "load_15m": load15,

        "load_per_cpu": load_per_cpu,

        "tcp_8443": tcp,
        "udp_8443": udp,

        "quic_listener_detected": udp,

        "gpu": gpu,

        "health": health,
        "health_reasons": reasons,

        "collector_ms": int(
            (time.time() - started) * 1000
        ),
    }
