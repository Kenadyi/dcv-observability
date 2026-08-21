from __future__ import annotations

import os
import re
import stat
import subprocess
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Any


DCV_LOG_DIR = Path("/var/log/dcv")
TAIL_LINES = 2_000
TAIL_BYTES = 4 * 1024 * 1024
MAX_RETURNED_EVENTS = 2_000
CONTEXT_LINES = 10
NEAR_LINE_DISTANCE = 10
CORRELATION_SECONDS = 15
DCV_LOG_TIMEZONE = timezone.utc

SESSION_FILE_RE = re.compile(
    r"^(?P<prefix>agent|dcv-xsession|Xdcv)\."
    r"(?P<owner>[^.]+)\.(?P<session>[^.]+)\.log(?:\.\d+)?$"
)
SEVERITY_RE = re.compile(
    r"(?<![A-Z])(?P<severity>ERROR|ERR|WARNING|WARN|INFO|DEBUG)(?![A-Z])",
    re.IGNORECASE,
)
TIMESTAMP_PATTERNS = (
    re.compile(r"(?P<timestamp>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"),
    re.compile(r"(?P<timestamp>\d{4}/\d{2}/\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)"),
    re.compile(r"(?P<timestamp>\d{2}/\d{2}/\d{4}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)"),
)
COMPONENT_RE = re.compile(r"(?:^|\s)\[(?P<component>[^\]]+)\](?=\s|$)")

FRAME_ACK_RE = re.compile(
    r"Flow controller is full.*?Elapsed time since last frame ack:\s*(?P<usec>\d+)\s*usec",
    re.IGNORECASE,
)
PEER_DISCONNECT_RE = re.compile(
    r"(?:connection (?:was )?closed by (?:the )?peer|peer closed (?:the )?(?:client )?connection)",
    re.IGNORECASE,
)
DISPLAY_CLOSED_RE = re.compile(
    r"display(?: channel)?\s+(?:was )?(?:closed|disconnected)", re.IGNORECASE
)
SESSION_CLOSED_RE = re.compile(
    r"(?:last client connection(?:\s+['\"][^'\"]+['\"])?\s+"
    r"(?:(?:has|was) been |was )?closed|session (?:was )?closed)",
    re.IGNORECASE,
)
UNKNOWN_LOCK_COMMAND_RE = re.compile(
    r"Unknown command\s+['\"]lock['\"]", re.IGNORECASE
)
LOCK_REQUEST_RE = re.compile(
    r"(?:server requested (?:an? )?OS session lock|"
    r"request(?:ed|ing)?.{0,35}(?:OS )?session lock|"
    r"OS session lock request forwarded|"
    r"lock(?:ing)? (?:the )?OS session)",
    re.IGNORECASE,
)
LOCK_FAILURE_RE = re.compile(
    r"(?:(?:OS )?session lock(?: request)?.{0,40}(?:failed|failure)|"
    r"failed to lock (?:the )?(?:OS )?session|"
    r"Unknown command\s+['\"]lock['\"])",
    re.IGNORECASE,
)
SCREENLOCK_RE = re.compile(r"screen lock application", re.IGNORECASE)
AGENT_RESTART_RE = re.compile(
    r"(?:dcvagent|agent).{0,50}(?:restart(?:ed|ing)?|start(?:ed|ing))", re.IGNORECASE
)
AGENT_EXIT_RE = re.compile(
    r"(?:dcvagent|agent).{0,80}(?:(?:exited|exit|terminated).{0,25}"
    r"(?:unexpected|abnormal|code\s*[=:]?\s*(?!0\b)\d+)|"
    r"unexpected(?:ly)? (?:exit|termination))",
    re.IGNORECASE,
)


