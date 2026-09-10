#!/bin/bash
# Multicast relay health check.
# exit 0 = healthy, 2 = stale (no packets forwarded since last check), 3 = broken.
#
# Reads the PktsOut counter of vif1 (the receiver-side VIF) from
# /proc/net/ip_mr_vif. The relay always creates vif0 = IN_IF, vif1 = OUT_IF.
#
# Columns in /proc/net/ip_mr_vif (kernel net/ipv4/ipmr.c):
#   $1 Idx  $2 Interface  $3 BytesIn  $4 PktsIn  $5 BytesOut  $6 PktsOut ...

STATE=/var/lib/mcast-relay/last
mkdir -p "$(dirname "$STATE")"

systemctl is-active --quiet mcast-relay || {
    logger -t mcast-relay "CRITICAL: service not running"; exit 3; }

NOW=$(awk 'NR>1 && $1==1 {print $6}' /proc/net/ip_mr_vif)
[ -z "$NOW" ] && { logger -t mcast-relay "CRITICAL: no vif1"; exit 3; }

PREV=$(cat "$STATE" 2>/dev/null || echo 0)
echo "$NOW" > "$STATE"

if [ "$NOW" -le "$PREV" ]; then
    logger -t mcast-relay "WARNING: no packets forwarded since last check (PktsOut=$NOW)"
    exit 2
fi
logger -t mcast-relay "OK: PktsOut=$NOW"
