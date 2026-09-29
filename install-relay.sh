#!/usr/bin/env bash
# Public-data-only relay; build and validate before replacing a running release.
set -Eeuo pipefail
umask 077
die() { printf '%s\n' "$*" >&2; exit 1; }
[[ $(uname -s) == Linux && $EUID -eq 0 ]] || die 'Run this Linux installer as root or with sudo.'
[[ -d /run/systemd/system ]] || die 'A running systemd is required.'
address=''
while (($#)); do
  case $1 in
    --address) [[ $# -ge 2 ]] || die '--address requires a public IP'; address=$2; shift 2 ;;
    --help|-h) printf 'Usage: sudo bash install-relay.sh [--address PUBLIC_IP]\n'; exit 0 ;;
    *) die 'Unknown argument; use --help.' ;;
  esac
done
root=/opt/aster-capacity-relay
etc=/etc/aster-capacity-relay
unit=/etc/systemd/system/aster-capacity-relay.service
cli=/usr/local/bin/aster-capacity-relay
service=aster-capacity-relay
stage='' scratch='' old='' switched=0 was_active=0 source_revision=''
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-/nonexistent}")" 2>/dev/null && pwd || true)
system_ready() {
  local tool
  for tool in python3 curl tar openssl sha256sum flock; do command -v "$tool" >/dev/null || return 1; done
  python3 -c 'import sys,venv,ensurepip,ssl; assert sys.version_info >= (3,11); ssl.create_default_context()' 2>/dev/null
}
if ! system_ready; then
  if command -v apt-get >/dev/null; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv curl tar openssl ca-certificates util-linux
  elif command -v dnf >/dev/null; then
    dnf install -y python3 python3-pip curl tar openssl ca-certificates util-linux
  fi
