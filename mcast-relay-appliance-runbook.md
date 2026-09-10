# Multicast Relay Appliance — Plan B Build Runbook

Dedicated physical appliance forwarding Bogen Nyquist C4000 zone audio between
two paging VLANs that live on separate L3 segments. Requires **nothing** from
the customer's routing infrastructure — no PIM, no topology disclosure, no core
config change. Two access ports and two static IPs is the entire ask.

**Scope:** delivering the multicast stream onto the receiving VLAN. Endpoint
behaviour past that point is the facility's responsibility.

**Supersedes:** the LXC runbook. Same relay, dedicated hardware, plus the
supervision and interface-pinning that a standalone appliance needs.

---

## 1. What to ask the customer for

Phrase the request so it's answerable without revealing topology:

> Two switch access ports, one in each paging VLAN, at whichever location is
> easiest for you. A static IP in each VLAN. A DHCP reservation (or static) on
> the Nyquist server so its address does not change.

Notes:

- **You may only need one location.** If the remote paging VLAN is already
  trunked to the main building, both access ports can come off the same switch.
  Only if that VLAN genuinely terminates at the remote site do you need
  physical presence there — then it's a fiber pair plus media converters or an
  SFP NIC.
- **Static, not DHCP,** on the relay. The config is keyed to addresses; a lease
  change is a silent outage.
- **The Nyquist server address is the hard dependency.** It is compiled into
  `SOURCE`. Get the reservation in writing.

### Disclose what the box does

This device has interfaces in two segments the customer deliberately separated.
Do not install it quietly. Hand them Appendix A — what forwards, what doesn't,
and the ruleset verbatim. An undocumented L3 bridge found during an audit is a
much worse outcome than a rejected change request, and a refusal tells you
something useful about the constraint you're designing against.

---

## 2. Hardware

| | |
|---|---|
| Platform | Small fanless x86, 2× Intel NIC |
| CPU / RAM | Anything current; 2 GB is generous |
| Storage | SSD, no spinning disk |
| Throughput | ~81 kbps per active zone — not a factor |
| OS | Debian 12 minimal, no desktop |

Pick for reliability and no moving parts. This sits in a closet for years.

If you virtualize it instead, use the LXC runbook and note the one extra step:
disable bridge-level IGMP snooping on the host (`/sys/class/net/<bridge>/bridge/
multicast_snooping` = 0), or the hypervisor prunes the stream before the guest
sees it.

---

## 3. Base OS install

Debian 12 netinst, standard system utilities only. No desktop, no print server.

```bash
apt-get update
apt-get install -y --no-install-recommends \
    python3-minimal nftables tcpdump iproute2 chrony
systemctl disable --now avahi-daemon 2>/dev/null || true
```

`chrony` matters — log timestamps are how you'll correlate a silent failure
against a page that didn't land.

`python3-minimal` is sufficient. The relay uses only `socket` and `struct`.

---

## 4. Pin the interface names

**Do this before anything else.** On bare metal with two identical NICs, kernel
enumeration order can swap `eth0`/`eth1` across a reboot or a firmware update.
The relay binds VIFs by IP so it survives a swap, but your firewall rules,
diagnostics, and everyone else's sanity do not.

Get the MACs:

```bash
ip -br link
```

Create `/etc/systemd/network/10-src.link`:

```ini
[Match]
MACAddress=aa:bb:cc:dd:ee:01

[Link]
Name=src501
```

And `/etc/systemd/network/11-dst.link`:

```ini
[Match]
MACAddress=aa:bb:cc:dd:ee:02

[Link]
Name=dst500
```

```bash
update-initramfs -u
reboot
ip -br a          # confirm src501 / dst500
```

Names now describe function. `src501` faces the Nyquist server, `dst500` faces
the receivers.

---

## 5. Addressing

`/etc/network/interfaces`:

```
auto lo
iface lo inet loopback

# source side — Nyquist server VLAN
auto src501
iface src501 inet static
    address 172.18.207.253
    netmask 255.255.240.0

# receiver side — remote paging VLAN
auto dst500
iface dst500 inet static
    address 172.19.215.253
    netmask 255.255.252.0
    gateway 172.19.215.254
```

- Substitute the addresses the customer assigns.
- **Exactly one default gateway**, on whichever side you'll manage the box from.
  Two default routes make reachability nondeterministic.
