#!/bin/bash
# Provision the OS only. No worker starts before data migration is complete.
set -euo pipefail
if [[ -f /var/lib/jobradar/bootstrap-complete ]] && command -v docker >/dev/null; then
  exit 0
fi
export DEBIAN_FRONTEND=noninteractive
install -d -m 700 /opt/jobradar /var/lib/jobradar
if ! swapon --show=NAME --noheadings | grep -qx /swapfile; then
  if [[ ! -f /swapfile ]]; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
  fi
  swapon /swapfile
fi
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
echo 'vm.swappiness=10' > /etc/sysctl.d/90-jobradar.conf
sysctl -p /etc/sysctl.d/90-jobradar.conf
apt-get update -qq
apt-get install -y ca-certificates curl python3
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
cat > /etc/apt/sources.list.d/docker.sources <<'EOF'
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: bookworm
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
EOF
apt-get update -qq
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
systemctl enable --now docker
install -d /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=100M\n' > /etc/systemd/journald.conf.d/jobradar.conf
systemctl restart systemd-journald
touch /var/lib/jobradar/bootstrap-complete
echo 'JobRadar OS bootstrap complete; workers remain stopped.'