fi
system_ready || die 'Python 3.11+, venv, openssl, curl and systemd are required (Debian 12+ or Ubuntu 24.04+ recommended).'
mkdir -p "$root/releases" "$root/cache"
chmod 755 "$root" "$root/releases" "$root/cache"
exec 9>"$root/upgrade.lock"
flock -n 9 || die 'Another relay installation is running.'
if [[ -L $root/current && -d $root/current ]]; then old=$(readlink -f "$root/current"); fi
systemctl is-active --quiet "$service" && was_active=1
cleanup() {
  local result=$?
  trap - EXIT HUP INT TERM
  if ((result != 0 && switched)); then
    printf '[relay] Upgrade failed; restoring the previous release.\n' >&2
    systemctl stop "$service" >/dev/null 2>&1 || true
    if [[ -n $old ]]; then
      ln -sfn "$old" "$root/current.rollback"
      mv -Tf "$root/current.rollback" "$root/current"
    else
      rm -f -- "$root/current"
    fi
    if [[ -f $scratch/previous.service ]]; then install -m 644 "$scratch/previous.service" "$unit"; else rm -f -- "$unit"; fi
    if [[ -f $scratch/previous.cli ]]; then install -m 755 "$scratch/previous.cli" "$cli"; else rm -f -- "$cli"; fi
    systemctl daemon-reload || true
    if ((was_active)); then
      systemctl start "$service" || printf 'Previous relay service could not restart; inspect systemctl status.\n' >&2
    fi
  fi
  # These are this invocation's unique staging directories, never runtime data.
  if [[ -n $stage && $stage == "$root"/releases/release-* && -f $stage/.install-owned &&
        $(readlink -f "$root/current" 2>/dev/null || true) != "$stage" ]]; then
    rm -rf -- "$stage"
  fi
  if [[ -n $scratch && $scratch == "$root"/.source-* ]]; then rm -rf -- "$scratch"; fi
  exit "$result"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
scratch=$(mktemp -d "$root/.source-XXXXXX")
if [[ ! -f $source_dir/trading/capacity_relay_server.py || ! -f $source_dir/requirements-relay.lock ]]; then
  source_revision=$(curl --retry 3 -fsSL https://api.github.com/repos/hxx344/aster_5x/commits/main | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])')
  [[ $source_revision =~ ^[a-f0-9]{40}$ ]] || die 'Invalid source revision.'
  if [[ -n $old && -f $old/.install-ready && $(cat "$old/.install-revision" 2>/dev/null || true) == "$source_revision" ]]; then
    source_dir=$old
    printf '[relay] Source revision unchanged; archive download skipped.\n'
  else
    printf '[relay] Downloading public relay source.\n'
    curl --retry 3 -fsSL "https://codeload.github.com/hxx344/aster_5x/tar.gz/$source_revision" -o "$scratch/source.tar.gz"
    mkdir "$scratch/source"
    tar -xzf "$scratch/source.tar.gz" --strip-components=1 -C "$scratch/source"
    source_dir=$scratch/source
  fi
fi
files=(monitor.py trading/__init__.py trading/capacity_relay_server.py requirements-relay.lock install-relay.sh
       deploy/configure-capacity-relay.py deploy/aster-capacity-relay.service deploy/aster-capacity-relay deploy/relay.env.example)
for name in "${files[@]}"; do [[ -f $source_dir/$name ]] || die "Missing relay file: $name"; done
fingerprints=$(python3 - "$source_dir" "${files[@]}" <<'PY'
import hashlib, json, os, platform, sys, sysconfig
from pathlib import Path
source = Path(sys.argv[1])
runtime = json.dumps([sys.version, os.path.realpath(sys.executable), sysconfig.get_config_var('SOABI'), platform.machine(), platform.libc_ver()])
dependency = hashlib.sha256(('relay-python-v1:' + runtime).encode() + (source / 'requirements-relay.lock').read_bytes()).hexdigest()
content = hashlib.sha256(('relay-release-v1:' + dependency).encode())
for name in sorted(sys.argv[2:]):
    data = (source / name).read_bytes()
    content.update(name.encode() + b'\0' + len(data).to_bytes(8, 'big') + data)
print(dependency, content.hexdigest(), sep='\n')
PY
)
mapfile -t keys <<< "$fingerprints"
installed_state() {
  sha256sum "$etc/environment" "$etc/server.crt" "$etc/server.key" "$etc/address" "$unit" "$cli" | sha256sum | cut -d ' ' -f1
}
address_matches=1
if [[ -n $address && -f $etc/address ]]; then
  python3 - "$address" "$etc/address" <<'PY' || address_matches=0
import ipaddress, pathlib, sys
assert ipaddress.ip_address(sys.argv[1]) == ipaddress.ip_address(pathlib.Path(sys.argv[2]).read_text().strip())
PY
fi
[[ $address_matches == 1 ]] || die 'Installed address and certificate are preserved; --address must match.'
if [[ -n $old && -f $old/.install-ready && -f $old/.venv/.complete &&
      $(cat "$old/.install-key" 2>/dev/null || true) == "${keys[1]}" &&
      $(cat "$old/.install-state" 2>/dev/null || true) == "$(installed_state 2>/dev/null || true)" ]] &&
    systemctl is-active --quiet "$service" && systemctl is-enabled --quiet "$service" &&
    [[ $(systemctl show --property=NeedDaemonReload --value "$service") == no ]] &&
    python3 "$source_dir/deploy/configure-capacity-relay.py" health >/dev/null 2>&1; then
  if [[ -n $source_revision ]]; then printf '%s\n' "$source_revision" > "$old/.install-revision"; fi
  printf '[relay] Code, dependencies and configuration unchanged; dependency installation, validation and restart skipped.\n'
  exit 0
fi
if [[ ! -f $etc/address && -z $address ]]; then
  if { true </dev/tty; } 2>/dev/null; then
    IFS= read -r -p 'Relay server public IP address: ' address </dev/tty
  else
    die 'First installation requires --address PUBLIC_IP (or an interactive terminal).'
  fi
fi
id "$service" >/dev/null 2>&1 || useradd --system --user-group --home-dir "$root" --no-create-home --shell /usr/sbin/nologin "$service"
install -d -m 750 -o root -g "$service" "$etc"
python3 "$source_dir/deploy/configure-capacity-relay.py" prepare-server --address "$address"
chmod 600 "$etc/environment" "$etc/connection.json" "$etc/address"
chown root:"$service" "$etc/server.key" "$etc/server.crt"
chmod 640 "$etc/server.key" "$etc/server.crt"
stage=$(mktemp -d "$root/releases/release-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
touch "$stage/.install-owned"
mkdir "$stage/trading" "$stage/deploy"
for name in "${files[@]}"; do install -m 644 "$source_dir/$name" "$stage/$name"; done
python_env="$root/cache/python-${keys[0]}"
if [[ -x $python_env/bin/python && -f $python_env/.complete ]]; then
  printf '[relay] Python dependencies unchanged; reuse completed environment.\n'
else
  # The cache path is derived exclusively from a validated SHA-256 fingerprint.
  [[ $python_env == "$root"/cache/python-* && ${keys[0]} =~ ^[a-f0-9]{64}$ ]] || die 'Invalid dependency cache path.'
  if [[ -e $python_env ]]; then rm -rf -- "$python_env"; fi
  python3 -m venv "$python_env"
  "$python_env/bin/python" -m pip install --no-cache-dir --disable-pip-version-check -q -r "$stage/requirements-relay.lock"
  "$python_env/bin/python" -m pip check
  touch "$python_env/.complete"
  chmod -R a+rX "$python_env"
  printf '[relay] Python dependencies installed and checked.\n'
fi
ln -s "$python_env" "$stage/.venv"
"$python_env/bin/python" - "$stage" <<'PY'
from pathlib import Path
import sys
root = Path(sys.argv[1])
for path in root.rglob('*.py'):
    if '.venv' not in path.parts:
        compile(path.read_text(encoding='utf-8'), str(path), 'exec')
PY
chmod -R a+rX "$stage"
systemd-run --quiet --wait --pipe --collect --unit="aster-capacity-relay-check-$$" \
  --uid="$service" --gid="$service" --property="EnvironmentFile=$etc/environment" \
  --property="WorkingDirectory=$stage" "$python_env/bin/python" -m trading.capacity_relay_server --check-config
if [[ -f $unit ]]; then cp -p "$unit" "$scratch/previous.service"; fi
if [[ -f $cli ]]; then cp -p "$cli" "$scratch/previous.cli"; fi
switched=1
ln -sfn "$stage" "$root/current.next"
mv -Tf "$root/current.next" "$root/current"
install -m 644 "$stage/deploy/aster-capacity-relay.service" "$unit"
install -m 755 "$stage/deploy/aster-capacity-relay" "$cli"
systemctl daemon-reload
systemctl enable "$service"
systemctl restart "$service"
healthy=0
for attempt in $(seq 1 20); do
  if systemctl is-active --quiet "$service" && python3 "$stage/deploy/configure-capacity-relay.py" health >/dev/null 2>&1; then
    healthy=1; break
  fi
  sleep 1
done
[[ $healthy == 1 ]] || die 'Relay health check failed; restoring the previous release.'
printf '%s\n' "${keys[1]}" > "$stage/.install-key"
printf '%s\n' "$source_revision" > "$stage/.install-revision"
installed_state > "$stage/.install-state"
touch "$stage/.install-ready"
switched=0
# Keep the current release and one rollback release. Never inspect trading data.
python3 - "$root" "$stage" "$old" <<'PY'
import re, shutil, sys
from pathlib import Path
root, current = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
previous = Path(sys.argv[3]).resolve() if sys.argv[3] else None
releases = root / 'releases'
for path in releases.iterdir():
    if (path.is_symlink() or not path.is_dir() or path.resolve().parent != releases
            or not re.fullmatch(r'release-\d{8}T\d{6}Z-[A-Za-z0-9]{6}', path.name)
            or not (path / '.install-owned').is_file() or path in (current, previous)):
        continue
    shutil.rmtree(path)
PY
printf '[relay] Installed and healthy. TLS port: 8766; default market: XAUUSD1.\n'
printf 'Status: sudo aster-capacity-relay status\nLogs: sudo aster-capacity-relay logs\n'
printf 'Export the private connection JSON only when ready to import it on the main server:\n  sudo aster-capacity-relay export-connect\n'
printf 'Then on the main server: sudo aster-desk relay-connect\n'
