#!/usr/bin/env python3
import re
import subprocess
import threading
from datetime import datetime
from collections import deque
from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import asyncio

app = FastAPI(title="Dae Dashboard")
app.mount("/static", StaticFiles(directory="/opt/dae-dashboard/static"), name="static")

# Journalctl-based log reader
class JournalLogReader:
    def __init__(self, max_lines=2000):
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
    GROUP_RESELECT = re.compile(r'_new_dialer="([^"]+)".*group="?([^\s"]+)"?.*min_moving_avg=([\d.]+)(ms|s).*network=(\S+)')
    # Node line: after "]: " prefix, then whitespace, number, dot, [subtag], name: time
    # Format: "   1. [dlg] 🇸🇬 SG 04: 5.216s"
    NODE_LINE = re.compile(r'\s+(\d+)\.\s+\[([^\]]+)\]\s+([^:]+):\s+([\d.]+)(ms|s)')

    def __init__(self):
        pass

    def get_freshness(self):
        return log_reader.get_freshness()

    def _strip_journal_prefix(self, line):
        """Remove journalctl timestamp prefix: '3月 13 16:53:29 x dae[8520]: '"""
        # Find the "]: " separator that ends the journal prefix
        parts = line.split(']: ', 1)
        if len(parts) > 1:
            return parts[1]
        return line

    def parse_groups(self):
        groups = {}
        lines = log_reader.get_lines()
        i = 0

        while i < len(lines):
            line = lines[i]
            # Strip journal prefix to get the actual message
            msg = self._strip_journal_prefix(line)

            # Check for Group header in message
            if "Group '" in msg and "]:" in msg:
                m = re.search(r"Group '([^']+)' \[([^\]]+)\]:", msg)
                if m:
                    g = m.group(1)
                    network = m.group(2)
                    if g not in groups:
                        groups[g] = {'selected': {}, 'nodes': [], 'networks': set()}

                    if network not in groups[g]['networks']:
                        groups[g]['networks'].add(network)

                    # Parse node lines following the group header
                    j = i + 1
                    while j < len(lines):
                        next_msg = self._strip_journal_prefix(lines[j])
                        # Check if it's a node line (starts with whitespace + number + .)
                        nm = self.NODE_LINE.search(next_msg)
                        if nm:
                            rank = nm.group(1)
                            subtag = nm.group(2)
                            name = nm.group(3).strip()
                            time_val = nm.group(4)
                            unit = nm.group(5)

                            # Convert to milliseconds
                            if unit == 's':
                                latency = int(float(time_val) * 1000)
                            else:
                                latency = int(time_val)

                            node_info = {'subtag': subtag, 'name': name, 'latency': latency, 'rank': int(rank)}
                            # Avoid duplicates
                            if not any(n['subtag'] == subtag and n['name'] == name for n in groups[g]['nodes']):
                                groups[g]['nodes'].append(node_info)
                            j += 1
                        else:
                            # Not a node line, stop parsing
                            break
            i += 1

        for g in groups:
            groups[g]['networks'] = list(groups[g]['networks'])
        return groups

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


parser = LogParser()


@app.get("/api/status")
async def status():
    f = parser.get_freshness()
    try:
        r = subprocess.run(['pgrep', '-f', 'dae run'], capture_output=True, text=True)
        p = bool(r.stdout.strip())
    except:
        p = False
    return {'log_freshness': f, 'process_running': p, 'overall_status': 'online' if f['is_fresh'] and p else 'offline'}


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
async def connections():
    return {'connections': []}


@app.get("/api/dns")
async def dns():
    return {'queries': []}


@app.get("/api/summary")
async def summary():
    f = parser.get_freshness()
    g = parser.parse_groups() if f['is_fresh'] else {}
    return {'groups': list(g.keys()), 'total_nodes': sum(len(v.get('nodes', [])) for v in g.values()), 'log_freshness': f}


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
            await websocket.send_json({
                'status': {'overall': 'online' if f['is_fresh'] else 'offline', 'age_seconds': f['age_seconds']},
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
