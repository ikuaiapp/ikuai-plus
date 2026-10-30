#!/usr/bin/env bash
set -euo pipefail

if [[ $(id -u) -ne 0 ]]; then
  echo 'Run as root on the VPS host.' >&2
  exit 1
fi
for tool in curl sha256sum tar python3; do
  command -v "$tool" >/dev/null || { echo "Missing required tool: $tool" >&2; exit 1; }
done
if [[ ! -r /dev/tty ]]; then
  echo 'An interactive terminal is required for the disk confirmation.' >&2
  exit 1
fi

release='https://github.com/ikuaiapp/ikuai-plus/releases/download/vps-reinstall-3.7.16-tencent-debian12-20261031'
source_sha='7f561409bd43f1681a4bb123b1395fb39c5550d0724091d0ea053f751f8af14b'
manifest_sha='c5a8168c3297517d4ed69c04c7c68179a37bf99f6952814fd2ffcc6fcc993d2f'
provider_args=()
if public_ip=$(curl --noproxy '*' -fsS --max-time 3 \
    http://169.254.0.23/latest/meta-data/public-ipv4 2>/dev/null); then
  # The installer independently checks Tencent metadata, WAN MAC, private IP,
  # and public egress before accepting this NAT mode.
  provider_args=(--tencent-nat-public-ip "$public_ip")
fi
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
curl -fL --retry 3 --connect-timeout 10 --max-time 180 -o "$tmp/source.tar.gz" "$release/ikuai-vps-reinstall-source.tar.gz"
printf '%s  %s\n' "$source_sha" "$tmp/source.tar.gz" | sha256sum -c -
tar -xzf "$tmp/source.tar.gz" -C "$tmp"
python3 "$tmp/vps_reinstall/install.py" \
  --manifest-url "$release/ikuai-cloud-v1-manifest.json" \
  --manifest-sha256 "$manifest_sha" \
  "${provider_args[@]}" --execute </dev/tty
