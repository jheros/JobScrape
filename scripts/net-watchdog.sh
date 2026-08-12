#!/usr/bin/env bash
# Detects a black-holed default route and recovers it automatically.
#
# THE INCIDENT THIS EXISTS FOR (2026-08-12, t4-vm): Astrill VPN
# (`openvpn-client@astrill.service`) got stuck in a repeating connect ->
# 6-minute-inactivity-timeout -> reconnect loop. The whole time, its
# split-tunnel routes (`0.0.0.0/1` and `128.0.0.0/1` via `tun0`) stayed
# installed and kept capturing every outbound packet, so ALL internet-bound
# traffic was silently dropped for 13+ hours -- only same-subnet LAN traffic
# (the dashboard itself, the local database) kept working, which is why the
# app looked healthy the entire time. `systemctl status` also looked fine:
# the service was "active (running)" throughout, since openvpn considers
# itself healthy mid-reconnect-loop. Only an actual outbound probe caught it.
#
# This script is that probe, run on a timer (see net-watchdog.timer). If a
# real request to the outside world fails AND the astrill service is the one
# active VPN that could be responsible, it restarts that service and checks
# again. It does nothing if astrill isn't running -- a real ISP/upstream
# outage is not something restarting a VPN client fixes, and this script has
# no business guessing at causes it can't actually address.
#
# Deliberately narrow and Astrill-specific rather than a generic "detect any
# VPN and restart it" abstraction: this box runs exactly one VPN client, for
# a reason nothing here needs to know, and building for hypothetical other
# VPNs this server doesn't run would just be untested code with no way to
# verify it actually works.
#
# Install (see also docs/DOCKER.md):
#   sudo cp scripts/net-watchdog.sh /usr/local/sbin/net-watchdog.sh
#   sudo chmod +x /usr/local/sbin/net-watchdog.sh
#   sudo cp scripts/net-watchdog.service scripts/net-watchdog.timer /etc/systemd/system/
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now net-watchdog.timer
#
# Every check and any restart it performs is logged to the systemd journal
# (`journalctl -u net-watchdog` / `-t net-watchdog`), not just silently done.

set -euo pipefail

readonly VPN_SERVICE="openvpn-client@astrill"
readonly PROBE_URL="https://1.1.1.1"
readonly PROBE_TIMEOUT=5
readonly COOLDOWN_FILE="/run/net-watchdog.last-restart"
# Don't restart more than once every 10 minutes -- if the VPN provider's own
# server is the problem, restarting every 5-minute tick just thrashes it
# without helping, and floods the journal with identical entries.
readonly COOLDOWN_SECONDS=600

log() {
    logger -t net-watchdog -- "$*"
}

probe_ok() {
    curl -fsS -m "$PROBE_TIMEOUT" -o /dev/null "$PROBE_URL"
}

if probe_ok; then
    exit 0
fi

log "outbound connectivity probe failed (curl $PROBE_URL)"

if ! systemctl is-active --quiet "$VPN_SERVICE"; then
    log "$VPN_SERVICE is not active -- not a VPN routing issue, nothing this script can do"
    exit 0
fi

if [ -f "$COOLDOWN_FILE" ]; then
    last_restart=$(cat "$COOLDOWN_FILE")
    now=$(date +%s)
    if [ $((now - last_restart)) -lt "$COOLDOWN_SECONDS" ]; then
        log "connectivity still down, but restarted within the last ${COOLDOWN_SECONDS}s -- waiting out the cooldown instead of restart-storming"
        exit 0
    fi
fi

log "restarting $VPN_SERVICE to recover outbound connectivity"
date +%s > "$COOLDOWN_FILE"
systemctl restart "$VPN_SERVICE"

# Give the tunnel a moment to actually establish before re-probing -- the
# original incident's own logs show a real (re)connect takes several seconds,
# not the near-instant fail a broken one produces.
sleep 10

if probe_ok; then
    log "connectivity restored after restarting $VPN_SERVICE"
else
    log "connectivity still down after restarting $VPN_SERVICE -- needs a human"
fi
