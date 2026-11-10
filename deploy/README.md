# Deploying and updating the live server

The VM runs whatever is checked out in `~/policyreplay`. GitHub is the source of truth; the VM is updated by `git pull`.

## Update the live site

| What changed | On the VM |
|---|---|
| `dashboard/index.html` only | `git pull`, then hard-refresh the browser. **No restart needed** (the file is read on every request). |
| Python code under `orchestrator/` | `git pull`, then `sudo systemctl restart policyreplay` |
| `target_app/` or `worker/` | `git pull`, `python orchestrator/sandbox_run.py build`, then restart |
| `deploy/policyreplay.service` | `git pull`, copy it into place, then reload and restart (below) |

## Change the daily model-call cap

1. Edit the `Environment=PR_DAILY_MODEL_CALLS=...` line in `deploy/policyreplay.service`, commit and push.
2. On the VM:
   ```bash
   cd ~/policyreplay && git pull
   diff deploy/policyreplay.service /etc/systemd/system/policyreplay.service   # review what will change
   sudo cp deploy/policyreplay.service /etc/systemd/system/policyreplay.service
   sudo systemctl daemon-reload && sudo systemctl restart policyreplay
   ```
3. Confirm the **running process** uses the new value (prints only `PR_` settings, never the key):
   ```bash
   tr '\0' '\n' < /proc/$(systemctl show -p MainPID --value policyreplay)/environ | grep '^PR_'
   ```

Notes: the counter lives in memory, so a restart resets it (it also resets at midnight UTC). Values in
`/etc/policyreplay.env` override `Environment=` lines in the unit, so keep the cap in one place only
(`grep DAILY /etc/policyreplay.env` should print nothing).

## Settings

| Variable | Meaning |
|---|---|
| `VULTR_INFERENCE_KEY`, `PR_TEXT_MODEL` | model access (in `/etc/policyreplay.env` only) |
| `PR_RUN_MODE` | `docker` = hardened sandbox (live); default `local` = development only |
| `PR_DAILY_MODEL_CALLS` | daily cap on model calls; default 100 |
| `PR_TRUST_PROXY` | `1` behind Caddy, so per-IP rate limits use the real client address |
| `PR_JSON_MODE`, `PR_DEBUG` | optional model-client switches |

Per-IP limits (20 analyses and 10 runs per hour) and the one-concurrent-run rule are set in code (`orchestrator/main.py`).

## Roll back

`git checkout <previous commit or tag>` on the VM, then restart (and rebuild images if `target_app/` or `worker/` changed).
