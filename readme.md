# hostcreators-ddns

A tiny DDNS updater for [hostcreators.sk](https://www.hostcreators.sk). Pulls your
current public IP, compares it to what was last pushed, and PATCHes one or more
DNS A records if it changed.

No official API is used — it authenticates against the web UI's private JSON
endpoint with your `remember_web` session cookie. Reads its config from a `.env`
file so one script handles multiple users, hosts, and records.

---

## How it works

- The provider has **no GET endpoint** for individual DNS records — only PATCH.
  So the script can't read the current record value back.
- Instead it keeps a **local state file** mapping each record ID to the last IP
  it successfully pushed.
- Each run: look up public IP → compare to state → PATCH only if different.
- Only successful PATCHes update the state, so a failure is retried next run.
- The record's `comment` field is set to `ddns: <client> @ <timestamp>` on
  every change — a cheap audit trail.

---

## Requirements

- Python 3.8+
- `requests` (`pip install requests`)

---

## Getting your cookie

1. Log in to hostcreators.sk in a browser.
2. Open DevTools → **Application** → **Cookies** → `https://www.hostcreators.sk`.
3. Find the cookie named `remember_web_...` (the suffix is a Laravel app hash).
4. Copy **both** the name and the value.

The cookie currently expires about 13 months out. When it does, you'll start
seeing `401` errors in the log — just grab a fresh one and edit the env file.

---

## Config file

Copy `.env.example` to `/etc/ddns/ddns.env` (mode `600`, owned by the user that runs the script):

**`RECORD_*` line format**

| field     | required | notes |
|-----------|----------|-------|
| `COOKIE`  | yes      | name of a `COOKIE_<name>` env var |
| `HOST_ID` | yes      | from the UI URL: `/admin/host/domain/<HOST_ID>/...` |
| `RECORD_ID` | yes    | from the record edit URL: `/dns/record/<ID>/edit` |
| `NAME`    | yes      | the record's DNS name, e.g. `jrosh.eu` or `*.jrosh.eu` |
| `TTL`     | no       | defaults to `1800` |
| `TYPE`    | no       | defaults to `A` |
| `LABEL`   | no       | only used in log lines; defaults to `NAME` |

To get the record ID for the env file, open the record in the UI and check the URL:

```
.../admin/host/domain/<HOST_ID>/dns/record/<RECORD_ID>/edit
```

**Multiple users:** add more `COOKIE_<name>=...` lines and reference them by
that name in the record lines.

⚠️ **Don't add env vars starting with `RECORD_` or `COOKIE_`** unless they're
actually a record or a cookie — the parser treats every such key as one.

---

## Running manually

```bash
DDNS_ENV=/etc/ddns/ddns.env python3 ddns.py
```

Exit code is `0` on success (including no-ops), `1` if any record failed to
update. Suitable for cron and systemd `OnFailure=` hooks.

---

## Install as a systemd timer (recommended)

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin ddns
sudo install -d -o ddns -g ddns /etc/ddns
sudo install -m 600 -o ddns -g ddns /dev/null /etc/ddns/ddns.env
sudo cp .env.example /etc/ddns/ddns.env

sudo install -m 755 ddns.py /usr/local/bin/ddns.py
```

`/etc/systemd/system/ddns.service`:

```ini
[Unit]
Description=hostcreators.sk DDNS updater
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
Environment=DDNS_ENV=/etc/ddns/ddns.env
ExecStart=/usr/bin/python3 /usr/local/bin/ddns.py
User=ddns
Group=ddns
StateDirectory=ddns
```

`/etc/systemd/system/ddns.timer`:

```ini
[Unit]
Description=Run DDNS updater every 5 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min
Unit=ddns.service

[Install]
WantedBy=timers.target
```

Enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ddns.timer
```

`StateDirectory=ddns` creates `/var/lib/ddns` owned by `ddns:ddns`, which is
where the state file lives by default.

---

## Install as a cron job

```cron
*/5 * * * * DDNS_ENV=$HOME/.ddns.env /usr/bin/python3 $HOME/bin/ddns.py >> $HOME/ddns.log 2>&1
```


---