# Temporary Railway BuildingConnected Login Bootstrap

This mode is temporary and admin-only. It exposes an interactive Chromium browser
over the Railway public URL through noVNC so a human can complete
BuildingConnected/Autodesk login and MFA. Use it only long enough to seed the
Railway volume-backed browser profile, then restore the normal Flask start
command.

Do not run the daily workflow while bootstrap mode is enabled.

## Persistent Profile

Bootstrap mode launches Chromium with this profile directory:

```text
/app/data/playwright_bc_profile/chromium
```

That path is on the Railway volume when `CQE_DATA_ROOT=/app/data` or
`RAILWAY_VOLUME_MOUNT_PATH=/app/data` is set.

## Switch Into Bootstrap Mode

Set the Railway service start command to:

```sh
bash scripts/run_bc_login_bootstrap.sh
```

Recommended temporary Railway variable:

```text
BC_BOOTSTRAP_VNC_PASSWORD=<temporary strong password>
```

If `BC_BOOTSTRAP_VNC_PASSWORD` is not set, the VNC session has no VNC password.
Only use that briefly and only when you understand the exposure.

After Railway redeploys or restarts with the bootstrap start command, open the
Railway public URL. The root URL redirects to noVNC. Complete the
BuildingConnected/Autodesk login and MFA in Chromium.

The script does not run the daily workflow, process projects, download files, or
write Google Sheets. It only starts Xvfb, fluxbox, x11vnc, noVNC/websockify, and
Chromium.

## Restore Normal Production Mode

After login is complete, restore the Railway service start command to:

```sh
xvfb-run -a gunicorn -w 1 -b 0.0.0.0:${PORT:-8080} app_web:app
```

Then redeploy or restart the Railway service. Keep Gunicorn at one worker because
the app uses SQLite on the mounted Railway volume.

Remove `BC_BOOTSTRAP_VNC_PASSWORD` after bootstrap mode is no longer needed.
