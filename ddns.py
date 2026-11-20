#!/usr/bin/env python3
"""hostcreators.sk DDNS updater."""

import ipaddress
import json
import logging
import os
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

DEFAULT_ENV = "/etc/ddns/ddns.env"
log = logging.getLogger("ddns")


def parse_env(path):
    env = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        env[k.strip()] = v
    return env


def build_config(env):
    base = env.get("API_BASE", "https://www.hostcreators.sk/api/v1/host/domain").rstrip("/")
    cookie_name = env.get("COOKIE_NAME")
    if not cookie_name:
        sys.exit("COOKIE_NAME missing from env")
    state_file = Path(env.get("STATE_FILE", "/var/lib/ddns/state.json"))
    client_name = env.get("CLIENT_NAME") or socket.gethostname()

    cookies = {
        k[7:]: v
        for k, v in env.items()
        if k.startswith("COOKIE_") and k != "COOKIE_NAME"
    }
    if not cookies:
        sys.exit("No COOKIE_* entries")

    records = []
    for key, val in env.items():
        if not key.startswith("RECORD_"):
            continue
        if "|" not in val:
            sys.exit(f"{key}: expected COOKIE|HOST|RECORD|NAME, got scalar {val!r}")
        parts = [p.strip() for p in val.split("|")]
        if len(parts) < 4:
            sys.exit(f"{key}: need COOKIE|HOST|RECORD|NAME, got {val!r}")
        ref, host_id, rec_id, name = parts[:4]
        ttl = int(parts[4]) if len(parts) > 4 and parts[4] else 1800
        rtype = parts[5] if len(parts) > 5 and parts[5] else "A"
        label = parts[6] if len(parts) > 6 and parts[6] else name
        if ref not in cookies:
            sys.exit(f"{key}: unknown cookie ref {ref!r}")
        if not cookies[ref]:
            sys.exit(f"{key}: cookie {ref!r} is empty")
        records.append({
            "key": f"{host_id}/{rec_id}",
            "url": f"{base}/{host_id}/dns/record/{rec_id}",
            "id": int(rec_id),
            "name": name,
            "ttl": ttl,
            "type": rtype,
            "label": label,
            "cookie": f"{cookie_name}={cookies[ref]}",
        })
    if not records:
        sys.exit("No RECORD_* entries")

    return {
        "records": records,
        "headers": {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:160.0) Gecko/20100101 Firefox/160.0",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://www.hostcreators.sk",
            "Referer": "https://www.hostcreators.sk/",
        },
        "state_file": state_file,
        "client_name": client_name,
    }


def get_public_ip():
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
        except requests.RequestException as e:
            log.warning("IP lookup failed (%s): %s", url, e)
            continue
        candidate = r.text.strip()
        try:
            ip = ipaddress.ip_address(candidate)
        except ValueError:
            log.warning("non-IP response from %s: %r", url, candidate)
            continue
        if ip.version != 4:
            log.warning("non-IPv4 from %s (%s); skipping", url, ip)
            continue
        return str(ip)
    return None


def load_state(path):
    try:
        raw = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    # Migrate old-format flat-string entries just in case.
    return {k: v if isinstance(v, dict) else {"ip": v} for k, v in raw.items()}


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


def build_payload(rec, ip, client):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    name = rec["name"]
    if name.startswith("*."):
        # hostcreators.sk encodes wildcards in rec_host, not in name.
        name = name[2:]
        rec_host = "*"
    else:
        rec_host = ""
    return {
        "data": {
            "id": rec["id"],
            "name": name,
            "ttl": rec["ttl"],
            "disabled": 0,
            "ordername": "",
            "auth": 1,
            "comment": f"ddns: {client} @ {stamp}",
            "rec_type": rec["type"],
            "rec_host": rec_host,
            "rec_data": ip,
            "rec_mx_priority": 0,
            "rec_data_srv": None,
            "rec_data_ds": None,
            "rec_data_dmarc": None,
        }
    }


def update_record(rec, ip, headers, client):
    h = {**headers, "Cookie": rec["cookie"]}
    r = requests.patch(rec["url"], headers=h, json=build_payload(rec, ip, client), timeout=30)
    r.raise_for_status()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = build_config(parse_env(os.environ.get("DDNS_ENV", DEFAULT_ENV)))

    # Preflight: verify state file is writable BEFORE any PATCH. Without this,
    # a non-writable state file would cause the script to re-PATCH on every
    # run (state never persists), i.e. a rapid-patch loop.
    state = load_state(cfg["state_file"])
    try:
        save_state(cfg["state_file"], state)
    except OSError as e:
        log.error("state file %s not writable: %s", cfg["state_file"], e)
        sys.exit(1)

    ip = get_public_ip()
    if not ip:
        log.error("could not determine public IPv4")
        sys.exit(1)
    log.info("public IP: %s", ip)

    now = int(time.time())
    failed = False

    for rec in cfg["records"]:
        entry = state.get(rec["key"], {})
        last_ip = entry.get("ip")

        if last_ip == ip:
            log.info("[%s] no change", rec["label"])
            continue

        log.info("[%s] %s -> %s", rec["label"], last_ip or "(new)", ip)
        try:
            update_record(rec, ip, cfg["headers"], cfg["client_name"])
        except requests.HTTPError as e:
            failed = True
            status = e.response.status_code if e.response is not None else "?"
            if status == 401:
                log.error("[%s] 401 - cookie invalid or expired", rec["label"])
            else:
                log.error("[%s] HTTP %s: %s", rec["label"], status, e)
            continue
        except Exception as e:
            failed = True
            log.error("[%s] unexpected error: %s", rec["label"], e)
            continue

        state[rec["key"]] = {"ip": ip, "ts": now}
        try:
            save_state(cfg["state_file"], state)
        except OSError as e:
            # PATCH succeeded but state couldn't be persisted. Next run will
            # PATCH again - log loudly so the operator notices.
            failed = True
            log.error("[%s] PATCH ok but state save failed: %s (next run WILL re-PATCH)",
                      rec["label"], e)
            continue
        log.info("[%s] updated", rec["label"])

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()