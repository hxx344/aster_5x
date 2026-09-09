#!/usr/bin/env bash
# Build before switching the running Linux service; preserve credentials and SQLite.
set -euo pipefail
umask 077
[[ ${EUID} -eq 0 ]] || { printf 'Run with sudo or as root.\n' >&2; exit 1; }
command -v systemctl >/dev/null || { printf 'systemd is required.\n' >&2; exit 1; }
[[ -d /run/systemd/system ]] || { printf 'systemd must be running.\n' >&2; exit 1; }
if command -v apt-get >/dev/null; then
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv curl ca-certificates tar xz-utils
elif command -v dnf >/dev/null; then
  dnf install -y python3 python3-pip curl ca-certificates tar xz
fi
python3 -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required"'
root=/opt/aster-desk
mkdir -p "$root/releases" /etc/aster-desk /var/lib/aster-desk
stage=$(mktemp -d "$root/releases/release-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
switched=0
old=''
if [[ -L $root/current && -d $root/current ]]; then old=$(readlink -f "$root/current"); fi
was_active=0
systemctl is-active --quiet aster-desk && was_active=1
had_unit=0
if [[ -f /etc/systemd/system/aster-desk.service ]]; then
  had_unit=1
  cp /etc/systemd/system/aster-desk.service "$stage/previous.service"
fi
cleanup() {
  result=$?
  if [[ $result -ne 0 && $switched -eq 1 ]]; then
    systemctl stop aster-desk || true
    if [[ -n $old ]]; then
      ln -sfn "$old" "$root/current.rollback"
      mv -Tf "$root/current.rollback" "$root/current"
    else
      rm -f "$root/current"
    fi
    if [[ $had_unit -eq 1 ]]; then
      cp "$stage/previous.service" /etc/systemd/system/aster-desk.service
    else
      systemctl disable aster-desk >/dev/null 2>&1 || true
      rm -f /etc/systemd/system/aster-desk.service
    fi
    systemctl daemon-reload
    if [[ $was_active -eq 1 ]]; then systemctl start aster-desk || true; fi
    printf 'Upgrade failed; previous service restored. Failed build retained at %s\n' "$stage" >&2
  fi
  exit "$result"
}
trap cleanup EXIT
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-/nonexistent}")" 2>/dev/null && pwd || true)
if [[ -f $source_dir/trading/server.py && -f $source_dir/dashboard/package-lock.json ]]; then
  tar -C "$source_dir" --exclude='*/node_modules' --exclude='*/dist' --exclude='*/.wrangler' --exclude='*/.vinext' \
    --exclude='*/__pycache__' --exclude='*/.env*' -cf - trading dashboard requirements.txt requirements.lock monitor.py config.json deploy DEPLOYMENT.md | tar -C "$stage" -xf -
else
  curl --retry 3 -fsSL https://codeload.github.com/hxx344/aster_5x/tar.gz/refs/heads/main -o "$stage/source.tar.gz"
  tar -xzf "$stage/source.tar.gz" -C "$stage" --strip-components=1
  rm "$stage/source.tar.gz"
fi
# Node is needed only during build. Pin the official archive and verify its checksum.
if ! command -v node >/dev/null || ! node -e 'const [a,b]=process.versions.node.split(".").map(Number); process.exit(a>22 || (a===22 && b>=13) ? 0 : 1)'; then
  case "$(uname -m)" in x86_64) arch=x64 ;; aarch64|arm64) arch=arm64 ;; *) printf 'Unsupported Node architecture\n' >&2; exit 1 ;; esac
  version=v22.23.2
  archive="node-$version-linux-$arch.tar.xz"
  mkdir -p "$root/node-$version"
  curl --retry 3 -fsSL "https://nodejs.org/dist/$version/$archive" -o "$stage/$archive"
  curl --retry 3 -fsSL "https://nodejs.org/dist/$version/SHASUMS256.txt" -o "$stage/SHASUMS256.txt"
  (cd "$stage"; awk -v filename="$archive" '$2 == filename' SHASUMS256.txt | sha256sum --check --status)
  tar -xJf "$stage/$archive" -C "$root/node-$version" --strip-components=1
  rm "$stage/$archive" "$stage/SHASUMS256.txt"
  export PATH="$root/node-$version/bin:$PATH"
fi
python3 -m venv "$stage/.venv"
"$stage/.venv/bin/pip" install --disable-pip-version-check -q -r "$stage/requirements.lock"
(cd "$stage/dashboard"; npm ci --no-fund; npm run typecheck; npm run lint; npm run build)
test -s "$stage/dashboard/dist/client/index.html"
(cd "$stage"; .venv/bin/python -c 'from trading.server import create_app')
id aster-desk >/dev/null 2>&1 || useradd --system --home-dir /var/lib/aster-desk --shell /usr/sbin/nologin aster-desk
chown aster-desk:aster-desk /var/lib/aster-desk
chmod 700 /var/lib/aster-desk /etc/aster-desk
new_password=''
if [[ ! -f /etc/aster-desk/environment ]]; then
  new_password=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
  sed "s/replace-with-at-least-16-random-characters/$new_password/" "$stage/deploy/trading.env.example" > /etc/aster-desk/environment
fi
chmod 600 /etc/aster-desk/environment
# Runtime user can read release code; credentials and runtime keep their own permissions.
chmod -R a+rX "$stage"
chmod 755 "$root" "$root/releases"
switched=1
if [[ $was_active -eq 1 ]]; then systemctl stop aster-desk; fi
ln -sfn "$stage" "$root/current.next"
mv -Tf "$root/current.next" "$root/current"
install -m 644 "$stage/deploy/aster-desk.service" /etc/systemd/system/aster-desk.service
install -m 755 "$stage/deploy/aster-desk" /usr/local/bin/aster-desk
systemctl daemon-reload
systemctl enable --now aster-desk
healthy=0
for attempt in $(seq 1 30); do
  if systemctl is-active --quiet aster-desk && curl --max-time 2 -fsS http://127.0.0.1:8765/api/health | python3 -c 'import json,sys; assert json.load(sys.stdin)["status"] == "ok"' 2>/dev/null; then
    healthy=1; break
  fi
  sleep 2
done
[[ $healthy -eq 1 ]] || { journalctl -u aster-desk --no-pager -n 15; exit 1; }
switched=0
if systemctl list-unit-files aster-5x.service --no-legend 2>/dev/null | grep -q '^aster-5x.service'; then
  systemctl disable --now aster-5x
fi
printf '\nAster Desk installed. http://127.0.0.1:8765 (use SSH tunnel or HTTPS reverse proxy)\n'
if [[ -n $new_password ]]; then printf 'Dashboard password: %s\n' "$new_password"; fi
printf 'Server environment: /etc/aster-desk/environment\nStatus: sudo aster-desk status\nGuide: %s/DEPLOYMENT.md\n' "$stage"
