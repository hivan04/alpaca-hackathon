# Deployment

## Where this should run

**Not on your laptop.** The design turns on 15:15 and 15:45 ET firing on time. A
machine that sleeps, drops wifi, or gets closed at 20:00 BST (= 15:00 ET) misses
the cutoff — and that is precisely the failure the firewall exists to prevent.

A small always-on host in `us-east` costs a few pounds a month and removes an
entire class of problem: no sleep, no timezone surprises, lower latency to
Alpaca. Use your own machine for development.

Whatever the host, **pin the timezone to `America/New_York`**. Every firewall
boundary is an ET time. A container defaulting to UTC will fire the 15:15 cutoff
at 11:15 ET, which is worse than not running at all.

---

## Option 1 — PM2

Good if you already have Node on the box. Survives reboots, restarts on crash,
tails logs nicely.

```bash
npm install -g pm2

make setup && make doctor            # confirm it works before daemonising

pm2 start ecosystem.config.js --only oaa-dev        # throwaway account
pm2 start ecosystem.config.js --only oaa-judged     # THE judged account
pm2 start ecosystem.config.js --only oaa-dashboard  # the public URL

pm2 save && pm2 startup              # survive a host reboot
pm2 logs oaa-judged
pm2 monit
```

Or through the Makefile:

```bash
make pm2-dev        make pm2-judged     # prompts for confirmation
make pm2-status     make pm2-logs       make pm2-stop
```

Notes on the config:

- `kill_timeout: 30000` — PM2 waits 30s after SIGTERM before SIGKILL. The runner
  catches SIGTERM and finishes the current cycle, so a restart never lands
  mid-liquidation.
- `min_uptime: 60s` with `max_restarts` — a process that crashes instantly in a
  loop gets marked errored rather than hammering Alpaca.
- `TZ: America/New_York` on every app.

**`pm2 stop` does not close positions.** Run `oaa flatten` first if that is what
you meant.

---

## Option 2 — systemd

Fewer moving parts than PM2 and the natural choice on a Linux VPS.

```bash
sudo useradd -r -s /usr/sbin/nologin oaa
sudo cp -r . /opt/alpaca-hackathon && sudo chown -R oaa:oaa /opt/alpaca-hackathon
sudo cp deploy/oaa.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now oaa
journalctl -u oaa -f
```

The unit pins `TZ`, restarts always, allows 45s for a graceful stop, and is
hardened (`ProtectSystem=strict`, writable paths limited to `runs/`, `data/`
and `logs/`).

---

## Option 3 — Docker

```bash
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml logs -f agent
```

Two containers — the loop and the dashboard — sharing `runs/` so the dashboard
reads what the agent writes. `uv` is installed in the image so the Alpaca MCP
server can be launched from inside it.

---

## Developing in VS Code

`.vscode/launch.json` ships debug targets for each entry point, so you can put a
breakpoint in the risk engine and step through a real decision:

| Target | What it does |
|---|---|
| doctor | dependency and credential check |
| firewall (now) / (simulate 15:15) | inspect the phase machine |
| scan --cycle carry_scan | the four premium gates across the universe |
| backtest: overnight | full walk-forward, writes a CSV |
| agent: overnight_signal | the assistant reasoning over MCP |
| scan (dry) | one intraday cycle, no orders |
| run: one pass | the scheduler, `--once` |
| pytest: current file | debug the test under the cursor |

`.vscode/tasks.json` wires the Makefile targets to the command palette
(`setup`, `doctor`, `test`, `lint`, `backtest overnight`, `pm2: start dev`).

Point the interpreter at `.venv/bin/python` — `settings.json` already does, and
adds `src/` to the analysis path so imports resolve.

---

## Pre-flight

Run this before leaving it unattended on the judged account:

```bash
oaa doctor --profile judged      # deps, credentials, options level, connection
./scripts/verify_accounts.sh     # dev and judged are genuinely different
oaa firewall                     # boundaries look right, clock is ET
oaa strategies                   # the enabled books and their gates
oaa scan --profile dev           # behaviour is sane on the throwaway account
date                             # host clock and TZ
```

Then start it, and watch the first full day before trusting it overnight.

## Monitoring

```bash
oaa firewall                     # phase, lock owner, budget
oaa journal --limit 30           # decisions, including declined ones
oaa report                       # equity curve + HTML
pm2 logs oaa-judged --lines 200  # or: journalctl -u oaa -f
```

The dashboard at `:8080` is read-only by design — nobody should be able to make
the account trade from a browser. That is the URL to put in the submission's
"Application URL" field.

## If something goes wrong

| Situation | Do this |
|---|---|
| Stop opening new positions | `OAA_EXECUTION__DRY_RUN=true` then restart the process |
| Stop everything, keep positions | `pm2 stop oaa-judged` (finishes the current cycle) |
| Close everything now | `oaa flatten --profile judged --yes` |
| The 15:15 cutoff failed | `oaa journal` for the `firewall_cutoff` event, then flatten the transient legs manually. The 15:45 verification will have disabled the transient books for tomorrow — that part worked |
| Host rebooted mid-session | The runner fires late cycles on startup, so a restart at 15:20 still runs the 15:15 cutoff |
