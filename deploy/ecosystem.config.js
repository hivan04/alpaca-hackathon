/**
 * PM2 process definitions.
 *
 *   pm2 start deploy/ecosystem.config.js --only oaa-dev      # throwaway account
 *   pm2 start deploy/ecosystem.config.js --only oaa-judged   # THE judged account
 *   pm2 start deploy/ecosystem.config.js --only oaa-dashboard
 *   pm2 logs oaa-judged
 *   pm2 save && pm2 startup            # survive a host reboot
 *
 * A note on where this runs. The whole design turns on 15:15 and 15:54 ET
 * firing on time. A laptop that sleeps misses the cutoff, and that is the exact
 * failure the firewall exists to prevent. Put the judged process on an
 * always-on host (a small us-east VPS is ideal) and use your machine for
 * development only.
 */
const path = require("path");
// This file lives in deploy/, so the repo root is one level up. Every path
// below is anchored there - pm2 resolves out_file/error_file against cwd.
const ROOT = path.join(__dirname, "..");
const PY = path.join(ROOT, ".venv", "bin", "oaa");

const base = {
  cwd: ROOT,
  interpreter: "none",          // `oaa` is already an executable entry point
  autorestart: true,
  max_restarts: 50,
  min_uptime: "60s",
  restart_delay: 10000,
  kill_timeout: 30000,          // let the current cycle finish before SIGKILL
  max_memory_restart: "700M",
  time: true,                   // timestamp every log line
  merge_logs: true,
};

module.exports = {
  apps: [
    // oaa-dev removed 30 Aug - everything runs on the judged profile.
    {
      ...base,
      name: "oaa-judged",
      script: PY,
      args: "run --profile judged",
      env: { OAA_PROFILE: "judged", PYTHONUNBUFFERED: "1", TZ: "America/New_York" },
      out_file: "logs/judged.out.log",
      error_file: "logs/judged.err.log",
      // The judged account is the submission. Restart hard, restart often,
      // but never silently give up on it.
      max_restarts: 200,
      exp_backoff_restart_delay: 5000,
    },
    {
      // The Streamlit operator dashboard - backtesting, live trading, and the
      // Control tab that switches books on and off per account. This is NOT
      // `oaa serve`, which is the public FastAPI page the submission links to.
      ...base,
      name: "oaa-control",
      script: PY,
      args: "dashboard --profile judged --port 8501",
      env: { OAA_PROFILE: "judged", PYTHONUNBUFFERED: "1", TZ: "America/New_York" },
      out_file: "logs/control.out.log",
      error_file: "logs/control.err.log",
    },
    {
      ...base,
      name: "oaa-dashboard",
      script: PY,
      args: "serve --profile judged",
      env: { OAA_PROFILE: "judged", PYTHONUNBUFFERED: "1", TZ: "America/New_York" },
      out_file: "logs/dashboard.out.log",
      error_file: "logs/dashboard.err.log",
    },
  ],
};
