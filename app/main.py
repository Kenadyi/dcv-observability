from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from .collector import collect_local
from .dcv_logs import collect_session_logs


app = FastAPI(title="DCV Observability", version="0.1.0")
_cache: dict[str, Any] = {
    "state": "initializing",
    "collection_state": "initializing",
    "servers": [],
    "collected_at": None,
    "collection_started_at": None,
    "last_collection_duration_ms": None,
    "collection_error": None,
}
_collection_lock = asyncio.Lock()
_collection_started_monotonic: float | None = None
_startup_collection_task: asyncio.Task[dict[str, Any]] | None = None


def server_timezone_name() -> str:
    try:
        target = str(Path("/etc/localtime").resolve())
        marker = "/zoneinfo/"
        if marker in target:
            return target.split(marker, 1)[1]
    except OSError:
        pass
    return str(datetime.now().astimezone().tzinfo or "UTC")


def _iso_milliseconds(value: datetime | None) -> str | None:
    return value.isoformat(timespec="milliseconds") if value else None


def collect_recent_session_issues(server: dict[str, Any]) -> dict[str, Any]:
    """Calculate the overview's bounded 24-hour issue count from session logs."""
    since = datetime.now().astimezone() - timedelta(hours=24)
    total = 0
    complete = True
    for session in server.get("sessions", []):
        logs = collect_session_logs(
            session["id"], session["owner"], from_time=since
        )
        total += logs["issue_count"]
        if logs["log_errors"] or not logs["source_files"]:
            complete = False
    return {
        "count": total if complete else None,
        "scope": "Past 24h",
        "available": complete,
    }


def _status_payload(**extra: Any) -> dict[str, Any]:
    payload = dict(_cache)
    collection_state = payload.get("collection_state")
    if collection_state not in {"initializing", "collecting", "ready", "error"}:
        collection_state = "ready" if payload.get("servers") else "initializing"
    payload["state"] = collection_state
    payload["collection_state"] = collection_state
    if collection_state == "collecting" and _collection_started_monotonic is not None:
        payload["collection_elapsed_ms"] = max(
            0, round((time.monotonic() - _collection_started_monotonic) * 1000)
        )
    else:
        payload["collection_elapsed_ms"] = None
    payload.update(extra)
    return payload


def _safe_collection_error(exc: Exception) -> str:
    detail = " ".join(str(exc).split())
    if detail:
        return f"{type(exc).__name__}: {detail[:240]}"
    return type(exc).__name__


async def refresh_all() -> dict[str, Any]:
    global _collection_started_monotonic
    if _collection_lock.locked():
        return _status_payload(collection_request="already_in_progress")

    async with _collection_lock:
        started = time.monotonic()
        _collection_started_monotonic = started
        _cache.update({
            "state": "collecting",
            "collection_state": "collecting",
            "collection_started_at": datetime.now(timezone.utc).isoformat(),
            "collection_error": None,
        })
        try:
            result = await asyncio.to_thread(collect_local)
            result["recent_session_issues"] = await asyncio.to_thread(
                collect_recent_session_issues, result
            )
        except Exception as exc:
            _cache.update({
                "state": "error",
                "collection_state": "error",
                "collection_error": _safe_collection_error(exc),
                "last_collection_duration_ms": round(
                    (time.monotonic() - started) * 1000
                ),
            })
        else:
            _cache.update({
                "state": "ready",
                "collection_state": "ready",
                "servers": [result],
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "collection_error": None,
                "last_collection_duration_ms": round(
                    (time.monotonic() - started) * 1000
                ),
            })
        finally:
            _collection_started_monotonic = None
        return _status_payload()


@app.on_event("startup")
async def startup():
    global _startup_collection_task
    _cache.update({
        "state": "initializing",
        "collection_state": "initializing",
        "collection_error": None,
    })
    _startup_collection_task = asyncio.create_task(refresh_all())


@app.get("/api/status")
async def status():
    return _status_payload()


@app.post("/api/refresh")
async def refresh():
    return await refresh_all()


