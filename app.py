#!/usr/bin/env python3
import re
import subprocess
import threading
import os
import tempfile
from datetime import datetime
from collections import deque
from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import asyncio

app = FastAPI(title="Dae Dashboard")
STATIC_DIR = "/opt/dae-dashboard/static"
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

SERVICE_NAME = "dae"
CONFIG_PATH = "/usr/local/etc/dae/config.dae"
CONFIG_ENV_KEY = "DAE_CONFIG_PATH"

# Journalctl-based log reader
class JournalLogReader:
    def __init__(self, max_lines=20000):
        self.max_lines = max_lines
        self.lines = deque(maxlen=max_lines)
        self.last_update = None
        self.running = False
        self.thread = None
        self.proc = None

    def start(self):
        if self.running:
            return

        self.running = True

        def read_journal():
            try:
                # First, read recent logs
                result = subprocess.run(
                    ['journalctl', '-u', 'dae', '-n', str(self.max_lines // 2), '--no-pager'],
                    capture_output=True, text=True
                )
                for line in result.stdout.strip().split('\n'):
                    if line:
                        self.lines.append(line)
                        self.last_update = datetime.now()

                # Then follow new logs
                self.proc = subprocess.Popen(
                    ['journalctl', '-u', 'dae', '-f', '--no-pager'],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1
                )

                while self.running:
                    line = self.proc.stdout.readline()
                    if line:
                        self.lines.append(line.rstrip('\n'))
                        self.last_update = datetime.now()
                    elif not self.running:
                        break
                    else:
                        import time
                        time.sleep(0.1)

            except Exception as e:
                print(f"Journal reader error: {e}")
                import traceback
                traceback.print_exc()
            finally:
                self.running = False
                if self.proc:
                    try:
                        self.proc.terminate()
                        self.proc.wait(timeout=2)
                    except:
                        self.proc.kill()

        self.thread = threading.Thread(target=read_journal, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.proc:
            try:
                self.proc.terminate()
            except:
                pass
        if self.thread:
            self.thread.join(timeout=2)

    def get_lines(self, count=None):
        lines = list(self.lines)
        if count:
            return lines[-count:]
        return lines

    def get_freshness(self):
        if self.last_update is None:
            return {'is_fresh': False, 'age_seconds': 999999, 'status': 'not_found'}
        age = int(datetime.now().timestamp() - self.last_update.timestamp())
        return {'is_fresh': age < 60, 'age_seconds': age, 'status': 'online' if age < 60 else 'offline'}


log_reader = JournalLogReader()
log_reader.start()


class LogParser:
    # Group reselect: contains _new_dialer and min_moving_avg
    GROUP_RESELECT = re.compile(r'_new_dialer="([^"]+)".*group="?([^\s"]+)"?.*min_moving_avg=([\d.]+)(ms|s).*network=([^\s"]+)')
    GROUP_HEADER_RE = re.compile(r"Group '([^']+)' \[([^\]]+)\]:")
    # Node line: after "]: " prefix, then whitespace, number, dot, [subtag], name: time
    # Format: "   1. [dlg] 🇸🇬 SG 04: 5.216s"
    NODE_LINE = re.compile(r'\s+(\d+)\.\s+\[([^\]]+)\]\s+(.+):\s+([\d.]+)(ms|s)')
    KV_RE = re.compile(r'(\w+)=(".*?"|\S+)')
    IP_PORT_RE = re.compile(r'(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})')
    MSG_CONN_RE = re.compile(r'(\d{1,3}(?:\.\d{1,3}){3}):(\d+)\s+<->\s+([^\s:]+):(\d+)')
    DNS_QUERY_RE = re.compile(r'(?i)\bdns\b.*?(?:query|lookup)\s+([^\s]+)')
    HOST_RE = re.compile(r'(?i)\b(host|domain|qname)=(".*?"|\S+)')

    def __init__(self):
        self.group_cache = {}
        self.group_cache_ts = 0
        self.group_cache_ttl = 300
        self.group_scan_lines = 100000

    def get_freshness(self):
        return log_reader.get_freshness()

    def _strip_journal_prefix(self, line):
        """Remove journalctl timestamp prefix: '3月 13 16:53:29 x dae[8520]: '"""
        # Find the "]: " separator that ends the journal prefix
        parts = line.split(']: ', 1)
        if len(parts) > 1:
            return parts[1]
        return line

    def _clean_value(self, v):
        if v is None:
            return v
        if isinstance(v, str) and len(v) >= 2 and v[0] == '"' and v[-1] == '"':
            return v[1:-1]
        return v

    def _parse_groups_from_lines(self, lines):
        groups = {}
        i = 0
        while i < len(lines):
            msg = self._strip_journal_prefix(lines[i])
            if "Group '" in msg and "]:" in msg:
                m = self.GROUP_HEADER_RE.search(msg)
                if m:
                    g = m.group(1)
                    network = self._clean_value(m.group(2))
                    if g not in groups:
                        groups[g] = {'selected': {}, 'nodes': [], 'networks': set()}
                    groups[g]['networks'].add(network)
                    j = i + 1
                    while j < len(lines):
                        next_msg = self._strip_journal_prefix(lines[j])
                        nm = self.NODE_LINE.search(next_msg)
                        if not nm:
                            break
                        rank = nm.group(1)
                        subtag = nm.group(2)
                        name = nm.group(3).strip()
                        time_val = nm.group(4)
                        unit = nm.group(5)
                        latency = int(float(time_val) * 1000) if unit == 's' else int(float(time_val))
                        node_info = {'subtag': subtag, 'name': name, 'latency': latency, 'rank': int(rank)}
                        if not any(n['subtag'] == subtag and n['name'] == name for n in groups[g]['nodes']):
                            groups[g]['nodes'].append(node_info)
                        j += 1
            i += 1

        for line in lines:
            msg = self._strip_journal_prefix(line)
            m = self.GROUP_RESELECT.search(msg)
            if not m:
                continue
            dialer = self._clean_value(m.group(1))
            group = self._clean_value(m.group(2))
            avg = m.group(3)
            unit = m.group(4)
            network = self._clean_value(m.group(5))
            if group not in groups:
                groups[group] = {'selected': {}, 'nodes': [], 'networks': set()}
            latency = int(float(avg) * 1000) if unit == 's' else int(float(avg))
            groups[group]['selected'][network] = {'dialer': dialer, 'latency': latency}
            groups[group]['networks'].add(network)

        for g in groups:
            groups[g]['networks'] = list(groups[g]['networks'])
        return groups

    def _merge_groups(self, base, extra):
        merged = {}
        for g, data in base.items():
            merged[g] = {
                'selected': dict(data.get('selected', {})),
                'nodes': list(data.get('nodes', [])),
                'networks': set(data.get('networks', []))
            }
        for g, data in extra.items():
            if g not in merged:
                merged[g] = {'selected': {}, 'nodes': [], 'networks': set()}
            merged[g]['selected'].update(data.get('selected', {}))
            merged[g]['networks'].update(data.get('networks', []))
            existing = {(n['subtag'], n['name']) for n in merged[g]['nodes']}
            for n in data.get('nodes', []):
                key = (n['subtag'], n['name'])
                if key not in existing:
                    merged[g]['nodes'].append(n)
                    existing.add(key)
        for g in merged:
            merged[g]['networks'] = list(merged[g]['networks'])
        return merged

    def _find_group_headers(self):
        r = _run_cmd(['journalctl', '-u', SERVICE_NAME, '--no-pager', '-o', 'short-iso', '-g', "Group '", '-n', '2000'])
        if not r['ok'] or not r['stdout']:
            return {}
        headers = {}
        for line in r['stdout'].splitlines():
            ts = line.split(' ', 1)[0]
            msg = self._strip_journal_prefix(line)
            m = self.GROUP_HEADER_RE.search(msg)
            if not m:
                continue
            group = m.group(1)
            headers[group] = ts
        return headers

    def _read_journal_window(self, ts, seconds=3):
        try:
            dt = datetime.fromisoformat(ts)
            end = dt.timestamp() + seconds
            until = datetime.fromtimestamp(end, tz=dt.tzinfo).isoformat()
        except Exception:
            return []
        r = _run_cmd(['journalctl', '-u', SERVICE_NAME, '--no-pager', '-o', 'short-iso', '--since', ts, '--until', until])
        if not r['ok'] or not r['stdout']:
            return []
        return r['stdout'].splitlines()

    def _scan_journal_for_groups(self):
        headers = self._find_group_headers()
        if not headers:
            r = _run_cmd(['journalctl', '-u', SERVICE_NAME, '--no-pager', '-n', str(self.group_scan_lines)])
            if not r['ok'] or not r['stdout']:
                return {}
            return self._parse_groups_from_lines(r['stdout'].splitlines())
        merged = {}
        for _, ts in headers.items():
            window = self._read_journal_window(ts)
            if not window:
                continue
            parsed = self._parse_groups_from_lines(window)
            merged = self._merge_groups(merged, parsed)
        return merged

    def parse_groups(self):
        now = datetime.now().timestamp()
        recent = self._parse_groups_from_lines(log_reader.get_lines())
        scanned = None
        if (now - self.group_cache_ts) >= self.group_cache_ttl:
            scanned = self._scan_journal_for_groups()
        base = scanned or self.group_cache or {}
        merged = self._merge_groups(base, recent) if base or recent else {}
        if merged:
            self.group_cache = merged
            self.group_cache_ts = now
            return merged
        return self.group_cache or recent or {}

    def get_all_nodes(self):
        groups = self.parse_groups()
        nodes = {}

        for g, data in groups.items():
            for n in data.get('nodes', []):
                key = f"{n['subtag']}_{n['name']}"
                if key not in nodes:
                    nodes[key] = {'subtag': n['subtag'], 'name': n['name'], 'latency': n['latency'], 'groups': []}
                if g not in nodes[key]['groups']:
                    nodes[key]['groups'].append(g)
        return nodes

    def _parse_kv(self, msg):
        kv = {}
        for k, v in self.KV_RE.findall(msg):
            if v.startswith('"') and v.endswith('"'):
                v = v[1:-1]
            kv[k] = v
        return kv

    def _parse_ip_port(self, value):
        if not value:
            return None, None
        m = self.IP_PORT_RE.search(value)
        if not m:
            return None, None
        return m.group(1), int(m.group(2))

    def parse_connections(self, limit=200):
        lines = log_reader.get_lines()[-limit*5:]
        results = []
        for line in reversed(lines):
            msg = self._strip_journal_prefix(line)
            if "<->" not in msg:
                continue
            kv = self._parse_kv(msg)
            msg_body = kv.get('msg') or msg
            m = self.MSG_CONN_RE.search(msg_body)
            src_ip, src_port, dst_host, dst_port = (None, None, None, None)
            if m:
                src_ip, src_port, dst_host, dst_port = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
            dst_ip, dst_port2 = self._parse_ip_port(kv.get('ip') or kv.get('dst') or kv.get('daddr') or kv.get('dst_ip') or kv.get('target'))
            if dst_ip and dst_port2:
                dst_host = dst_ip
                dst_port = dst_port2
            if not dst_host and not dst_port:
                continue
            results.append({
                'src_ip': src_ip or '-',
                'src_port': src_port or 0,
                'dst_ip': dst_host or '-',
                'dst_port': dst_port or 0,
                'dialer': kv.get('dialer') or kv.get('via') or kv.get('node') or '-',
                'outbound': kv.get('outbound') or kv.get('group') or '-',
                'network': kv.get('network') or kv.get('proto') or '-',
                'domain': kv.get('sniffed') or kv.get('domain') or kv.get('host') or kv.get('qname') or kv.get('_qname') or ''
            })
            if len(results) >= limit:
                break
        return results

    def parse_dns(self, limit=200):
        lines = log_reader.get_lines()[-limit*5:]
        results = []
        for line in reversed(lines):
            msg = self._strip_journal_prefix(line)
            lower = msg.lower()
            if "dns" not in lower:
                continue
            kv = self._parse_kv(msg)
            qname = kv.get('_qname') or kv.get('qname') or kv.get('query') or kv.get('domain') or kv.get('host')
            if not qname:
                m = self.DNS_QUERY_RE.search(msg)
                qname = m.group(1) if m else None
            if not qname:
                continue
            if qname.endswith('.'):
                qname = qname[:-1]
            results.append({
                'qname': qname,
                'dialer': kv.get('dialer') or kv.get('server') or kv.get('resolver') or '-',
                'network': kv.get('network') or kv.get('proto') or '-'
            })
            if len(results) >= limit:
                break
        return results

    def parse_traffic(self, limit=200):
        lines = log_reader.get_lines()[-limit*5:]
        results = []
        for line in reversed(lines):
            msg = self._strip_journal_prefix(line)
            kv = self._parse_kv(msg)
            m = self.HOST_RE.search(msg)
            host = None
            if m:
                host = m.group(2)
                if host.startswith('"') and host.endswith('"'):
                    host = host[1:-1]
            host = host or kv.get('sniffed') or kv.get('_qname') or kv.get('host') or kv.get('domain') or kv.get('qname')
            if not host:
                continue
            if host.endswith('.'):
                host = host[:-1]
            results.append({
                'host': host,
                'dialer': kv.get('dialer') or kv.get('node') or '-',
                'group': kv.get('group') or kv.get('outbound') or '-',
                'dns': kv.get('server') or kv.get('resolver') or (kv.get('dialer') if 'DNS' in (kv.get('network') or '') else '-') or '-',
                'network': kv.get('network') or kv.get('proto') or '-'
            })
            if len(results) >= limit:
                break
        return results


parser = LogParser()


def _run_cmd(cmd, timeout=5):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {'ok': r.returncode == 0, 'code': r.returncode, 'stdout': r.stdout.strip(), 'stderr': r.stderr.strip()}
    except Exception as e:
        return {'ok': False, 'code': -1, 'stdout': '', 'stderr': str(e)}


def _systemd_status():
    r = _run_cmd(['systemctl', 'show', SERVICE_NAME, '--no-page',
                  '-p', 'ActiveState', '-p', 'SubState', '-p', 'MainPID',
                  '-p', 'ExecMainStatus', '-p', 'ExecMainCode', '-p', 'Result', '-p', 'UnitFileState'])
    data = {}
    if r['ok']:
        for line in r['stdout'].splitlines():
            if '=' in line:
                k, v = line.split('=', 1)
                data[k] = v
    return {
        'active_state': data.get('ActiveState', 'unknown'),
        'sub_state': data.get('SubState', 'unknown'),
        'main_pid': int(data.get('MainPID', '0') or 0),
        'exec_main_status': data.get('ExecMainStatus', ''),
        'exec_main_code': data.get('ExecMainCode', ''),
        'result': data.get('Result', ''),
        'unit_file_state': data.get('UnitFileState', ''),
        'raw': r
    }


def _resolve_config_path():
    candidates = []
    env_path = os.environ.get(CONFIG_ENV_KEY)
    if env_path:
        candidates.append(env_path)
    candidates += [CONFIG_PATH, "/etc/dae/config.dae", "/usr/local/etc/dae/dae.conf"]
    r = _run_cmd(['systemctl', 'show', SERVICE_NAME, '--no-page', '-p', 'ExecStart'])
    if r['ok'] and r['stdout']:
        m = re.search(r'--config(?:=|\s+)(\S+)', r['stdout'])
        if m:
            candidates.insert(0, m.group(1))
        m2 = re.search(r'-c\s+(\S+)', r['stdout'])
        if m2:
            candidates.insert(0, m2.group(1))
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return candidates[0]


@app.get("/api/status")
async def status():
    f = parser.get_freshness()
    systemd = _systemd_status()
    active = systemd.get('active_state') == 'active'
    overall = 'online' if active and f['is_fresh'] else 'degraded' if active else 'offline'
    return {
        'log_freshness': f,
        'systemd': systemd,
        'overall_status': overall
    }


@app.get("/api/groups")
async def groups():
    f = parser.get_freshness()
    if not f['is_fresh']:
        return {'error': 'stale', 'age_seconds': f['age_seconds']}
    return parser.parse_groups()


@app.get("/api/nodes")
async def nodes():
    f = parser.get_freshness()
    if not f['is_fresh']:
        return {'error': 'stale', 'age_seconds': f['age_seconds'], 'nodes': {}}
    return {'nodes': parser.get_all_nodes()}


@app.get("/api/connections")
async def connections(limit: int = 200):
    f = parser.get_freshness()
    if not f['is_fresh']:
        return {'error': 'stale', 'age_seconds': f['age_seconds'], 'connections': []}
    return {'connections': parser.parse_connections(limit=limit)}


@app.get("/api/dns")
async def dns(limit: int = 200):
    f = parser.get_freshness()
    if not f['is_fresh']:
        return {'error': 'stale', 'age_seconds': f['age_seconds'], 'queries': []}
    return {'queries': parser.parse_dns(limit=limit)}


@app.get("/api/traffic")
async def traffic(limit: int = 200):
    f = parser.get_freshness()
    if not f['is_fresh']:
        return {'error': 'stale', 'age_seconds': f['age_seconds'], 'records': []}
    return {'records': parser.parse_traffic(limit=limit)}


@app.get("/api/summary")
async def summary():
    f = parser.get_freshness()
    g = parser.parse_groups() if f['is_fresh'] else {}
    total_nodes = len(parser.get_all_nodes()) if f['is_fresh'] else 0
    return {'groups': list(g.keys()), 'total_nodes': total_nodes, 'log_freshness': f}


@app.get("/api/config")
async def get_config():
    cfg_path = _resolve_config_path()
    try:
        with open(cfg_path, 'r') as f:
            content = f.read()
        stat = os.stat(cfg_path)
        return {'content': content, 'path': cfg_path, 'size': stat.st_size, 'mtime': stat.st_mtime, 'env_key': CONFIG_ENV_KEY}
    except Exception as e:
        return {'error': str(e), 'path': cfg_path, 'env_key': CONFIG_ENV_KEY}


@app.put("/api/config")
async def save_config(payload: dict):
    cfg_path = _resolve_config_path()
    content = payload.get('content', '')
    try:
        dirpath = os.path.dirname(cfg_path)
        with tempfile.NamedTemporaryFile('w', delete=False, dir=dirpath) as tmp:
            tmp.write(content)
            temp_name = tmp.name
        os.replace(temp_name, cfg_path)
        return {'ok': True, 'path': cfg_path, 'env_key': CONFIG_ENV_KEY}
    except Exception as e:
        return {'ok': False, 'error': str(e), 'path': cfg_path, 'env_key': CONFIG_ENV_KEY}


@app.post("/api/reload")
async def reload_config():
    r = _run_cmd(['dae', 'reload'])
    return r


@app.get("/api/logs")
async def logs(limit: int = 100):
    es = []
    for line in log_reader.get_lines()[-limit:]:
        msg = parser._strip_journal_prefix(line)
        tm = re.search(r'^(\w+\s+\d+\s+[\d:]+)', line)
        lv = re.search(r'level=(\w+)', line)
        es.append({
            'time': tm.group(1) if tm else '',
            'level': lv.group(1) if lv else 'info',
            'message': msg
        })
    return {'logs': es}


@app.get("/", response_class=HTMLResponse)
async def root():
    with open("/opt/dae-dashboard/templates/index.html", 'r') as f:
        return f.read()


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            f = parser.get_freshness()
            systemd = _systemd_status()
            active = systemd.get('active_state') == 'active'
            overall = 'online' if active and f['is_fresh'] else 'degraded' if active else 'offline'
            await websocket.send_json({
                'status': {
                    'overall': overall,
                    'age_seconds': f['age_seconds'],
                    'systemd': systemd
                },
                'groups': parser.parse_groups() if f['is_fresh'] else {},
                'ts': datetime.now().isoformat()
            })
            await asyncio.sleep(2)
    except:
        pass


if __name__ == "__main__":
    import uvicorn
    try:
        uvicorn.run(app, host="0.0.0.0", port=8899)
    finally:
        log_reader.stop()
