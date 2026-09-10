#!/usr/bin/env python3
"""Static multicast relay using the Linux kernel MRT forwarding cache.

Forwards configured multicast groups from a source-facing interface to a
receiver-facing interface. Preserves the original source IP; every UDP port
within each group crosses automatically. Port restriction, when configured,
is enforced by the nftables ruleset generated during guided setup.

Configuration is read from /opt/mcast-relay/relay.conf (override with the
MCAST_RELAY_CONF environment variable or a positional argument):

    IN_IF=       local IP on the source-facing interface (vif0)
    OUT_IF=      local IP on the receiver-facing interface (vif1)
    SOURCE=      multicast sender's unicast IP for the static (S,G) entry,
                 or "any" for a wildcard (*,G) entry (not recommended)
    GROUPS=      comma-separated groups/ranges, e.g. 239.1.1.1-239.1.1.39
    PORT_POLICY= all | list   (informational here; enforced by nftables)
    PORTS=       UDP ports/ranges when PORT_POLICY=list

Run with --check to validate the configuration without touching the kernel.
"""

import os
import signal
import socket
import struct
import sys
import time

DEFAULT_CONF = "/opt/mcast-relay/relay.conf"

JOINS_PER_SOCKET = 15   # stays below default net.ipv4.igmp_max_memberships (20)
MAX_GROUPS = 512        # sanity cap

IPPROTO_IP = 0
MRT_INIT, MRT_DONE, MRT_ADD_VIF, MRT_ADD_MFC, MRT_DEL_MFC = 200, 201, 202, 204, 205
MAXVIFS = 32
VIFCTL = "=HBBI4s4s"
MFCCTL = "=4s4sH32s2xIIIi"

joiners = []  # module scope deliberately: if these sockets are garbage
              # collected the kernel drops the group memberships


def die(msg):
    print(f"mcast-relay: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


# ------------------------------------------------------------ config parsing

def parse_conf(path):
    conf = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                conf[key.strip()] = value.strip()
    except OSError as e:
        die(f"cannot read config {path}: {e}")
    for key in ("IN_IF", "OUT_IF", "SOURCE", "GROUPS"):
        if not conf.get(key):
            die(f"config {path} is missing {key}=... (run: sudo mcast-relay-setup)")
    return conf


def valid_ip(s):
    try:
        socket.inet_aton(s)
        return True
    except OSError:
        return False


def ip_to_int(s):
    return struct.unpack("!I", socket.inet_aton(s))[0]


def int_to_ip(n):
    return socket.inet_ntoa(struct.pack("!I", n))


def is_local(addr):
    """True if addr is assigned to one of this host's interfaces."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((addr, 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


def expand_groups(spec):
    """Expand a GROUPS spec (single / list / ranges) into a deduped list."""
    groups = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, _, hi = part.partition("-")
            lo, hi = lo.strip(), hi.strip()
            if not (valid_ip(lo) and valid_ip(hi)):
                die(f"bad group range in GROUPS: {part}")
            n_lo, n_hi = ip_to_int(lo), ip_to_int(hi)
            if n_hi < n_lo:
                die(f"group range is reversed: {part}")
            if n_hi - n_lo > MAX_GROUPS:
                die(f"group range too large (> {MAX_GROUPS}): {part}")
            groups.extend(int_to_ip(n) for n in range(n_lo, n_hi + 1))
        else:
            if not valid_ip(part):
                die(f"bad group address in GROUPS: {part}")
            groups.append(part)

    ordered = list(dict.fromkeys(groups))
    if not ordered:
        die("GROUPS expands to nothing")
    for g in ordered:
        if (ip_to_int(g) >> 28) != 0xE:
            die(f"{g} is not a multicast address (224.0.0.0/4)")
    if len(ordered) > MAX_GROUPS:
        die(f"too many groups ({len(ordered)} > {MAX_GROUPS})")
    return ordered


def resolve_source(raw):
    """Return the (S,G) source address: a unicast IP, or 0.0.0.0 for 'any'."""
    if raw.lower() == "any":
        return "0.0.0.0"  # wildcard (*,G) MFC entry
    if not valid_ip(raw):
        die(f"SOURCE must be a valid IPv4 address or 'any', got: {raw}")
    if int(raw.split(".")[0]) >= 224:
        die(f"SOURCE should be the sender's UNICAST address, not a group: {raw}")
    return raw


# ---------------------------------------------------------------- kernel API

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


# ------------------------------------------------------------------- main

def load_config(conf_path):
    conf = parse_conf(conf_path)
    in_if = conf["IN_IF"]
    out_if = conf["OUT_IF"]
    source = resolve_source(conf["SOURCE"])
    groups = expand_groups(conf["GROUPS"])
    port_policy = conf.get("PORT_POLICY", "all")

    if in_if == out_if:
        die("IN_IF and OUT_IF must be different local addresses")
    for name, addr in (("IN_IF", in_if), ("OUT_IF", out_if)):
        if not valid_ip(addr) or not is_local(addr):
            die(f"{name}={addr} is not assigned to any interface on this host")

    return in_if, out_if, source, groups, port_policy


def main():
    check_only = "--check" in sys.argv[1:]
    positional = [a for a in sys.argv[1:] if a != "--check"]
    conf_path = (positional[0] if positional
                 else os.environ.get("MCAST_RELAY_CONF", DEFAULT_CONF))

    in_if, out_if, source, groups, port_policy = load_config(conf_path)

    span = (f"{groups[0]}..{groups[-1]}" if len(groups) > 1 else groups[0])
    print(f"config : in={in_if} out={out_if} source={source} "
          f"groups={len(groups)} ({span}) ports={port_policy}", flush=True)

    if check_only:
        print("--check: configuration valid", flush=True)
        return

    s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_IGMP)
    s.setsockopt(IPPROTO_IP, MRT_INIT, 1)
    add_vif(s, 0, in_if)
    add_vif(s, 1, out_if)
    print(f"vif0={in_if} vif1={out_if}", flush=True)

    j = None
    for i, grp in enumerate(groups):
        if i % JOINS_PER_SOCKET == 0:
            j = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            joiners.append(j)
        j.setsockopt(IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     socket.inet_aton(grp) + socket.inet_aton(in_if))
        mfc(s, MRT_ADD_MFC, source, grp, 0, 1)

    print(f"forwarding {len(groups)} groups from {source} across "
          f"{len(joiners)} sockets: src => dst", flush=True)

    def bye(*_):
        for grp in groups:
            try:
                mfc(s, MRT_DEL_MFC, source, grp, 0)
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
