# dae Dashboard

A web-based monitoring and control dashboard for [dae](https://github.com/daeuniverse/dae) - a Linux eBPF-based proxy service.

## Features

- **Real-time monitoring**: Live updates via WebSocket
- **Systemd status**: ActiveState/SubState/MainPID with health summary
- **Config editor**: View and edit `/usr/local/etc/dae/config.dae`
- **Reload control**: Trigger `dae reload` from the UI
- **Group status**: View proxy groups and their selected dialer
- **Node information**: Nodes list with subscription source (subtag)
- **Latency tracking**: Node latency in milliseconds
- **Traffic insights**: DNS and traffic info parsed from logs
- **Log viewer**: Raw log streaming from journalctl

## Requirements

- Python 3.8+
- FastAPI
- Uvicorn
- dae running via systemd (logs from `journalctl -u dae`)

## Installation

```bash
# Install dependencies
pip3 install fastapi uvicorn --break-system-packages

# Create directory
mkdir -p /opt/dae-dashboard/templates

# Copy files
cp app.py /opt/dae-dashboard/
cp templates/index.html /opt/dae-dashboard/templates/

# Optional static directory (only needed if you add static assets)
mkdir -p /opt/dae-dashboard/static

# Create systemd service
cat > /etc/systemd/system/dae-dashboard.service << EOF
[Unit]
Description=DAE Dashboard
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/dae-dashboard
ExecStart=/usr/bin/python3 /opt/dae-dashboard/app.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF

# Enable and start
systemctl daemon-reload
systemctl enable dae-dashboard
systemctl start dae-dashboard
```

## Usage

Access the dashboard at http://your-server:8899/

## API Endpoints

- `GET /` - Dashboard HTML
- `GET /api/status` - Service status
- `GET /api/groups` - Group information with nodes
- `GET /api/nodes` - All unique nodes
- `GET /api/connections` - Connection logs (best-effort parsing)
- `GET /api/dns` - DNS query logs (best-effort parsing)
- `GET /api/traffic` - Traffic summary from logs
- `GET /api/config` - Read dae config
- `PUT /api/config` - Update dae config
- `POST /api/reload` - Reload dae config
- `GET /api/logs` - Raw logs
- `WS /ws` - WebSocket for real-time updates

## Architecture

- **Backend**: FastAPI with journalctl log reading
- **Frontend**: Vanilla JavaScript with flexbox layout
- **Log Source**: systemd journal (no file size issues)

## License

MIT