- Do not reuse the SVI/gateway address of either VLAN.

```bash
systemctl restart networking
ip -br a && ip route
```

---

## 6. Kernel settings

```bash
cat > /etc/sysctl.d/99-mcast-relay.conf <<'EOF'
net.ipv4.ip_forward = 1
net.ipv4.conf.all.rp_filter = 0
net.ipv4.conf.default.rp_filter = 0
net.ipv4.conf.all.force_igmp_version = 2
net.ipv4.igmp_max_memberships = 100
EOF
sysctl --system
```

Verify only the keys you care about — `sysctl --system` replays every file on
disk and will throw unrelated permission errors on some systems:

```bash
sysctl net.ipv4.ip_forward net.ipv4.conf.all.rp_filter \
       net.ipv4.conf.all.force_igmp_version net.ipv4.igmp_max_memberships
```

Why each one:

- `ip_forward` — required for kernel multicast forwarding.
- `rp_filter=0` — forwarding here is asymmetric; strict RPF silently drops the
  stream.
- `force_igmp_version=2` — matches what Nyquist stations emit.
- `igmp_max_memberships=100` — the default is **20**, and the relay joins 39
  groups. Hitting the ceiling produces `OSError: [Errno 105] No buffer space
  available`. The script also batches joins across sockets so it stays portable
  to sites where you don't own this sysctl.

---

## 7. Lock unicast shut

`ip_forward=1` turns this into a general router between two segments the
customer separated on purpose. Close it in the same change window.

```bash
cat > /etc/nftables.conf <<'EOF'
#!/usr/sbin/nft -f
flush ruleset

table inet relay {
    chain forward {
        type filter hook forward priority 0; policy drop;
        ip daddr 239.0.0.0/8 accept
    }
}
EOF

systemctl enable nftables
systemctl restart nftables
nft list ruleset
```

**`restart`, not `enable --now`.** If the service is already running, `--now`
is a no-op and you're left with Debian's default skeleton (`table inet filter`,
all chains `policy accept`). Confirm the output says `table inet relay` — if it
says `table inet filter`, your ruleset never loaded.

### Verifying it actually blocks

Pinging from the relay itself proves nothing — that's OUTPUT, not FORWARD, and
the relay has an interface in both subnets anyway.

Test from **a third host** on the source VLAN, targeting something on the
receiver VLAN. Before the ruleset loads it succeeds via the relay; after, it
fails. That's the demonstration for their security team.

---

## 8. Install the relay

```bash
mkdir -p /opt/mcast-relay
cat > /opt/mcast-relay/mcast-relay.py <<'PYEOF'
#!/usr/bin/env python3
"""Static multicast relay via the Linux kernel forwarding cache.
Preserves original source IP. Forwards all ports within each group."""
import socket, struct, signal, sys, time

IN_IF  = "172.18.207.253"    # src501 - Nyquist server side
OUT_IF = "172.19.215.253"    # dst500 - receiver side
SOURCE = "172.18.192.210"    # Nyquist C4000 server

GROUPS = [f"239.1.1.{n}" for n in range(1, 40)]
JOINS_PER_SOCKET = 15

IPPROTO_IP = 0
MRT_INIT, MRT_DONE, MRT_ADD_VIF, MRT_ADD_MFC, MRT_DEL_MFC = 200, 201, 202, 204, 205
MAXVIFS = 32
VIFCTL = "=HBBI4s4s"
MFCCTL = "=4s4sH32s2xIIIi"

joiners = []


def add_vif(s, vifi, addr):
    s.setsockopt(IPPROTO_IP, MRT_ADD_VIF, struct.pack(
        VIFCTL, vifi, 0, 1, 0, socket.inet_aton(addr), b"\x00" * 4))


def mfc(s, opt, src, grp, in_vif, out_vif=None):
    ttls = bytearray(MAXVIFS)
    if out_vif is not None:
        ttls[out_vif] = 1
    s.setsockopt(IPPROTO_IP, opt, struct.pack(
        MFCCTL, socket.inet_aton(src), socket.inet_aton(grp),
        in_vif, bytes(ttls), 0, 0, 0, 0))


def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_IGMP)
    s.setsockopt(IPPROTO_IP, MRT_INIT, 1)
    add_vif(s, 0, IN_IF)
    add_vif(s, 1, OUT_IF)
    print(f"vif0={IN_IF} vif1={OUT_IF}", flush=True)

    j = None
    for i, grp in enumerate(GROUPS):
        if i % JOINS_PER_SOCKET == 0:
            j = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            joiners.append(j)
        j.setsockopt(IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     socket.inet_aton(grp) + socket.inet_aton(IN_IF))
        mfc(s, MRT_ADD_MFC, SOURCE, grp, 0, 1)

    print(f"forwarding {len(GROUPS)} groups from {SOURCE} across "
          f"{len(joiners)} sockets: src => dst", flush=True)

    def bye(*_):
        for grp in GROUPS:
            try:
                mfc(s, MRT_DEL_MFC, SOURCE, grp, 0)
            except OSError:
                pass
        s.setsockopt(IPPROTO_IP, MRT_DONE, 1)
        s.close()
        print("torn down", flush=True)
        sys.exit(0)

    signal.signal(signal.SIGINT, bye)
    signal.signal(signal.SIGTERM, bye)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
PYEOF
chmod 755 /opt/mcast-relay/mcast-relay.py
python3 -c "import ast; ast.parse(open('/opt/mcast-relay/mcast-relay.py').read()); print('syntax ok')"
```