def _run(cmd: list[str], timeout: int = 12) -> tuple[int, str, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return (
            result.returncode,
            result.stdout.decode("utf-8", errors="replace"),
            result.stderr.decode("utf-8", errors="replace"),
        )
    except Exception as exc:
        return 1, "", str(exc)


def _safe_filename(value: str) -> str | None:
    value = value.strip()
    if not value or value in {".", ".."} or Path(value).name != value:
        return None
    return value


def _list_dcv_filenames() -> tuple[list[str], list[str]]:
    try:
        names = [
            entry.name
            for entry in os.scandir(DCV_LOG_DIR)
            if entry.is_file(follow_symlinks=False)
        ]
        return sorted(names), []
    except OSError as exc:
        direct_error = str(exc)

    rc, out, err = _run([
        "sudo", "-n", "/usr/bin/find", str(DCV_LOG_DIR),
        "-maxdepth", "1", "-type", "f", "-printf", "%f\n",
    ])
    if rc != 0:
        return [], [err.strip() or direct_error or "unable to list DCV logs"]
    names = []
    for line in out.splitlines():
        name = _safe_filename(line)
        if name is not None:
            names.append(name)
    return sorted(set(names)), []


def _session_variants(owner: str, session_id: str) -> set[str]:
    variants = {owner, session_id}
    for value in (owner, session_id):
        base = value.removesuffix("_session")
        variants.add(base)
        variants.add(f"{base}_session")
        variants.add(f"{base}-session")
    return variants


def discover_session_files(owner: str, session_id: str) -> tuple[list[str], list[str]]:
    """Match supported per-session files, requiring an exact owner segment."""
    filenames, errors = _list_dcv_filenames()
    variants = _session_variants(owner, session_id)
    matches = []
    for filename in filenames:
        match = SESSION_FILE_RE.fullmatch(filename)
        if (
            match
            and match.group("owner") == owner
            and match.group("session") in variants
        ):
            matches.append(filename)
    return matches, errors


def _read_denied(error: str) -> bool:
    return bool(re.search(
        r"permission denied|operation not permitted|password is required|not allowed",
        error,
        re.IGNORECASE,
    ))


def _consume_log_command(
    command: list[str],
    consumer: Any,
) -> tuple[Any, bool, str]:
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        return None, False, str(exc)
    if process.stdout is None:
        if process.stderr is not None:
            process.stderr.close()
        return None, False, "log reader did not provide stdout"
    try:
        payload, stopped_early = consumer(process.stdout)
    except Exception as exc:
        process.stdout.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        if process.stderr is not None:
            process.stderr.close()
        return None, False, str(exc)
    process.stdout.close()
    try:
        return_code = process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if process.stderr is not None:
            process.stderr.close()
        return payload, False, "log reader did not exit after its stream was closed"
    stderr = process.stderr.read().strip() if process.stderr is not None else ""
    if process.stderr is not None:
        process.stderr.close()
    if return_code == 0 or stopped_early:
        return payload, True, ""
    return payload, False, stderr or f"log reader exited with status {return_code}"


def _read_validated_log(
    filename: str,
    command_prefix: list[str],
    consumer: Any,
) -> tuple[Any, dict[str, Any]]:
    """Read one owner/session-discovered DCV log with direct/sudo fallback."""
    safe_name = _safe_filename(filename)
    if safe_name is None or SESSION_FILE_RE.fullmatch(safe_name) is None:
        return None, {
            "filename": filename,
            "read_method": "direct",
            "status": "failed",
            "error": "unsafe or unsupported log filename rejected",
        }
    path = DCV_LOG_DIR / safe_name
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return None, {
                "filename": safe_name,
                "read_method": "direct",
                "status": "failed",
                "error": "validated log target is not a regular file",
            }
    except PermissionError:
        # Sudo-based discovery already established the file type; protected
        # directory metadata may not be visible to the application user.
        pass
    except OSError as exc:
        return None, {
            "filename": safe_name,
            "read_method": "direct",
            "status": "failed",
            "error": str(exc),
        }
    direct_command = [*command_prefix, str(path)]
    payload, success, direct_error = _consume_log_command(
        direct_command, consumer
    )
    if success:
        return payload, {
            "filename": safe_name,
            "read_method": "direct",
            "status": "read",
            "error": None,
        }
    if not _read_denied(direct_error):
        return None, {
            "filename": safe_name,
            "read_method": "direct",
            "status": "failed",
            "error": direct_error,
        }

    payload, success, sudo_error = _consume_log_command(
        ["sudo", "-n", *direct_command], consumer
    )
    if success:
        return payload, {
            "filename": safe_name,
            "read_method": "sudo",
            "status": "read",
            "error": None,
        }
    return None, {
        "filename": safe_name,
        "read_method": "sudo",
        "status": "denied" if _read_denied(sudo_error) else "failed",
        "error": sudo_error or direct_error,
    }


def _line_count(filename: str) -> int | None:
    def consume(stream: Any) -> tuple[str, bool]:
        return stream.read(), False

    out, access = _read_validated_log(
        filename, ["/usr/bin/wc", "-l", "--"], consume
    )
    if access["status"] != "read" or out is None:
        return None
    match = re.match(r"\s*(\d+)", out)
    return int(match.group(1)) if match else None


def _tail_file(
    filename: str,
) -> tuple[list[str], int, dict[str, Any], str | None]:
    def consume(stream: Any) -> tuple[str, bool]:
        return stream.read(), False

    out, access = _read_validated_log(
        filename,
        ["/usr/bin/tail", "-c", str(TAIL_BYTES), "--"],
        consume,
    )
    if access["status"] != "read" or out is None:
        return [], 1, access, access["error"] or f"unable to read {filename}"
    lines = out.splitlines()[-TAIL_LINES:]
    newline_count = _line_count(filename)
    if newline_count is None:
        first_line = 1
    else:
        total_lines = newline_count + (1 if out and not out.endswith(("\n", "\r")) else 0)
        first_line = max(1, total_lines - len(lines) + 1)
    return lines, first_line, access, None


def _timestamp_epoch_from_line(line: str) -> tuple[str | None, float | None]:
    for pattern in TIMESTAMP_PATTERNS:
        match = pattern.search(line)
        if match:
            return _canonical_time(match.group("timestamp"))
    return None, None


def _scan_range_stream(
    stream: Any,
    from_epoch: float | None,
    to_epoch: float | None,
) -> tuple[list[str], int, dict[str, Any]]:
    """Stream a chronological log and retain only the requested range context."""
    before = deque(maxlen=CONTEXT_LINES + 1)
    untimestamped_tail = deque(maxlen=TAIL_LINES)
    captured: list[tuple[int, str]] = []
    started = from_epoch is None
    timestamp_seen = False
    past_to_timestamps = 0
    lines_scanned = 0
    matching_timestamped = 0
    stopped_early = False
    earliest: tuple[str, float] | None = None
    latest: tuple[str, float] | None = None

    for line_number, raw_line in enumerate(stream, start=1):
        lines_scanned = line_number
        line = raw_line.rstrip("\r\n")
        untimestamped_tail.append((line_number, line))
        canonical, epoch = _timestamp_epoch_from_line(line)
        if epoch is not None:
            timestamp_seen = True
            if earliest is None or epoch < earliest[1]:
                earliest = (canonical, epoch)
            if latest is None or epoch > latest[1]:
                latest = (canonical, epoch)

            before_window = from_epoch is not None and epoch < from_epoch
            after_window = to_epoch is not None and epoch > to_epoch
            if not before_window and not after_window:
                matching_timestamped += 1

            if not started and not before_window:
                captured.extend(before)
                started = True

            if started and after_window:
                past_to_timestamps += 1
            elif started:
                past_to_timestamps = 0

        if started:
            captured.append((line_number, line))
            # Two chronological timestamps beyond To establish the boundary and
            # preserve intervening untimestamped context for bracket placement.
            if past_to_timestamps >= 2:
                stopped_early = True
                break
        else:
            before.append((line_number, line))

    if not timestamp_seen:
        captured = list(untimestamped_tail)
    if not captured:
        return [], 1, {
            "lines_scanned": lines_scanned,
            "matching_timestamped_lines": matching_timestamped,
            "earliest": earliest,
            "latest": latest,
            "stopped_early": stopped_early,
        }
    return [line for _, line in captured], captured[0][0], {
        "lines_scanned": lines_scanned,
        "matching_timestamped_lines": matching_timestamped,
        "earliest": earliest,
        "latest": latest,
        "stopped_early": stopped_early,
    }


def _scan_time_range_file(
    filename: str,
    from_epoch: float | None,
    to_epoch: float | None,
) -> tuple[list[str], int, dict[str, Any], dict[str, Any], str | None]:
    empty_stats = {
        "lines_scanned": 0,
        "matching_timestamped_lines": 0,
        "earliest": None,
        "latest": None,
        "stopped_early": False,
    }

    def consume(stream: Any) -> tuple[tuple[list[str], int, dict[str, Any]], bool]:
        result = _scan_range_stream(stream, from_epoch, to_epoch)
        return result, result[2]["stopped_early"]

    payload, access = _read_validated_log(
        filename, ["/usr/bin/tail", "-n", "+1", "--"], consume
    )
    if access["status"] != "read" or payload is None:
        return (
            [],
            1,
            empty_stats,
            access,
            access["error"] or f"unable to read {filename}",
        )
    lines, first_line, stats = payload
    return lines, first_line, stats, access, None


def _scan_search_stream(
    stream: Any,
    query: str,
    from_epoch: float | None,
    to_epoch: float | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Stream a log and retain bounded source context around literal matches."""
    query_folded = query.casefold()
    before: deque[tuple[int, str]] = deque(maxlen=CONTEXT_LINES)
    active: list[dict[str, Any]] = []
    completed: deque[dict[str, Any]] = deque(maxlen=MAX_RETURNED_EVENTS)
    lines_scanned = 0
    matching_lines = 0
    past_to_timestamps = 0
    stopped_early = False

    for line_number, raw_line in enumerate(stream, start=1):
        lines_scanned = line_number
        line = raw_line.rstrip("\r\n")

        for window in active:
            window["lines"].append((line_number, line))
            window["remaining"] -= 1
        finished = [window for window in active if window["remaining"] <= 0]
        for window in finished:
            completed.append(window)
            active.remove(window)

        if query_folded in line.casefold():
            matching_lines += 1
            active.append({
                "target_line": line_number,
                "lines": [*before, (line_number, line)],
                "remaining": CONTEXT_LINES,
            })

        before.append((line_number, line))
        _, epoch = _timestamp_epoch_from_line(line)
        if epoch is not None and to_epoch is not None and epoch > to_epoch:
            past_to_timestamps += 1
        elif epoch is not None:
            past_to_timestamps = 0

        # Preserve after-context for active matches before ending a chronological
        # range scan. Closing stdout stops both direct and sudo readers safely.
        if past_to_timestamps >= 2 and not active:
            stopped_early = True
            break

    completed.extend(active)
    return list(completed), {
        "lines_scanned": lines_scanned,
        "matching_lines": matching_lines,
        "stopped_early": stopped_early,
    }


def _scan_search_file(
    filename: str,
    query: str,
    from_epoch: float | None,
    to_epoch: float | None,
    fallback_base: float,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], str | None]:
    empty_stats = {
        "lines_scanned": 0,
        "matching_lines": 0,
        "stopped_early": False,
    }

    def consume(stream: Any) -> tuple[tuple[list[dict[str, Any]], dict[str, Any]], bool]:
        result = _scan_search_stream(stream, query, from_epoch, to_epoch)
        return result, result[1]["stopped_early"]

    payload, access = _read_validated_log(
        filename, ["/usr/bin/tail", "-n", "+1", "--"], consume
    )
    if access["status"] != "read" or payload is None:
        return (
            [],
            empty_stats,
            access,
            access["error"] or f"unable to read {filename}",
        )

    windows, stats = payload
    matched_events: list[dict[str, Any]] = []
    for window in windows:
        numbered_lines = window["lines"]
        if not numbered_lines:
            continue
        source_lines = [line for _, line in numbered_lines]
        first_line = numbered_lines[0][0]
        line_count = max(len(source_lines), 1)
        events = [
            parse_log_line(
                line,
                filename,
                fallback_base + (line_number / line_count),
                line_number,
            )
            for line_number, line in numbered_lines
        ]
        _add_context_and_time_placement(events, source_lines, first_line)
        target = next(
            (
                event for event in events
                if event["source_line_number"] == window["target_line"]
            ),
            None,
        )
        if target is not None:
            matched_events.append(target)
    return matched_events, stats, access, None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    candidate = value.replace(",", ".")
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        for pattern in ("%Y/%m/%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
            try:
                parsed = datetime.strptime(candidate.split(".", 1)[0], pattern)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=DCV_LOG_TIMEZONE)
    return parsed.astimezone(DCV_LOG_TIMEZONE)


def _canonical_time(value: str | None) -> tuple[str | None, float | None]:
    parsed = _parse_datetime(value)
    if parsed is None:
        return None, None
    display_time = parsed.astimezone(DCV_LOG_TIMEZONE).replace(tzinfo=None)
    return display_time.isoformat(timespec="milliseconds"), parsed.timestamp()


def _normalize_severity(value: str | None) -> str:
    if not value:
        return "UNKNOWN"
    value = value.upper()
    return {"ERR": "ERROR", "WARN": "WARNING"}.get(value, value)


def _clean_message(message: str, timestamp: str | None, severity: str) -> str:
    value = message.strip()
    if timestamp:
        value = value.replace(timestamp, "", 1).strip(" []:-")
    value = re.sub(r"^(?:\[[^\]]+\]\s*)+", "", value).strip()
    value = re.sub(
        rf"^(?:{re.escape(severity)}|WARN(?:ING)?|ERR(?:OR)?|INFO|DEBUG)\s*[:|-]?\s*",
        "", value, count=1, flags=re.IGNORECASE,
    )
    return value or message.strip()


def _classify(message: str, clean_message: str, severity: str) -> tuple[str, str, str]:
    frame_ack = FRAME_ACK_RE.search(message)
    if frame_ack:
        usec = int(frame_ack.group("usec"))
        return "DISPLAY_PERFORMANCE", "FRAME ACK STALL", f"DCV frame acknowledgement stalled for {usec:,} µs"
    if UNKNOWN_LOCK_COMMAND_RE.search(message):
        return "LOCKING", "ERROR", "Unknown command 'lock'"
    if PEER_DISCONNECT_RE.search(message):
        return "CONNECTION", "DISCONNECT", "Client connection closed by peer"
    if DISPLAY_CLOSED_RE.search(message):
        return "DISPLAY_PERFORMANCE", "DISPLAY", "Display channel disconnected"
    if SESSION_CLOSED_RE.search(message):
        return "SESSION_LIFECYCLE", "SESSION", "Last client connection closed"
    if LOCK_FAILURE_RE.search(message):
        exit_code = re.search(
            r"(?:exit(?:ed)?(?: with)? code|failed)\s*[=:]?\s*(\d+)",
            message, re.IGNORECASE,
        )
        suffix = f" — exit code {exit_code.group(1)}" if exit_code else ""
        return "LOCKING", "WARNING", f"OS session lock request failed{suffix}"
    if LOCK_REQUEST_RE.search(message):
        return "LOCKING", "LOCK", "OS session lock requested or forwarded"
    if SCREENLOCK_RE.search(message):
        return "LOCKING", "LOCK", clean_message
    if AGENT_EXIT_RE.search(message):
        return "AGENT", "AGENT EXIT", "DCV agent exited unexpectedly"
    if AGENT_RESTART_RE.search(message):
        return "AGENT", "AGENT", "DCV agent started or restarted"
    if re.search(r"\b(?:server )?ping\b.*\bclient\b", message, re.IGNORECASE):
        return "CONNECTION", "CONNECTION", clean_message
    if re.search(r"\bclient\b.*\bconnect(?:ed|ion)\b", message, re.IGNORECASE):
        return "CONNECTION", "CONNECTION", clean_message
    if severity in {"ERROR", "WARNING", "INFO", "DEBUG"}:
        return severity, severity, clean_message
    return "OTHER", "OTHER", clean_message


def parse_log_line(
    line: str,
    filename: str,
    fallback_order: float,
    source_line_number: int = 1,
) -> dict[str, Any]:
    timestamp_text = None
    for pattern in TIMESTAMP_PATTERNS:
        match = pattern.search(line)
        if match:
            timestamp_text = match.group("timestamp")
            break
    canonical_time, timestamp_epoch = _canonical_time(timestamp_text)
    severity_match = SEVERITY_RE.search(line)
    severity = _normalize_severity(
        severity_match.group("severity") if severity_match else None
    )
    component = None
    for match in COMPONENT_RE.finditer(line):
        candidate = match.group("component").strip()
        if (
            candidate
            and not SEVERITY_RE.fullmatch(candidate)
            and not any(pattern.fullmatch(candidate) for pattern in TIMESTAMP_PATTERNS)
            and not re.fullmatch(r"[\d:.-]+", candidate)
        ):
            component = candidate
            break
    if not component:
        component = filename.split(".", 1)[0] or "dcv"
    raw_message = line.strip()
    clean_message = _clean_message(raw_message, timestamp_text, severity)
    category, event_type, normalized_message = _classify(
        raw_message, clean_message, severity
    )
    return {
        "timestamp": canonical_time,
        "time_type": "exact" if timestamp_epoch is not None else "unknown",
        "time_from": canonical_time,
        "time_to": canonical_time,
        "approximate_time": None,
        "severity": severity,
        "component": component,
        "message": raw_message,
        "raw_log_line": raw_message,
        "source_filename": filename,
        "source_line_number": source_line_number,
        "context_before": [],
        "context_after": [],
        "category": category,
        "event_type": event_type,
        "normalized_message": normalized_message,
        "_timestamp_epoch": timestamp_epoch,
        "_time_from_epoch": timestamp_epoch,
        "_time_to_epoch": timestamp_epoch,
        "_sort": timestamp_epoch if timestamp_epoch is not None else fallback_order,
    }


def _add_context_and_time_placement(
    events: list[dict[str, Any]],
    source_lines: list[str],
    first_line: int,
) -> None:
    exact_positions = [
        index for index, event in enumerate(events)
        if event["_timestamp_epoch"] is not None
    ]
    for index, event in enumerate(events):
        if event["_timestamp_epoch"] is not None:
            continue
        before_index = max((pos for pos in exact_positions if pos < index), default=None)
        after_index = min((pos for pos in exact_positions if pos > index), default=None)
        before = events[before_index] if before_index is not None else None
        after = events[after_index] if after_index is not None else None
        before_distance = (
            event["source_line_number"] - before["source_line_number"]
            if before is not None else None
        )
        after_distance = (
            after["source_line_number"] - event["source_line_number"]
            if after is not None else None
        )
        before_near = before is not None and before_distance <= NEAR_LINE_DISTANCE
        after_near = after is not None and after_distance <= NEAR_LINE_DISTANCE
        if before_near and after_near:
            event.update({
                "time_type": "bracketed",
                "time_from": before["timestamp"],
                "time_to": after["timestamp"],
                "_time_from_epoch": before["_timestamp_epoch"],
                "_time_to_epoch": after["_timestamp_epoch"],
                "_sort": (before["_timestamp_epoch"] + after["_timestamp_epoch"]) / 2,
            })
        elif before_near or after_near:
            neighbor = before if before_near else after
            event.update({
                "time_type": "near",
                "time_from": neighbor["timestamp"] if before_near else None,
                "time_to": neighbor["timestamp"] if after_near else None,
                "approximate_time": neighbor["timestamp"],
                "_time_from_epoch": neighbor["_timestamp_epoch"],
                "_time_to_epoch": neighbor["_timestamp_epoch"],
                "_sort": neighbor["_timestamp_epoch"],
            })

        if event["severity"] in {"ERROR", "WARNING"} or UNKNOWN_LOCK_COMMAND_RE.search(event["message"]):
            line_offset = event["source_line_number"] - first_line
            before_start = max(0, line_offset - CONTEXT_LINES)
            after_end = min(len(source_lines), line_offset + CONTEXT_LINES + 1)
            event["context_before"] = [
                {"line_number": first_line + pos, "text": source_lines[pos]}
                for pos in range(before_start, line_offset)
            ]
            event["context_after"] = [
                {"line_number": first_line + pos, "text": source_lines[pos]}
                for pos in range(line_offset + 1, after_end)
            ]


def _public_event(event: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in event.items() if not key.startswith("_")}


def _event_overlaps_window(
    event: dict[str, Any],
    from_epoch: float | None,
    to_epoch: float | None,
) -> bool:
    if event["time_type"] == "unknown":
        return False
    event_from = event["_time_from_epoch"]
    event_to = event["_time_to_epoch"]
    if from_epoch is not None and event_to < from_epoch:
        return False
    if to_epoch is not None and event_from > to_epoch:
        return False
    return True


def _important_unplaced(event: dict[str, Any]) -> bool:
    return (
        event["time_type"] == "unknown"
        and (
            event["severity"] in {"ERROR", "WARNING"}
            or event["category"] in {"LOCKING", "AGENT"}
        )
    )


def _frame_ack_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    samples = []
    for event in events:
        match = FRAME_ACK_RE.search(event["message"])
        if match:
            samples.append((int(match.group("usec")), event))
    values = [sample[0] for sample in samples]
    last = samples[0][1] if samples else None
    return {
        "count": len(values),
        "max_usec": max(values) if values else None,
        "average_usec": round(fmean(values), 1) if values else None,
        "last_occurrence": (
            last["timestamp"] or last["approximate_time"] or last["time_to"]
            if last else None
        ),
    }


def _disconnect_correlations(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    placed = sorted(
        (event for event in events if event["time_type"] != "unknown"),
        key=lambda event: event["_sort"],
    )
    correlations = []
    for disconnect in placed:
        if not PEER_DISCONNECT_RE.search(disconnect["message"]):
            continue
        related = [
            event for event in placed
            if abs(event["_sort"] - disconnect["_sort"]) <= CORRELATION_SECONDS
            and (event is disconnect or event["category"] in {
                "CONNECTION", "DISPLAY_PERFORMANCE", "SESSION_LIFECYCLE", "LOCKING", "AGENT"
            })
        ]
        lock_events = [event for event in related if event["category"] == "LOCKING"]
        correlations.append({
            "disconnect_timestamp": disconnect["timestamp"] or disconnect["approximate_time"],
            "display_channel_closed": any(DISPLAY_CLOSED_RE.search(event["message"]) for event in related),
            "session_closed": any(SESSION_CLOSED_RE.search(event["message"]) for event in related),
            "agent_restarted": any(AGENT_RESTART_RE.search(event["message"]) for event in related),
            "lock_order": (
                "after" if any(event["_sort"] > disconnect["_sort"] for event in lock_events)
                else "before" if any(event["_sort"] < disconnect["_sort"] for event in lock_events)
                else "same_timestamp" if lock_events else "not_observed"
            ),
            "related_events": [_public_event(event) for event in related],
        })
    return correlations


def _lock_correlations(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    correlations = []
    for lock_error in events:
        if not UNKNOWN_LOCK_COMMAND_RE.search(lock_error["message"]):
            continue
        related = []
        for event in events:
            if event is lock_error or event["category"] != "LOCKING":
                continue
            same_file_nearby = (
                event["source_filename"] == lock_error["source_filename"]
                and abs(event["source_line_number"] - lock_error["source_line_number"]) <= CONTEXT_LINES
            )
            time_nearby = (
                event["time_type"] != "unknown"
                and lock_error["time_type"] != "unknown"
                and abs(event["_sort"] - lock_error["_sort"]) <= 2
            )
            if same_file_nearby or time_nearby:
                related.append(event)
        related.sort(key=lambda event: (event["_sort"], event["source_filename"], event["source_line_number"]))
        correlations.append({
            "lock_error": _public_event(lock_error),
            "related_events": [_public_event(event) for event in related],
            "basis": (
                "source_order" if any(
                    event["source_filename"] == lock_error["source_filename"]
                    for event in related
                ) else "time_context" if related else "none"
            ),
            "same_event_confirmed": False,
        })
    return correlations


def _analyze(events: list[dict[str, Any]]) -> dict[str, Any]:
    frame_metrics = _frame_ack_metrics(events)
    metrics = {
        "frame_ack_stalls": frame_metrics["count"],
        "client_disconnects": sum(bool(PEER_DISCONNECT_RE.search(event["message"])) for event in events),
        "os_lock_failures": sum(bool(LOCK_FAILURE_RE.search(event["message"])) for event in events),
        "unexpected_agent_exits": sum(bool(AGENT_EXIT_RE.search(event["message"])) for event in events),
    }
    disconnect_correlations = _disconnect_correlations(events)
    timeline = [_public_event(event) for event in sorted(events, key=lambda event: event["_sort"]) if event["category"] != "OTHER"]
    issue_count = sum(
        1 for event in events
        if event["severity"] in {"ERROR", "WARNING"}
        or event["event_type"] in {"FRAME ACK STALL", "DISCONNECT", "AGENT EXIT"}
        or LOCK_FAILURE_RE.search(event["message"])
    )
    return {
        "session_metrics": metrics,
        "frame_ack_analysis": frame_metrics,
        "evidence_timeline": timeline,
        "disconnect_correlations": disconnect_correlations,
        "lock_correlations": _lock_correlations(events),
        "issue_count": issue_count,
    }


def _connection_information(events: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    patterns = {
        "most_recent_client_ip": re.compile(r"\bclient\b.{0,80}?\b(?:ip|address|from)\b\s*[:=]?\s*(?P<value>(?:\d{1,3}\.){3}\d{1,3}|[0-9a-f:]{3,})", re.IGNORECASE),
        "client_type_or_user_agent": re.compile(r"\b(?:user[- ]agent|client(?: type)?)\b\s*[:=]\s*(?P<value>[^,;]+)", re.IGNORECASE),
        "protocol": re.compile(r"\b(?:dcv )?protocol\b\s*[:=]\s*(?P<value>[A-Za-z0-9_.+/-]+)", re.IGNORECASE),
        "datagram_capability": re.compile(r"\bdatagram(?: capability| support| enabled)?\b\s*[:=]\s*(?P<value>enabled|disabled|supported|unsupported|true|false|yes|no)", re.IGNORECASE),
        "connection_id": re.compile(r"\bconnection[-_ ]id\b\s*[:=]\s*(?P<value>[A-Za-z0-9_.:-]+)", re.IGNORECASE),
    }
    for key, pattern in patterns.items():
        for event in events:
            match = pattern.search(event["message"])
            if match:
                result[key] = match.group("value").strip().strip('"\'')
                break
    return result


def collect_session_logs(
    session_id: str,
    owner: str | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    include_unplaced: bool = False,
    search_query: str | None = None,
) -> dict[str, Any]:
    """Collect bounded, owner-scoped evidence for one validated DCV session."""
    for label, value in (("from_time", from_time), ("to_time", to_time)):
        if value is not None and value.utcoffset() is None:
            raise ValueError(f"{label} must be timezone-aware")
    owner = owner or session_id
    matches, errors = discover_session_files(owner, session_id)
    from_epoch = from_time.timestamp() if from_time is not None else None
    to_epoch = to_time.timestamp() if to_time is not None else None
    has_time_filter = from_epoch is not None or to_epoch is not None
    search_query = search_query.strip() if search_query else None
    read_mode = (
        "time_range_scan" if has_time_filter
        else "search_scan" if search_query
        else "tail"
    )
    all_events: list[dict[str, Any]] = []
    files_read: list[str] = []
    files_scanned: list[str] = []
    file_access: list[dict[str, Any]] = []
    timestamped_lines = 0
    untimestamped_lines = 0
    lines_scanned = 0
    matching_timestamped_lines = 0
    files_searched: list[str] = []

    for file_index, filename in enumerate(matches):
        try:
            mtime = (DCV_LOG_DIR / filename).stat().st_mtime
        except OSError:
            mtime = float(file_index)
        if search_query:
            file_events, scan_stats, access, error = _scan_search_file(
                filename,
                search_query,
                from_epoch,
                to_epoch,
                mtime,
            )
            lines_scanned += scan_stats["lines_scanned"]
            lines = []
            first_line = 1
        elif has_time_filter:
            lines, first_line, scan_stats, access, error = _scan_time_range_file(
                filename, from_epoch, to_epoch
            )
            lines_scanned += scan_stats["lines_scanned"]
            matching_timestamped_lines += scan_stats["matching_timestamped_lines"]
        else:
            lines, first_line, access, error = _tail_file(filename)
            scan_stats = None
        file_access.append(access)
        if error:
            errors.append(f"{filename}: {error}")
            continue
        files_read.append(filename)
        files_scanned.append(filename)
        if search_query:
            files_searched.append(filename)
        else:
            file_events = []
            line_count = max(len(lines), 1)
            for offset, line in enumerate(lines):
                if not line.strip():
                    continue
                event = parse_log_line(
                    line,
                    filename,
                    mtime + (offset / line_count),
                    first_line + offset,
                )
                file_events.append(event)
            _add_context_and_time_placement(file_events, lines, first_line)
        timestamped_lines += sum(
            event["time_type"] == "exact" for event in file_events
        )
        untimestamped_lines += sum(
            event["time_type"] != "exact" for event in file_events
        )
        all_events.extend(file_events)

    all_events.sort(key=lambda event: event["_sort"], reverse=True)
    exact_events = [
        event for event in all_events if event["time_type"] == "exact"
    ]
    earliest_event = min(
        exact_events, key=lambda event: event["_timestamp_epoch"], default=None
    )
    latest_event = max(
        exact_events, key=lambda event: event["_timestamp_epoch"], default=None
    )
    earliest_timestamp = earliest_event["timestamp"] if earliest_event else None
    latest_timestamp = latest_event["timestamp"] if latest_event else None
    unplaced = [event for event in all_events if _important_unplaced(event)]
    if has_time_filter:
        placed_in_window = [
            event for event in all_events
            if _event_overlaps_window(event, from_epoch, to_epoch)
        ]
        selected = list(placed_in_window)
    else:
        placed_in_window = list(all_events)
        selected = list(all_events)
    if has_time_filter and include_unplaced:
        selected.extend(event for event in unplaced if event not in selected)
        selected.sort(key=lambda event: event["_sort"], reverse=True)

    returned = (
        selected[:MAX_RETURNED_EVENTS]
        if has_time_filter or search_query
        else selected
    )

    counts = {name: 0 for name in ("ERROR", "WARNING", "INFO", "DEBUG", "UNKNOWN")}
    category_counts: dict[str, int] = {}
    for event in selected:
        counts[event["severity"]] = counts.get(event["severity"], 0) + 1
        category_counts[event["category"]] = category_counts.get(event["category"], 0) + 1

    diagnostics = {
        "owner": owner,
        "session_id": session_id,
        "accepted_session_variants": sorted(_session_variants(owner, session_id)),
        "files_discovered": matches,
        "files_read": files_read,
        "files_scanned": files_scanned,
        "file_access": file_access,
        "read_mode": read_mode,
        "search_query": search_query,
        "files_searched": files_searched,
        "matching_lines": len(selected) if search_query else None,
        "requested_from": (
            from_time.isoformat(timespec="milliseconds") if from_time else None
        ),
        "requested_to": (
            to_time.isoformat(timespec="milliseconds") if to_time else None
        ),
        "scan_lines_read": lines_scanned if has_time_filter or search_query else None,
        "lines_parsed": timestamped_lines + untimestamped_lines,
        "timestamped_lines": timestamped_lines,
        "untimestamped_lines": untimestamped_lines,
        "lines_inside_requested_window": len(placed_in_window),
        "timestamped_lines_inside_window": sum(
            event["time_type"] == "exact" for event in selected
        ),
        "matching_timestamped_lines": (
            matching_timestamped_lines if has_time_filter else timestamped_lines
        ),
        "returned_events": len(returned),
        "earliest_parsed_timestamp": earliest_timestamp,
        "latest_parsed_timestamp": latest_timestamp,
    }
    analysis = _analyze(selected)
    analysis["evidence_timeline"] = [
        _public_event(event)
        for event in sorted(returned, key=lambda event: event["_sort"])
        if search_query or event["category"] != "OTHER"
    ]
    return {
        "severity_counts": counts,
        "category_counts": category_counts,
        "events": [_public_event(event) for event in returned],
        "connection_information": _connection_information(selected),
        "source_files": matches,
        "log_errors": errors,
        "unplaced_evidence_count": len(unplaced),
        "unplaced_evidence": [_public_event(event) for event in unplaced],
        "include_unplaced": include_unplaced or not has_time_filter,
        "time_filter_active": has_time_filter,
        "search_query": search_query,
        "search_match_count": len(selected) if search_query else None,
        "diagnostics": diagnostics,
        "earliest_timestamp": earliest_timestamp,
        "latest_timestamp": latest_timestamp,
        **analysis,
    }
