# DAE Dashboard

This repo provides a FastAPI-based dashboard for dae. The app reads dae logs from `journalctl -u dae` and serves a single-page UI from `templates/index.html`.

## Quick commands

- Run locally: `python3 app.py`
- Lint/validate: `python3 -m py_compile app.py`

## Project notes

- Config file path: `/usr/local/etc/dae/config.dae`
- Reload command: `dae reload`
- systemd status: `systemctl show dae --no-page`
- Dashboard port: `8899`

## Deployment expectations

- Install deps: `pip3 install fastapi uvicorn --break-system-packages`
- Service unit: `/etc/systemd/system/dae-dashboard.service`
- Install path: `/opt/dae-dashboard`
