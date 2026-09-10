# Multicast Relay

A single-purpose Linux appliance that forwards IP multicast between two
network segments that are deliberately separated at Layer 3 — without PIM,
without touching the customer's routing core, and without forwarding any
unicast between them.

Built for Bogen Nyquist C4000 zone paging audio (multicast to
`239.1.1.x`), but the groups, source, and port policy are all configured —
it will relay any `(source, group)` set you point it at.

**How it works:** the relay opens the kernel's multicast routing API
(`MRT_*` socket options), registers one virtual interface per side, and
installs static `(S,G)` forwarding-cache entries. The kernel forwards the
packets — original source IP preserved, TTL decremented (that decrement is
how you prove the relay is in the path). A default-deny nftables chain
permits only the configured multicast to cross; all unicast is dropped.

## Repo layout

| File | Purpose |
|---|---|
| `mcast-relay.py` | The relay (stdlib only — `socket`, `struct`) |
| `setup.sh` | Guided post-install setup wizard (`mcast-relay-setup`) |
| `install.sh` | One-time installer for the appliance (root) |
| `relay.conf.example` | Reference for the single config file |
| `check-relay.sh` | Health check (PktsOut liveness) |
| `systemd/` | `mcast-relay.service` + hourly check `.service`/`.timer` |
| `sysctl/` | Kernel settings drop-in |
| `mcast-relay-appliance-runbook.md` | Full as-built runbook (master doc) |

## Install

Interface pinning and addressing are site-specific — do those first
(runbook sections 4–5). Then on the appliance:

```bash
git clone <this repo>
cd multicast-relay
sudo ./install.sh
```

The installer installs everything **disabled and locked down**: the
firewall drops all forwarding until setup has run, and the relay refuses
to start without a valid config (`--check` validation runs as
`ExecStartPre`).

## Guided setup

```bash
sudo mcast-relay-setup
```

The wizard asks for:

1. **Which interfaces** — the one facing the multicast source (e.g. the
   Nyquist server VLAN) and the one facing the receivers
2. **The multicast source IP** — the sender's unicast address; the static
   `(S,G)` key. Get a DHCP reservation or static for it, or forwarding
   breaks silently when it changes
3. **Multicast groups** — a single group, a comma list, or a range like
   `239.1.1.1-239.1.1.39`
4. **Port policy** — relay **all UDP ports** within those groups
   (recommended for paging: RTCP rides along free, per-zone port changes
   don't matter) or **only specific ports** (enforced by nftables)

It then writes `/opt/mcast-relay/relay.conf`, regenerates a **scoped**
nftables ruleset (only the configured groups — and ports, if restricted —
may cross; default-deny everything else), validates the config, and
enables the relay plus the hourly health-check timer.

Re-running the wizard is safe; the previous config is kept as
`relay.conf.bak.<timestamp>`. To apply a prepared config without
prompts: `sudo mcast-relay-setup /path/to/relay.conf`.

## Verify

```bash
journalctl -u mcast-relay -f        # expect "vif0=... vif1=..." then
                                    # "forwarding N groups ... across M sockets"
cat /proc/net/ip_mr_vif             # two VIFs; counters climbing on vif1
cat /proc/net/ip_mr_cache           # active (S,G) entries
tcpdump -i <src-if> -nnv -c 5 host 239.1.1.32
tcpdump -i <dst-if> -nnv -c 5 host 239.1.1.32
```

**The TTL decrement is the only real proof** — 16 in on the source side,
15 out on the receiver side. Identical TTL on both sides means you're
looking at a flooded copy and the relay isn't in the path.

Also re-test the unicast block from a **third host** on the source VLAN —
a ping from the relay itself proves nothing (that's OUTPUT, and the relay
has an interface in both subnets anyway).

## Known limitations

1. **Static (S,G)** — keyed to the source IP; a changed address breaks
   forwarding silently (health check + scheduled test tone catch it).
2. **Fixed group set** — groups configured here only; adding a zone in the
   paging system UI does not update the relay. Re-run setup.
3. **No HA** — two relays would double-deliver; active/standby needs real
   design.
4. **Not vendor-supported** — if the paging carries emergency notification,
   the long-term answers are PIM on the customer's L3 or a second paging
   server at the remote site.

## License

MIT