**Always run the `ast.parse` check.** A bad paste produces a syntax error that
systemd hides inside a restart loop, and `systemctl status` will cheerfully
report `active (running)` on the way past.

### Design notes

- The MFC keys on **(source, group)** only. Every UDP port in a group crosses
  automatically, which is why RTCP comes along free and why the ephemeral
  source port changing between pages doesn't matter.
- Nyquist streams on the zone's configured base port **+2** for audio and **+3**
  for RTCP. Ports are irrelevant to this relay, but you need this to interpret
  a capture correctly.
- `joiners` is module scope deliberately. If those sockets are garbage
  collected the kernel drops the memberships.
- VIFs bind by **IP**, not interface name — the relay survives an interface
  name swap.

---

## 9. Service unit

`/etc/systemd/system/mcast-relay.service`:

```ini
[Unit]
Description=Nyquist multicast paging relay
Documentation=file:/opt/mcast-relay/README.md
After=network-online.target nftables.service
Wants=network-online.target
Requires=nftables.service

[Service]
Type=simple
ExecStartPre=/bin/sh -c 'ip -br a show src501 | grep -q UP && ip -br a show dst500 | grep -q UP'
ExecStart=/usr/bin/python3 /opt/mcast-relay/mcast-relay.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
```

`Requires=nftables.service` is deliberate: if the ruleset fails to load, the
relay must not start with unicast forwarding wide open.

```bash
systemctl daemon-reload
systemctl enable --now mcast-relay
sleep 2
systemctl status mcast-relay --no-pager
journalctl -u mcast-relay -n 20 --no-pager
```

Expect `vif0=... vif1=...` and `forwarding 39 groups from ... across 3 sockets`.

---

## 10. Verify

Trigger a page to a zone the remote site should hear, then:

```bash
# two VIFs; PktsIn climbing on vif 0, PktsOut on vif 1
cat /proc/net/ip_mr_vif

# active (S,G) entries
cat /proc/net/ip_mr_cache

# arriving on the source side
tcpdump -i src501 -nnv -c 5 host 239.1.1.32

# leaving on the receiver side
tcpdump -i dst500 -nnv -c 5 host 239.1.1.32
```

**The TTL decrement is the only real proof.** 16 arriving on `src501`, **15**
leaving on `dst500`. Identical TTL on both sides means you're looking at a
flooded copy and the relay isn't in the path at all.

Then confirm audio at a remote endpoint, and confirm unicast is still blocked
using the third-host test from section 7.

---

## 11. Supervision

Both remaining failure modes are silent. This section is not optional on a
dedicated appliance — you control the whole box, so there's no excuse for not
watching it.

`/opt/mcast-relay/check-relay.sh`:

```bash
#!/bin/bash
# exit 0 healthy, 2 stale, 3 not running
STATE=/var/lib/mcast-relay/last
mkdir -p "$(dirname "$STATE")"

systemctl is-active --quiet mcast-relay || {
    logger -t mcast-relay "CRITICAL: service not running"; exit 3; }

NOW=$(awk 'NR>1 && $1==1 {print $5}' /proc/net/ip_mr_vif)
[ -z "$NOW" ] && { logger -t mcast-relay "CRITICAL: no vif1"; exit 3; }

PREV=$(cat "$STATE" 2>/dev/null || echo 0)
echo "$NOW" > "$STATE"

if [ "$NOW" -le "$PREV" ]; then
    logger -t mcast-relay "WARNING: no packets forwarded since last check ($NOW)"
    exit 2
fi
logger -t mcast-relay "OK: PktsOut=$NOW"
```

```bash
chmod 755 /opt/mcast-relay/check-relay.sh
```

Run it on a cadence longer than the gap between known pages:

```bash
cat > /etc/systemd/system/mcast-relay-check.timer <<'EOF'
[Unit]
Description=Multicast relay health check

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat > /etc/systemd/system/mcast-relay-check.service <<'EOF'
[Unit]
Description=Multicast relay health check
[Service]
Type=oneshot
ExecStart=/opt/mcast-relay/check-relay.sh
EOF

systemctl daemon-reload
systemctl enable --now mcast-relay-check.timer
```

Point syslog at your monitoring platform. A counter that hasn't advanced across
a window that should have contained a page means the path is dead.

**Pair this with a scheduled daily tone** to a zone only the remote site hears.
Without guaranteed traffic there is nothing to measure, and "no packets" is
ambiguous between a broken relay and a quiet afternoon. A 2 AM one-second tone
to a remote-only zone turns the check from heuristic into a real test.

---

## 12. Handoff — known limitations

Put all of this in the as-built. It is the difference between a documented
design and someone else's inherited surprise.

1. **Static (S,G).** Keyed to the Nyquist server IP. If that address changes,
   forwarding stops silently.
2. **Fixed group range,** 239.1.1.1–39. Zones created outside that block do not
   cross. Adding a zone in the Nyquist UI does **not** update the relay.
3. **Single point of failure.** No HA. Two relays would double-deliver every
   page — active/standby needs real design work, not a second box.
4. **Not a vendor-supported configuration.** If overhead paging at this facility
   carries emergency notification, the defensible long-term answers are PIM on
   the customer's L3 switching, or a second Nyquist server at the remote site.
   This appliance is the answer when neither is available.
5. **Firmware/OS updates.** Nothing here depends on kernel internals beyond the
   stable `MRT_*` socket API, but re-run section 10 after any major upgrade.

---

## Appendix A — change request text

> **Purpose:** Deliver Nyquist overhead paging audio from the paging VLAN in
> Building A to the paging VLAN in Building B. The paging system uses IP
> multicast; multicast does not cross between IP segments without a forwarder.
>
> **Device:** Dedicated single-purpose appliance, Debian Linux, two network
> interfaces. One access port in each paging VLAN. One static IP per VLAN. No
> other network access required.
>
> **What it forwards:** IP multicast destined to 239.0.0.0/8 only.
>
> **What it does not forward:** All other traffic between the two segments is
> dropped by an explicit default-deny firewall policy. Unicast traffic cannot
> traverse this device.
>
> ```
> table inet relay {
>     chain forward {
>         type filter hook forward priority 0; policy drop;
>         ip daddr 239.0.0.0/8 accept
>     }
> }
> ```
>
> **Traffic volume:** Approximately 81 kbps per active paging zone, only while
> a page or scheduled tone is in progress.
>
> **Alternative:** If the customer prefers, this can be replaced by enabling
> PIM sparse-mode on the paging VLAN interfaces of the existing Layer 3
> switching, with no additional hardware. That approach requires a routing
> configuration change on the customer's core.

---

## Quick reference

| Item | Value |
|---|---|
| `src501` | 172.18.207.253/20 — Nyquist server VLAN |
| `dst500` | 172.19.215.253/22 — receiver VLAN |
| Nyquist source | 172.18.192.210 |
| Groups | 239.1.1.1 – 239.1.1.39 |
| Zone ports | configured base +2 = audio, +3 = RTCP |
| Service | `mcast-relay.service` |
| Health check | `mcast-relay-check.timer`, hourly |
| Logs | `journalctl -u mcast-relay -f` |
| Win condition | TTL 16 in on src, TTL 15 out on dst |