def find_session(session_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    for server in _cache.get("servers", []):
        for session in server.get("sessions", []):
            if session.get("id") == session_id:
                return server, session
    raise HTTPException(status_code=404, detail="DCV session not found")


@app.get("/api/sessions/{session_id}")
async def session_detail_api(
    session_id: str,
    from_time: datetime | None = Query(None, alias="from"),
    to_time: datetime | None = Query(None, alias="to"),
    include_unplaced: bool = Query(False),
    q: str | None = Query(None, max_length=512),
):
    server, session = find_session(session_id)
    # API callers may provide DCV log wall time without an offset. Interpret it
    # explicitly as UTC; the browser UI already sends an explicit trailing Z.
    if from_time is not None and from_time.utcoffset() is None:
        from_time = from_time.replace(tzinfo=timezone.utc)
    if to_time is not None and to_time.utcoffset() is None:
        to_time = to_time.replace(tzinfo=timezone.utc)
    if from_time and to_time and from_time.timestamp() > to_time.timestamp():
        raise HTTPException(status_code=422, detail="'from' must not be after 'to'")
    log_from = from_time.astimezone(timezone.utc) if from_time else None
    log_to = to_time.astimezone(timezone.utc) if to_time else None
    timezone_name = server_timezone_name()
    try:
        server_zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        server_zone = datetime.now().astimezone().tzinfo or timezone.utc
    logs = await asyncio.to_thread(
        collect_session_logs,
        session_id,
        session["owner"],
        log_from,
        log_to,
        include_unplaced,
        q,
    )
    requested_from = _iso_milliseconds(from_time)
    requested_to = _iso_milliseconds(to_time)
    interpreted_log_from = _iso_milliseconds(log_from)
    interpreted_log_to = _iso_milliseconds(log_to)
    interpreted_server_from = _iso_milliseconds(
        from_time.astimezone(server_zone) if from_time else None
    )
    interpreted_server_to = _iso_milliseconds(
        to_time.astimezone(server_zone) if to_time else None
    )
    diagnostics = dict(logs["diagnostics"])
    diagnostics.update({
        "requested_from": requested_from,
        "requested_to": requested_to,
        "interpreted_log_from": interpreted_log_from,
        "interpreted_log_to": interpreted_log_to,
        "interpreted_server_from": interpreted_server_from,
        "interpreted_server_to": interpreted_server_to,
        "log_timezone": "UTC",
        "server_timezone": timezone_name,
    })
    metadata = dict(session)
    metadata["hostname"] = server.get("hostname") or server.get("name")
    return {
        "session": metadata,
        "severity_counts": logs["severity_counts"],
        "category_counts": logs["category_counts"],
        "recent_events": logs["events"],
        "evidence_timeline": logs["evidence_timeline"],
        "session_metrics": logs["session_metrics"],
        "frame_ack_analysis": logs["frame_ack_analysis"],
        "disconnect_correlations": logs["disconnect_correlations"],
        "lock_correlations": logs["lock_correlations"],
        "connection_information": logs["connection_information"],
        "source_files": logs["source_files"],
        "log_errors": logs["log_errors"],
        "unplaced_evidence_count": logs["unplaced_evidence_count"],
        "unplaced_evidence": logs["unplaced_evidence"],
        "include_unplaced": logs["include_unplaced"],
        "time_filter_active": logs["time_filter_active"],
        "search_query": logs["search_query"],
        "search_match_count": logs["search_match_count"],
        "diagnostics": diagnostics,
        "earliest_timestamp": logs["earliest_timestamp"],
        "latest_timestamp": logs["latest_timestamp"],
        "excluded_untimestamped_count": (
            0 if logs["include_unplaced"] else logs["unplaced_evidence_count"]
        ),
        "evidence_window": {
            "from": _iso_milliseconds(log_from),
            "to": _iso_milliseconds(log_to),
        },
        "time_interpretation": {
            "requested_from": requested_from,
            "requested_to": requested_to,
            "interpreted_log_from": interpreted_log_from,
            "interpreted_log_to": interpreted_log_to,
            "log_timezone": "UTC",
            "interpreted_server_from": interpreted_server_from,
            "interpreted_server_to": interpreted_server_to,
            "server_timezone": timezone_name,
        },
    }


@app.get("/sessions/{session_id}", response_class=HTMLResponse)
async def session_detail_page(session_id: str):
    find_session(session_id)
    return HTMLResponse(
        SESSION_DETAIL.replace("__SESSION_ID__", json.dumps(session_id))
    )


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(DASHBOARD.replace("__STATUS_POLL_SECONDS__", "15"))


DASHBOARD = r"""
<!doctype html>
<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>DCV Observability</title>
<style>
:root{font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#1f2937;background:#f5f7fb}*{box-sizing:border-box}body{margin:0}header{background:#111827;color:white;padding:22px 28px;display:flex;align-items:center;justify-content:space-between;gap:20px}h1{font-size:22px;margin:0}.subtitle{color:#cbd5e1;margin-top:5px;font-size:13px}.refresh-area{display:flex;align-items:center;gap:12px}.refresh-time{font-size:12px;color:#cbd5e1;text-align:right}.button{border:0;background:white;color:#111827;padding:9px 13px;border-radius:8px;cursor:pointer;font-weight:700}.button:disabled{opacity:.6;cursor:not-allowed}main{padding:24px;max-width:1400px;margin:auto}.notice{background:#fffbeb;border:1px solid #fde68a;color:#78350f;border-radius:10px;padding:12px 14px;font-size:13px;margin-bottom:18px}.summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-bottom:18px}.card{background:white;border:1px solid #e5e7eb;border-radius:12px;padding:16px;box-shadow:0 1px 2px rgba(0,0,0,.03)}.label{color:#6b7280;font-size:11px;text-transform:uppercase;letter-spacing:.05em}.value{font-size:26px;font-weight:760;margin-top:5px}.scope{font-size:11px;color:#6b7280;margin-top:4px}.server{margin-bottom:18px}.server-top{display:flex;align-items:center;justify-content:space-between;gap:15px}.server-name{font-size:20px;font-weight:760}.identity{display:flex;gap:8px;flex-wrap:wrap;margin-top:9px}.identity span{font-size:12px;color:#4b5563;background:#f3f4f6;padding:5px 8px;border-radius:7px}.status{font-size:12px;font-weight:750;text-transform:uppercase;border-radius:999px;padding:5px 9px}.healthy{background:#dcfce7;color:#166534}.warning{background:#fef3c7;color:#92400e}.critical{background:#fee2e2;color:#991b1b}.unreachable{background:#e5e7eb;color:#374151}table{width:100%;border-collapse:collapse;margin-top:16px;font-size:13px}th,td{padding:11px 9px;border-top:1px solid #e5e7eb;text-align:left}th{color:#6b7280;font-size:11px;text-transform:uppercase;letter-spacing:.04em}.sort-button{border:0;background:transparent;color:inherit;font:inherit;text-transform:inherit;letter-spacing:inherit;padding:0;cursor:pointer}.sort-arrow{display:inline-block;min-width:10px}a{color:#2563eb;text-decoration:none}.error{margin-top:12px;background:#fef2f2;color:#991b1b;padding:10px;border-radius:8px;font-size:13px}.failure{color:#fca5a5}.small{font-size:12px;color:#6b7280}@media(max-width:900px){.summary{grid-template-columns:repeat(2,1fr)}header{align-items:flex-start}.refresh-area{align-items:flex-end;flex-direction:column}}@media(max-width:560px){.summary{grid-template-columns:1fr}main{padding:14px}header{padding:18px}.refresh-time{max-width:180px}table{display:block;overflow-x:auto}}
</style></head><body>
<header><div><h1>DCV Observability</h1><div class='subtitle'>Read-only v0.1 · Local session troubleshooting</div></div><div class='refresh-area'><div id='refreshStatus' class='refresh-time'>Last refreshed: —</div><button id='refreshButton' class='button' type='button'>Refresh</button></div></header>
<main><div class='notice'>Reports running DCV sessions and bounded log evidence. It does not claim active client state, latency, FPS, or bandwidth.</div>
<div class='summary'><div class='card'><div class='label'>Running Sessions</div><div id='sumSessions' class='value'>—</div></div><div class='card'><div class='label'>Host Memory</div><div id='sumMemory' class='value'>—</div></div><div class='card'><div class='label'>Host Load</div><div id='sumLoad' class='value'>—</div><div id='sumLoadScope' class='scope'>1 minute</div></div><div class='card'><div class='label'>Recent Session Issues</div><div id='sumIssues' class='value'>—</div><div class='scope'>Past 24h · bounded log evidence</div></div></div>
<div id='server'></div></main>
<script>
const STATUS_POLL_SECONDS=__STATUS_POLL_SECONDS__;
let refreshInFlight=false,lastStatusData=null,sessionSortKey='id',sessionSortDirection='asc';
const byId=id=>document.getElementById(id);
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]))}
function age(s){if(s===null||s===undefined)return'—';const d=Math.floor(s/86400),h=Math.floor((s%86400)/3600),m=Math.floor((s%3600)/60);return d?`${d}d ${h}h`:h?`${h}h ${m}m`:`${m}m`}
function localTime(value){if(!value)return'—';const date=new Date(value);return Number.isNaN(date.getTime())?'—':date.toLocaleString()}
function sessionValue(item,key){if(key==='age_seconds'||key==='dcv_process_cpu_pct'||key==='dcv_process_mem_pct')return Number(item[key]??0);return String(item[key]??'').toLocaleLowerCase()}
function sortedSessions(items){return [...items].sort((a,b)=>{const av=sessionValue(a,sessionSortKey),bv=sessionValue(b,sessionSortKey),result=typeof av==='number'?av-bv:av.localeCompare(bv);return sessionSortDirection==='asc'?result:-result})}
function sessionArrow(key){return sessionSortKey===key?(sessionSortDirection==='asc'?'↑':'↓'):''}
function sortSessions(key){if(sessionSortKey===key)sessionSortDirection=sessionSortDirection==='asc'?'desc':'asc';else{sessionSortKey=key;sessionSortDirection='asc'}render(lastStatusData)}
function sortHeader(label,key){return `<button class='sort-button' onclick="sortSessions('${key}')">${label} <span class='sort-arrow'>${sessionArrow(key)}</span></button>`}
function render(data){lastStatusData=data;const collectionState=data.collection_state||data.state,x=(data.servers||[])[0];byId('refreshStatus').classList.remove('failure');if(collectionState==='initializing'||collectionState==='collecting'){byId('refreshStatus').textContent='Collecting initial DCV data...';byId('sumSessions').textContent='—';byId('sumMemory').textContent='—';byId('sumLoad').textContent='—';byId('sumIssues').textContent='—';byId('server').innerHTML=`<div class='notice'>Collecting initial DCV data...</div>`;return}byId('refreshStatus').textContent=`Last refreshed: ${localTime(data.collected_at)}`;if(collectionState==='error'&&!x){byId('refreshStatus').textContent='DCV data collection failed';byId('refreshStatus').classList.add('failure');byId('server').innerHTML=`<div class='error'>DCV data collection failed. ${esc(data.collection_error||'Refresh to retry.')}</div>`;return}if(!x){byId('server').innerHTML=`<div class='error'>No local server data returned.</div>`;return}const gpu=x.gpu&&x.gpu.available&&x.gpu.gpus&&x.gpu.gpus.length?x.gpu.gpus[0]:null,issues=x.recent_session_issues||{},state=!x.reachable?'unreachable':(x.health||'healthy');byId('sumSessions').textContent=x.session_count??'—';byId('sumMemory').textContent=x.memory_used_pct===null||x.memory_used_pct===undefined?'—':`${x.memory_used_pct}%`;byId('sumLoad').textContent=x.load_1m??'—';byId('sumLoadScope').textContent=x.load_per_cpu===null||x.load_per_cpu===undefined?'1 minute':`1 minute · ${x.load_per_cpu} / CPU`;byId('sumIssues').textContent=issues.available?issues.count:'Unavailable';const rows=sortedSessions(x.sessions||[]).map(q=>`<tr><td><a href='/sessions/${encodeURIComponent(q.id)}'><b>${esc(q.id)}</b></a></td><td>${esc(q.owner)}</td><td>${esc(q.type)}</td><td>${esc(q.state)}</td><td>${age(q.age_seconds)}</td><td>${q.dcv_process_cpu_pct??0}%</td><td>${q.dcv_process_mem_pct??0}%</td></tr>`).join('');const errors=(x.errors||[]).map(e=>`<div class='error'>${esc(e)}</div>`).join('');byId('server').innerHTML=`<section class='card server'><div class='server-top'><div><div class='server-name'>${esc(x.name||x.hostname||'Local DCV server')}</div><div class='identity'><span>${esc(x.dcv_version||'DCV version unavailable')}</span><span>${esc(x.cpu_count??'—')} CPU</span><span>QUIC / UDP 8443 ${x.quic_listener_detected?'enabled':'not detected'}</span><span>GPU ${gpu?esc(gpu.name):'n/a'}</span></div></div><div class='status ${state}'>${esc(state)}</div></div>${errors}<table><thead><tr><th>${sortHeader('Session','id')}</th><th>${sortHeader('Owner','owner')}</th><th>${sortHeader('Type','type')}</th><th>${sortHeader('State','state')}</th><th>${sortHeader('Age','age_seconds')}</th><th>${sortHeader('Session CPU','dcv_process_cpu_pct')}</th><th>${sortHeader('Session Memory','dcv_process_mem_pct')}</th></tr></thead><tbody>${rows||`<tr><td colspan='7'>No sessions returned.</td></tr>`}</tbody></table><div class='small' style='margin-top:10px'>Session CPU and memory are summed Xdcv + dcvagent process usage, not all processes owned by the user.</div></section>`}
async function loadStatus(){const response=await fetch('/api/status',{cache:'no-store'});if(!response.ok)throw new Error(`Status request failed (${response.status})`);render(await response.json())}
async function refreshNow(){if(refreshInFlight)return;refreshInFlight=true;const button=byId('refreshButton');button.disabled=true;button.textContent='Refreshing...';byId('refreshStatus').classList.remove('failure');try{const response=await fetch('/api/refresh',{method:'POST'});if(!response.ok)throw new Error(`Refresh failed (${response.status})`);await loadStatus()}catch(error){byId('refreshStatus').textContent=`Refresh failed: ${error.message}`;byId('refreshStatus').classList.add('failure')}finally{button.disabled=false;button.textContent='Refresh';refreshInFlight=false}}
byId('refreshButton').addEventListener('click',refreshNow);
loadStatus().catch(error=>{byId('refreshStatus').textContent=`Status failed: ${error.message}`;byId('refreshStatus').classList.add('failure')});
setInterval(()=>{if(!refreshInFlight)loadStatus().catch(()=>{})},Math.max(STATUS_POLL_SECONDS,10)*1000);
</script></body></html>
"""


SESSION_DETAIL = r"""
<!doctype html>
<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>DCV Session Evidence</title>
<style>
:root{font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#1f2937;background:#f5f7fb}*{box-sizing:border-box}body{margin:0}.session-header{position:sticky;top:0;z-index:20;background:#111827;color:#fff;padding:11px 20px;display:grid;grid-template-columns:minmax(280px,1fr) auto auto;align-items:center;gap:18px;box-shadow:0 2px 8px rgba(0,0,0,.15)}h1{font-size:18px;margin:0}.identity-line{color:#cbd5e1;font-size:12px;margin-top:4px;display:flex;gap:12px;flex-wrap:wrap}.header-metrics{display:flex;gap:15px;font-size:11px;white-space:nowrap}.header-metrics b{display:block;font-size:14px;margin-top:2px}.header-actions{text-align:right;font-size:11px;color:#cbd5e1}.header-actions a{color:#cbd5e1;margin-right:9px;text-decoration:none}.button{border:1px solid #d1d5db;background:#fff;color:#374151;padding:6px 9px;border-radius:7px;cursor:pointer;font-weight:700;font-size:11px}.button.primary{background:#111827;color:#fff;border-color:#111827}.button:disabled{opacity:.6;cursor:not-allowed}main{padding:12px 18px;max-width:1500px;margin:auto}.panel{background:#fff;border:1px solid #e5e7eb;border-radius:9px;padding:10px 12px;margin-bottom:10px}.period-actions{display:flex;gap:5px;flex-wrap:wrap}.coverage,.timezone-debug{font-size:10px;color:#6b7280;margin-top:3px}.counts{display:grid;grid-template-columns:repeat(6,1fr);border:1px solid #e5e7eb;border-radius:8px;background:#fff;margin-bottom:10px}.count{padding:7px 10px;border-right:1px solid #e5e7eb}.count:last-child{border-right:0}.count-label{font-size:9px;color:#6b7280;text-transform:uppercase;letter-spacing:.03em}.count-value{font-size:17px;font-weight:750;margin-top:1px}.evidence-panel{background:#fff;border:1px solid #e5e7eb;border-radius:9px;padding:10px 12px}.evidence-heading{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:7px}.evidence-heading h2{font-size:15px;margin:0}.filters{display:flex;gap:4px;flex-wrap:wrap}.filter{border:1px solid #d1d5db;background:#fff;color:#4b5563;padding:4px 7px;border-radius:999px;cursor:pointer;font-size:10px;font-weight:700}.filter.active{border-color:#2563eb;background:#eff6ff;color:#1d4ed8}.table-wrap{overflow:auto;max-height:calc(100vh - 285px)}table{width:100%;border-collapse:collapse;font-size:11px}th,td{padding:6px 7px;border-top:1px solid #e5e7eb;text-align:left;vertical-align:top}th{position:sticky;top:0;background:#fff;color:#6b7280;font-size:9px;text-transform:uppercase;letter-spacing:.04em;z-index:1}.sort-button{border:0;background:transparent;color:inherit;font:inherit;text-transform:inherit;letter-spacing:inherit;padding:0;cursor:pointer}.sort-arrow{display:inline-block;min-width:9px}.time-cell{font:10px/1.35 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:nowrap}.time-note{font:9px system-ui;color:#6b7280;margin-top:1px}.level{font-size:9px;font-weight:800;border-radius:999px;padding:3px 5px;background:#e5e7eb}.level.ERROR{background:#fee2e2;color:#991b1b}.level.WARNING{background:#fef3c7;color:#92400e}.category{white-space:nowrap}.source{white-space:nowrap}.source small{display:block;color:#6b7280;margin-top:2px}.event-message{font-weight:600}.raw-details{margin-top:3px}.raw-details summary{cursor:pointer;color:#2563eb;font-size:10px}.raw-box{margin-top:4px;padding:6px 7px;border:1px solid #e5e7eb;border-radius:6px;background:#f9fafb;font:10px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;overflow-wrap:anywhere}.raw-meta{font:10px system-ui;color:#4b5563;margin-bottom:4px}.empty{padding:15px;text-align:center;color:#4b5563;background:#f9fafb;border-radius:7px}.diagnostics{font-size:10px;color:#6b7280;margin-top:6px}.diagnostics summary{cursor:pointer}.diagnostics pre{white-space:pre-wrap;font:9px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace}.error{color:#991b1b;font-size:11px;margin-top:5px}@media(max-width:1000px){.session-header{grid-template-columns:1fr auto}.header-metrics{grid-row:2;grid-column:1/-1}.counts{grid-template-columns:repeat(3,1fr)}.count:nth-child(3){border-right:0}.table-wrap{max-height:none}}@media(max-width:600px){main{padding:9px}.session-header{padding:9px 11px}.counts{grid-template-columns:repeat(2,1fr)}.evidence-heading{align-items:flex-start;flex-direction:column}.header-metrics{overflow-x:auto}}
.period-title{font-size:10px;font-weight:800;text-transform:uppercase;color:#6b7280;margin-bottom:5px}.datetime-row{display:flex;align-items:flex-start;gap:8px;flex-wrap:wrap}.datetime-field{position:relative}.datetime-field>label{display:block;font-size:9px;font-weight:800;color:#6b7280;margin-bottom:2px}.datetime-input-wrap{display:flex}.datetime-input{width:218px;border:1px solid #d1d5db;border-radius:6px 0 0 6px;padding:6px 8px;font:11px ui-monospace,SFMono-Regular,Menlo,monospace}.datetime-input.invalid{border-color:#dc2626;background:#fef2f2}.calendar-button{width:30px;border:1px solid #d1d5db;border-left:0;border-radius:0 6px 6px 0;background:#f9fafb;cursor:pointer;color:#4b5563;padding:0}.calendar-button svg{width:14px;height:14px;vertical-align:middle}.field-error{min-height:12px;color:#b91c1c;font-size:9px;margin-top:2px}.datetime-actions{display:flex;gap:5px;align-items:flex-end;padding-bottom:12px;flex-wrap:wrap}.log-search-row{display:flex;align-items:center;gap:5px;margin:2px 0 5px;flex-wrap:wrap}.log-search-row label{font-size:10px;font-weight:800;color:#6b7280;margin-right:3px}.search-input{width:min(420px,100%);border:1px solid #d1d5db;border-radius:6px;padding:6px 8px;font-size:11px}.search-status{font-size:10px;color:#4b5563;margin-left:3px}.convenience-picker{position:absolute;top:43px;right:0;z-index:15;width:255px;background:#fff;border:1px solid #d1d5db;border-radius:8px;padding:9px;box-shadow:0 10px 24px rgba(17,24,39,.16)}.convenience-picker[hidden]{display:none}.picker-row{display:grid;grid-template-columns:1fr 88px;gap:6px}.picker-row input{min-width:0;border:1px solid #d1d5db;border-radius:6px;padding:6px;font:10px ui-monospace,SFMono-Regular,Menlo,monospace}.picker-footer{display:flex;justify-content:space-between;align-items:center;margin-top:7px}.log-zone{font-size:10px;color:#6b7280}@media(max-width:700px){.datetime-row{display:block}.datetime-field{margin-bottom:5px}.datetime-input-wrap{width:100%}.datetime-input{width:100%}.datetime-actions{padding-bottom:0}.search-input{flex:1;min-width:180px}.convenience-picker{left:0;right:auto}}
</style></head><body>
<header class='session-header'><div><h1 id='ownerName'>Owner: —</h1><div id='identityLine' class='identity-line'></div></div><div id='headerMetrics' class='header-metrics'></div><div class='header-actions'><div><a href='/'>← Dashboard</a><button id='refreshButton' class='button'>Refresh</button></div><div id='lastRefreshed' style='margin-top:3px'>Last refreshed: —</div></div></header>
<main><section class='panel'><div class='period-title'>Evidence Period</div><div class='datetime-row'><div class='datetime-field'><label for='fromInput'>FROM</label><div class='datetime-input-wrap'><input id='fromInput' class='datetime-input' type='text' autocomplete='off' spellcheck='false' placeholder='YYYY-MM-DD HH:MM:SS'><button id='fromCalendarButton' class='calendar-button' type='button' aria-label='Choose From date and time'><svg viewBox='0 0 24 24' aria-hidden='true'><path fill='currentColor' d='M7 2h2v2h6V2h2v2h3a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h3V2Zm13 8H4v10h16V10ZM4 8h16V6h-3v2h-2V6H9v2H7V6H4v2Z'/></svg></button></div><div id='fromInputError' class='field-error'></div><div id='fromPicker' class='convenience-picker' hidden><div class='picker-row'><input id='fromPickerDate' type='date' aria-label='From date'><input id='fromPickerTime' type='text' autocomplete='off' placeholder='HH:MM:SS' maxlength='8' aria-label='From time'></div><div class='picker-footer'><span class='log-zone'>UTC</span><button id='fromPickerUse' class='button' type='button'>Use</button></div><div id='fromPickerError' class='field-error'></div></div></div><div class='datetime-field'><label for='toInput'>TO</label><div class='datetime-input-wrap'><input id='toInput' class='datetime-input' type='text' autocomplete='off' spellcheck='false' placeholder='YYYY-MM-DD HH:MM:SS'><button id='toCalendarButton' class='calendar-button' type='button' aria-label='Choose To date and time'><svg viewBox='0 0 24 24' aria-hidden='true'><path fill='currentColor' d='M7 2h2v2h6V2h2v2h3a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h3V2Zm13 8H4v10h16V10ZM4 8h16V6h-3v2h-2V6H9v2H7V6H4v2Z'/></svg></button></div><div id='toInputError' class='field-error'></div><div id='toPicker' class='convenience-picker' hidden><div class='picker-row'><input id='toPickerDate' type='date' aria-label='To date'><input id='toPickerTime' type='text' autocomplete='off' placeholder='HH:MM:SS' maxlength='8' aria-label='To time'></div><div class='picker-footer'><span class='log-zone'>UTC</span><button id='toPickerUse' class='button' type='button'>Use</button></div><div id='toPickerError' class='field-error'></div></div></div><div class='datetime-actions'><button id='applyButton' class='button primary' type='button'>Apply</button><button id='clearButton' class='button' type='button'>Clear</button><button class='button quick' data-minutes='15'>Last 15 min</button><button class='button quick' data-minutes='60'>Last 1 hour</button><button class='button quick' data-minutes='1440'>Last 24 hours</button></div></div><div class='log-search-row'><label for='searchInput'>Search logs</label><input id='searchInput' class='search-input' type='text' autocomplete='off' spellcheck='false' placeholder='Search log text...'><button id='searchButton' class='button primary' type='button'>Search</button><button id='clearSearchButton' class='button' type='button'>Clear</button><span id='searchStatus' class='search-status'></span></div><div id='rangeLabel' class='coverage'>Time range: All available logs</div><div id='timezoneDebug' class='timezone-debug'>Log timezone: UTC</div><div id='coverage' class='coverage'>Available log data: —</div><label class='timezone-debug'><input id='includeUnplaced' type='checkbox'> Include unplaced evidence · Untimestamped evidence: <span id='unplacedCount'>0</span></label><details class='diagnostics'><summary>Diagnostics</summary><pre id='diagnosticsText'></pre></details><div id='periodError' class='error'></div></section><div id='counts' class='counts'></div><section class='evidence-panel'><div class='evidence-heading'><h2>DCV Evidence</h2><div id='filters' class='filters'></div></div><div id='table' class='table-wrap'></div></section></main>
<script>
const SESSION_ID=__SESSION_ID__;
const filterKeys=['ERROR','WARNING','CONNECTION','LOCKING','DISPLAY_PERFORMANCE','AGENT','INFO','DEBUG'];
const enabled=new Set(filterKeys.filter(key=>key!=='DEBUG'));
let dataState=null,requestInFlight=false,evidenceSortKey='time',evidenceSortDirection='desc',appliedRange=null,appliedSearch='';
const byId=id=>document.getElementById(id);
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]))}
function age(s){if(s===null||s===undefined)return'—';const d=Math.floor(s/86400),h=Math.floor((s%86400)/3600),m=Math.floor((s%3600)/60);return d?`${d}d ${h}h`:h?`${h}h ${m}m`:`${m}m`}
function utcDateTime(date){return date.toISOString().slice(0,19).replace('T',' ')}
function validDate(value){if(!/^\d{4}-\d{2}-\d{2}$/.test(value))return false;const [year,month,day]=value.split('-').map(Number),date=new Date(Date.UTC(year,month-1,day));return date.getUTCFullYear()===year&&date.getUTCMonth()===month-1&&date.getUTCDate()===day}
function validTime(value){if(!/^\d{2}:\d{2}:\d{2}$/.test(value))return false;const [hour,minute,second]=value.split(':').map(Number);return hour<=23&&minute<=59&&second<=59}
function parseLogDateTime(value){const match=/^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})$/.exec(value);if(!match)return {error:'Use YYYY-MM-DD HH:MM:SS'};if(!validDate(match[1]))return {error:'Invalid date'};if(!validTime(match[2]))return {error:'Invalid time'};return {value,iso:`${match[1]}T${match[2]}.000Z`}}
function hasTimeFilter(){return appliedRange!==null}
function apiUrl(){const params=new URLSearchParams();if(appliedRange){params.set('from',appliedRange.fromIso);params.set('to',appliedRange.toIso)}if(appliedSearch)params.set('q',appliedSearch);if(byId('includeUnplaced').checked&&hasTimeFilter())params.set('include_unplaced','true');const query=params.toString();return `/api/sessions/${encodeURIComponent(SESSION_ID)}${query?'?'+query:''}`}
function clearFieldErrors(){for(const id of ['fromInput','toInput']){byId(id).classList.remove('invalid');byId(`${id}Error`).textContent=''}}
function fieldError(id,message){byId(id).classList.add('invalid');byId(`${id}Error`).textContent=message}
function validateInputs(){clearFieldErrors();const fromText=byId('fromInput').value.trim(),toText=byId('toInput').value.trim();let valid=true,from=null,to=null;if(!fromText){fieldError('fromInput','From is required');valid=false}else{from=parseLogDateTime(fromText);if(from.error){fieldError('fromInput',from.error);valid=false}}if(!toText){fieldError('toInput','To is required');valid=false}else{to=parseLogDateTime(toText);if(to.error){fieldError('toInput',to.error);valid=false}}if(valid&&to.iso<from.iso){fieldError('toInput','To must be at or after From');valid=false}return valid?{from:from.value,to:to.value,fromIso:from.iso,toIso:to.iso}:null}
function updateRangeLabel(){byId('rangeLabel').textContent=appliedRange?`Time range: ${appliedRange.from} → ${appliedRange.to}`:'Time range: All available logs'}
function closePickers(){byId('fromPicker').hidden=true;byId('toPicker').hidden=true}
function togglePicker(target){const picker=byId(`${target}Picker`),opening=picker.hidden;closePickers();if(!opening)return;const parsed=parseLogDateTime(byId(`${target}Input`).value.trim()),base=parsed.error?utcDateTime(new Date()):parsed.value;byId(`${target}PickerDate`).value=base.slice(0,10);byId(`${target}PickerTime`).value=base.slice(11);byId(`${target}PickerError`).textContent='';picker.hidden=false;const dateInput=byId(`${target}PickerDate`);if(typeof dateInput.showPicker==='function')dateInput.showPicker()}
function usePicker(target){const date=byId(`${target}PickerDate`).value,time=byId(`${target}PickerTime`).value.trim(),error=byId(`${target}PickerError`);if(!validDate(date)){error.textContent='Choose a valid date';return}if(!validTime(time)){error.textContent='Use 24-hour HH:MM:SS';return}byId(`${target}Input`).value=`${date} ${time}`;byId(`${target}Input`).classList.remove('invalid');byId(`${target}InputError`).textContent='';closePickers()}
function applyRange(){const range=validateInputs();if(!range)return;appliedRange=range;updateRangeLabel();closePickers();loadEvidence()}
function setQuick(minutes){const end=new Date(Math.ceil(Date.now()/1000)*1000),start=new Date(end.getTime()-minutes*60000),from=utcDateTime(start),to=utcDateTime(end);byId('fromInput').value=from;byId('toInput').value=to;appliedRange={from,to,fromIso:`${from.replace(' ','T')}.000Z`,toIso:`${to.replace(' ','T')}.000Z`};clearFieldErrors();updateRangeLabel();closePickers();loadEvidence()}
function clearTimeFilter(){appliedRange=null;byId('fromInput').value='';byId('toInput').value='';byId('includeUnplaced').checked=false;clearFieldErrors();updateRangeLabel();closePickers();loadEvidence()}
function runSearch(){appliedSearch=byId('searchInput').value.trim();loadEvidence()}
function clearSearch(){appliedSearch='';byId('searchInput').value='';loadEvidence()}
function fullTime(value){return value?String(value).replace('T',' ').replace(/(?:\+00:00|Z)$/,''):'Timestamp unavailable'}
function eventTime(event){if(event.time_type==='exact')return [fullTime(event.timestamp),''];if(event.time_type==='bracketed'){const end=fullTime(event.time_to).split(' ')[1];return [`${fullTime(event.time_from)} – ${end}`,'inferred']}if(event.time_type==='near')return [`~${fullTime(event.approximate_time)}`,'approximate'];return ['Timestamp unavailable','']}
function categoryLabel(event){const labels={CONNECTION:'Connection',LOCKING:'Lock',DISPLAY_PERFORMANCE:'Display',SESSION_LIFECYCLE:'Session',AGENT:'Agent'};return labels[event.category]||event.category}
function eventSortTime(event){const value=event.timestamp||event.approximate_time||event.time_from||event.time_to;return value?Date.parse(`${value}Z`):null}
function evidenceValue(event,key){if(key==='time')return eventSortTime(event);if(key==='level')return String(event.severity||'').toLowerCase();if(key==='category')return categoryLabel(event).toLowerCase();if(key==='source')return `${event.component||''} ${event.source_filename||''}`.toLowerCase();return String(event.normalized_message||'').toLowerCase()}
function sortedEvidence(events){return [...events].sort((a,b)=>{const av=evidenceValue(a,evidenceSortKey),bv=evidenceValue(b,evidenceSortKey);if(evidenceSortKey==='time'){if(av===null&&bv===null)return 0;if(av===null)return 1;if(bv===null)return-1}const result=typeof av==='number'?av-bv:av.localeCompare(bv);return evidenceSortDirection==='asc'?result:-result})}
function evidenceArrow(key){return evidenceSortKey===key?(evidenceSortDirection==='asc'?'↑':'↓'):''}
function sortEvidence(key){if(evidenceSortKey===key)evidenceSortDirection=evidenceSortDirection==='asc'?'desc':'asc';else{evidenceSortKey=key;evidenceSortDirection='asc'}renderTable()}
function evidenceHeader(label,key){return `<button class='sort-button' onclick="sortEvidence('${key}')">${label} <span class='sort-arrow'>${evidenceArrow(key)}</span></button>`}
function isVisible(event){if(event.severity==='DEBUG'&&!enabled.has('DEBUG'))return false;if(enabled.has(event.severity))return true;if(enabled.has('CONNECTION')&&event.category==='CONNECTION')return true;if(enabled.has('LOCKING')&&event.category==='LOCKING')return true;if(enabled.has('DISPLAY_PERFORMANCE')&&event.category==='DISPLAY_PERFORMANCE')return true;if(enabled.has('AGENT')&&event.category==='AGENT')return true;return false}
function renderFilters(){const labels={ERROR:'Error',WARNING:'Warning',CONNECTION:'Connection',LOCKING:'Lock',DISPLAY_PERFORMANCE:'Display',AGENT:'Agent',INFO:'Info',DEBUG:'Debug'},allActive=filterKeys.every(key=>enabled.has(key));byId('filters').innerHTML=`<button class='filter ${allActive?'active':''}' data-key='ALL'>All</button>`+filterKeys.map(key=>`<button class='filter ${enabled.has(key)?'active':''}' data-key='${key}'>${labels[key]}</button>`).join('');byId('filters').querySelectorAll('button').forEach(button=>button.onclick=()=>{const key=button.dataset.key;if(key==='ALL'){filterKeys.forEach(item=>enabled.add(item))}else{enabled.has(key)?enabled.delete(key):enabled.add(key)}renderFilters();renderTable()})}
function contextText(event){const lines=[...(event.context_before||[]),{line_number:event.source_line_number,text:event.raw_log_line},...(event.context_after||[])];return lines.length>1?`\n\nContext:\n${lines.map(line=>`${line.line_number===event.source_line_number?'→':' '} ${line.line_number}  ${line.text}`).join('\n')}`:''}
function rawDetails(event){const type={exact:'exact',bracketed:'bracketed',near:'approximate',unknown:'unavailable'}[event.time_type]||'unavailable';return `<details class='raw-details'><summary>View raw</summary><div class='raw-box'><div class='raw-meta'>Source: ${esc(event.source_filename)} · line ${esc(event.source_line_number)} · timestamp type: ${esc(type)}</div>${esc(event.raw_log_line)}${esc(contextText(event))}</div></details>`}
function renderTable(){const events=sortedEvidence((dataState?.evidence_timeline||[]).filter(isVisible));const noSearchMatches=appliedSearch&&(dataState?.search_match_count??0)===0,emptyMessage=noSearchMatches?'No matching log evidence for this search.':'No matching evidence is currently displayed.';byId('table').innerHTML=events.length?`<table><thead><tr><th>${evidenceHeader('Date/Time','time')}</th><th>${evidenceHeader('Level','level')}</th><th>${evidenceHeader('Category','category')}</th><th>${evidenceHeader('Source','source')}</th><th>${evidenceHeader('Event','event')}</th></tr></thead><tbody>${events.map(event=>{const [time,note]=eventTime(event);return `<tr><td class='time-cell'>${esc(time)}${note?`<div class='time-note'>${esc(note)}</div>`:''}</td><td><span class='level ${esc(event.severity)}'>${esc(event.severity)}</span></td><td class='category'>${esc(categoryLabel(event))}</td><td class='source'>${esc(event.component)}<small>${esc(event.source_filename)}:${esc(event.source_line_number)}</small></td><td><div class='event-message'>${esc(event.normalized_message)}</div>${rawDetails(event)}</td></tr>`}).join('')}</tbody></table>`:`<div class='empty'>${emptyMessage}</div>`}
function render(data){dataState=data;const s=data.session,m=data.session_metrics,d=data.diagnostics||{},t=data.time_interpretation||{},countPrefix=appliedSearch?'Matching ':'';byId('ownerName').textContent=`Owner: ${s.owner}`;byId('identityLine').innerHTML=`<span>Session: ${esc(s.id)}</span><span>Server: ${esc(s.hostname)}</span><span>Type: ${esc(s.type)}</span><span>State: ${esc(s.state)}</span>`;byId('headerMetrics').innerHTML=`<span>Age<b>${age(s.age_seconds)}</b></span><span>Session CPU<b>${s.dcv_process_cpu_pct??0}%</b></span><span>Session Memory<b>${s.dcv_process_mem_pct??0}%</b></span>`;byId('lastRefreshed').textContent=`Last refreshed: ${new Date().toLocaleTimeString()}`;updateRangeLabel();byId('searchStatus').textContent=appliedSearch?`Search: "${appliedSearch}" · Matches: ${data.search_match_count??0}`:'';byId('coverage').textContent=`Available log data: ${fullTime(data.earliest_timestamp)} → ${fullTime(data.latest_timestamp)}`;byId('timezoneDebug').textContent='Log timezone: UTC';byId('unplacedCount').textContent=data.unplaced_evidence_count||0;const factual=[[`${countPrefix}disconnect events`,m.client_disconnects],[`${countPrefix}frame ACK stalls`,m.frame_ack_stalls],[`${countPrefix}OS lock failures`,m.os_lock_failures],[`${countPrefix}unexpected agent exits`,m.unexpected_agent_exits],[`${countPrefix}errors`,data.severity_counts?.ERROR||0],[`${countPrefix}warnings`,data.severity_counts?.WARNING||0]];byId('counts').innerHTML=factual.map(([label,value])=>`<div class='count'><div class='count-label'>${label}</div><div class='count-value'>${value}</div></div>`).join('');byId('diagnosticsText').textContent=`Log files read: ${(d.files_read||[]).length}\nLines parsed: ${d.lines_parsed||0}\nTimestamped lines: ${d.timestamped_lines||0}\nUntimestamped lines: ${d.untimestamped_lines||0}\nLines inside window: ${d.lines_inside_requested_window||0}\nEarliest parsed: ${d.earliest_parsed_timestamp||'—'}\nLatest parsed: ${d.latest_parsed_timestamp||'—'}\nRequested: ${t.requested_from||'all'} → ${t.requested_to||'all'}\nInterpreted UTC: ${d.interpreted_log_from||'all'} → ${d.interpreted_log_to||'all'}\nServer timezone: ${t.server_timezone||'—'}`;renderFilters();renderTable();byId('periodError').textContent=(data.log_errors||[]).join(' · ')}
async function loadEvidence(){if(requestInFlight)return;requestInFlight=true;const buttons=[byId('refreshButton'),byId('applyButton'),byId('clearButton'),byId('searchButton'),byId('clearSearchButton'),...document.querySelectorAll('.quick')];buttons.forEach(button=>button.disabled=true);byId('refreshButton').textContent='Refreshing...';byId('periodError').textContent='';try{const response=await fetch(apiUrl(),{cache:'no-store'});if(!response.ok){const body=await response.json().catch(()=>({}));throw new Error(body.detail||`Evidence request failed (${response.status})`)}render(await response.json())}catch(error){byId('periodError').textContent=error.message}finally{buttons.forEach(button=>button.disabled=false);byId('refreshButton').textContent='Refresh';requestInFlight=false}}
byId('refreshButton').onclick=loadEvidence;byId('applyButton').onclick=applyRange;byId('clearButton').onclick=clearTimeFilter;byId('searchButton').onclick=runSearch;byId('clearSearchButton').onclick=clearSearch;byId('searchInput').addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();runSearch()}});for(const target of ['from','to']){byId(`${target}CalendarButton`).onclick=()=>togglePicker(target);byId(`${target}PickerUse`).onclick=()=>usePicker(target)}byId('includeUnplaced').onchange=()=>{if(hasTimeFilter())loadEvidence()};document.querySelectorAll('.quick').forEach(button=>button.onclick=()=>setQuick(Number(button.dataset.minutes)));document.addEventListener('click',event=>{if(!event.target.closest('.datetime-field'))closePickers()});document.addEventListener('keydown',event=>{if(event.key==='Escape')closePickers()});updateRangeLabel();loadEvidence();
</script></body></html>
"""
