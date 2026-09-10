#!/bin/bash
# Multicast relay appliance installer.
# Run ON the appliance as root: sudo ./install.sh
#
# Installs the relay layer: code, sysctl, a LOCKED-DOWN nftables ruleset
# (policy drop — nothing forwards until guided setup), and systemd units
# (not yet enabled). Interface name pinning and IP addressing are
# site-specific — follow the runbook, sections 4-5, by hand first.
#
# Afterwards run the guided setup:
#   sudo mcast-relay-setup

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

[ "$EUID" -eq 0 ] || { echo -e "${RED}Run as root: sudo ./install.sh${NC}"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR=/opt/mcast-relay
STATE_DIR=/var/lib/mcast-relay

echo -e "${GREEN}=== Multicast Relay Installer ===${NC}"
echo ""

echo -e "${YELLOW}[1/6] Installing system packages...${NC}"
# On the target appliance (Debian) install the deps; elsewhere (e.g. a dev
# box) skip quietly if there's no apt.
if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends \
        python3-minimal nftables tcpdump iproute2 chrony \
        || echo -e "${YELLOW}  Package install had errors — check manually.${NC}"
else
    echo "  apt-get not found — assuming dependencies are already present."
fi
command -v nft >/dev/null 2>&1 || { echo -e "${RED}nft not found.${NC}"; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo -e "${RED}python3 not found.${NC}"; exit 1; }

echo -e "${YELLOW}[2/6] Installing relay files...${NC}"
mkdir -p "$INSTALL_DIR" "$STATE_DIR"
cp "$SCRIPT_DIR/mcast-relay.py"        "$INSTALL_DIR/"
cp "$SCRIPT_DIR/check-relay.sh"        "$INSTALL_DIR/"
cp "$SCRIPT_DIR/setup.sh"             "$INSTALL_DIR/"
cp "$SCRIPT_DIR/relay.conf.example"    "$INSTALL_DIR/"
cp "$SCRIPT_DIR/README.md"            "$INSTALL_DIR/" 2>/dev/null || true
chmod 755 "$INSTALL_DIR/mcast-relay.py" "$INSTALL_DIR/check-relay.sh" "$INSTALL_DIR/setup.sh"

# Always syntax-check the pasted/checked-out code — a bad file inside a
# systemd restart loop hides the error (runbook section 8):
python3 -c "import ast; ast.parse(open('$INSTALL_DIR/mcast-relay.py').read()); print('  mcast-relay.py: syntax ok')"

echo -e "${YELLOW}[3/6] Applying kernel settings...${NC}"
cp "$SCRIPT_DIR/sysctl/99-mcast-relay.conf" /etc/sysctl.d/
sysctl -q -w net.ipv4.ip_forward=1
sysctl -q -w net.ipv4.conf.all.rp_filter=0
sysctl -q -w net.ipv4.conf.default.rp_filter=0
sysctl -q -w net.ipv4.conf.all.force_igmp_version=2
sysctl -q -w net.ipv4.igmp_max_memberships=100
echo "  forwarding on, rp_filter off, IGMPv2, igmp_max_memberships=100"

echo -e "${YELLOW}[4/6] Installing LOCKED-DOWN firewall...${NC}"
# Until guided setup generates the scoped ruleset, block ALL forwarding.
# (ip_forward=1 is already on — deny-by-default must land in the same window.)
cat > /etc/nftables.conf <<'EOF'
#!/usr/sbin/nft -f
flush ruleset

# Placeholder ruleset installed by install.sh — everything dropped.
# Guided setup (sudo mcast-relay-setup) replaces this with the scoped
# multicast-only ruleset once interfaces, source, groups, and ports are known.
table inet relay {
    chain forward {
        type filter hook forward priority 0; policy drop;
    }
}
EOF
systemctl enable nftables
systemctl restart nftables
nft list ruleset | grep -q "table inet relay" \
    || { echo -e "${RED}nftables ruleset failed to load (expected 'table inet relay').${NC}"; exit 1; }
echo "  default-deny forward ruleset active"

echo -e "${YELLOW}[5/6] Installing systemd units...${NC}"
cp "$SCRIPT_DIR/systemd/mcast-relay.service"            /etc/systemd/system/
cp "$SCRIPT_DIR/systemd/mcast-relay-check.service"       /etc/systemd/system/
cp "$SCRIPT_DIR/systemd/mcast-relay-check.timer"         /etc/systemd/system/
systemctl daemon-reload
# Deliberately NOT enabling anything yet: the relay only starts after a
# successful guided setup produces a valid relay.conf.

echo -e "${YELLOW}[6/6] Creating setup launcher...${NC}"
ln -sf "$INSTALL_DIR/setup.sh" /usr/local/bin/mcast-relay-setup

echo ""
echo -e "${GREEN}=== Installation Complete ===${NC}"
echo ""
echo "  Relay installed to: $INSTALL_DIR"
echo "  Firewall:           default-deny (multicast flows only after setup)"
echo "  Units:              installed but not enabled (setup enables them)"
echo ""
echo -e "${YELLOW}Next: run the guided setup — it asks for interfaces, the${NC}"
echo -e "${YELLOW}multicast source, groups, and the port policy, then enables${NC}"
echo -e "${YELLOW}the relay and the hourly health check:${NC}"
echo ""
echo "    sudo mcast-relay-setup"
echo ""
read -r -p "Run guided setup now? [y/N]: " ans
if [ "${ans,,}" = "y" ] || [ "${ans,,}" = "yes" ]; then
    exec "$INSTALL_DIR/setup.sh"
fi
