#!/bin/bash
set -euo pipefail
. /etc/os-release
if [[ "$ID" != ubuntu || "$VERSION_ID" != 24.04 ]]; then
  echo 'This setup requires Ubuntu 24.04. Use a dedicated Gazebo-Ubuntu distro.' >&2
  exit 1
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y curl gnupg ca-certificates mesa-utils
curl -fsSL https://packages.osrfoundation.org/gazebo.gpg -o /usr/share/keyrings/pkgs-osrf-archive-keyring.gpg
printf '%s\n' 'deb [arch=amd64 signed-by=/usr/share/keyrings/pkgs-osrf-archive-keyring.gpg] https://packages.osrfoundation.org/gazebo/ubuntu-stable noble main' > /etc/apt/sources.list.d/gazebo-stable.list
apt-get update
apt-get install -y gz-harmonic
if ! id gazebo >/dev/null 2>&1; then useradd -m -s /bin/bash gazebo; fi
gz sim --versions
