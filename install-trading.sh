#!/usr/bin/env bash
# Build before switching the running Linux service; preserve credentials and SQLite.
set -euo pipefail
umask 077
[[ ${EUID} -eq 0 ]] || { printf 'Run with sudo or as root.\n' >&2; exit 1; }
command -v systemctl >/dev/null || { printf 'systemd is required.\n' >&2; exit 1; }
[[ -d /run/systemd/system ]] || { printf 'systemd must be running.\n' >&2; exit 1; }
root=/opt/aster-desk
started=$SECONDS
step_started=$SECONDS
step_name=''
step() {
  if [[ -n $step_name ]]; then printf '[upgrade] %s: %ss\n' "$step_name" "$((SECONDS - step_started))"; fi
  step_name=$1
  step_started=$SECONDS
  printf '[upgrade] %s...\n' "$step_name"
}
step 'Check system dependencies'
system_ready() {
  local tool
  for tool in python3 curl tar xz sha256sum flock; do command -v "$tool" >/dev/null || return 1; done
  python3 -c 'import sys, venv, ensurepip, ssl, os; assert sys.version_info >= (3, 10); assert os.path.isfile(ssl.get_default_verify_paths().cafile or "")' 2>/dev/null
}
if ! system_ready; then
  if command -v apt-get >/dev/null; then
    apt-get update -qq
    apt-get install -y -qq python3 python3-venv curl ca-certificates tar xz-utils util-linux
  elif command -v dnf >/dev/null; then
    dnf install -y python3 python3-pip curl ca-certificates tar xz util-linux
  fi
fi
system_ready || { printf 'Python 3.10+, venv, CA certificates, curl, tar, xz and flock are required.\n' >&2; exit 1; }
mkdir -p "$root/releases" "$root/cache" /etc/aster-desk /var/lib/aster-desk
# Serialize cache publication and the current-release switch.
exec 9>"$root/upgrade.lock"
flock -n 9 || { printf 'Another Aster Desk upgrade is running.\n' >&2; exit 1; }
chmod 755 "$root" "$root/releases" "$root/cache"
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
  if [[ $result -ne 0 ]]; then
    printf '[upgrade] Failed during %s after %ss (total %ss).\n' "$step_name" "$((SECONDS - step_started))" "$((SECONDS - started))" >&2
  fi
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
# Bash does not run an EXIT trap when an unhandled signal terminates it.
# Route interrupted upgrades through the same rollback as a failed command.
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
step 'Download source'
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-/nonexistent}")" 2>/dev/null && pwd || true)
if [[ -f $source_dir/trading/server.py && -f $source_dir/dashboard/package-lock.json ]]; then
  tar -C "$source_dir" --exclude='*/node_modules' --exclude='*/dist' --exclude='*/.wrangler' --exclude='*/.vinext' \
    --exclude='*/__pycache__' --exclude='*/.env*' --exclude='*/.next' --exclude='*/tsconfig.tsbuildinfo' \
    -cf - trading dashboard requirements.txt requirements.lock monitor.py config.json deploy DEPLOYMENT.md | tar -C "$stage" -xf -
else
  curl --retry 3 -fsSL https://codeload.github.com/hxx344/aster_5x/tar.gz/refs/heads/main -o "$stage/source.tar.gz"
  tar -xzf "$stage/source.tar.gz" -C "$stage" --strip-components=1
  rm "$stage/source.tar.gz"
fi
# Node is needed only during build. Pin the official archive and verify its checksum.
step 'Prepare Node'
version=v22.23.2
node_ready() {
  command -v node >/dev/null && command -v npm >/dev/null &&
    node -e 'const [a,b]=process.versions.node.split(".").map(Number); process.exit(a>22 || (a===22 && b>=13) ? 0 : 1)'
}
if ! node_ready && [[ -x $root/node-$version/bin/node && -x $root/node-$version/bin/npm ]]; then
  export PATH="$root/node-$version/bin:$PATH"
fi
if ! node_ready; then
  case "$(uname -m)" in x86_64) arch=x64 ;; aarch64|arm64) arch=arm64 ;; *) printf 'Unsupported Node architecture\n' >&2; exit 1 ;; esac
  archive="node-$version-linux-$arch.tar.xz"
  mkdir -p "$root/node-$version"
  curl --retry 3 -fsSL "https://nodejs.org/dist/$version/$archive" -o "$stage/$archive"
  curl --retry 3 -fsSL "https://nodejs.org/dist/$version/SHASUMS256.txt" -o "$stage/SHASUMS256.txt"
  (cd "$stage"; awk -v filename="$archive" '$2 == filename' SHASUMS256.txt | sha256sum --check --status)
  tar -xJf "$stage/$archive" -C "$root/node-$version" --strip-components=1
  rm "$stage/$archive" "$stage/SHASUMS256.txt"
  export PATH="$root/node-$version/bin:$PATH"
fi
node_ready || { printf 'Node or npm is unavailable.\n' >&2; exit 1; }
node_identity=$(node -p 'JSON.stringify({version:process.version,abi:process.versions.modules,platform:process.platform,arch:process.arch})')
npm_identity=$(npm --version)
# Hash paths and contents, including public assets/configuration, not mtimes.
# Bump the recipe prefix when changing how dependencies or assets are prepared.
fingerprints=$(python3 - "$stage" "$node_identity" "$npm_identity" <<'PY'
import hashlib, json, os, platform, sys, sysconfig
from pathlib import Path
source = Path(sys.argv[1])
def digest(prefix, files):
    result = hashlib.sha256(prefix.encode())
    for path in sorted(files):
        name = path.relative_to(source).as_posix().encode()
        data = path.read_bytes()
        result.update(len(name).to_bytes(8, 'big') + name)
        result.update(len(data).to_bytes(8, 'big') + data)
    return result.hexdigest()
python_runtime = json.dumps([sys.version, os.path.realpath(sys.executable), sysconfig.get_config_var('SOABI'), platform.machine(), platform.libc_ver()])
python_key = digest('python-v1:' + python_runtime, [source / 'requirements.lock'])
node_runtime = json.dumps([sys.argv[2:], platform.libc_ver()])
npm_key = digest('npm-v1:' + node_runtime, [source / 'dashboard/package.json', source / 'dashboard/package-lock.json'])
ignored = {'node_modules', 'dist', '.wrangler', '.vinext', '.next', '.git', '__pycache__'}
files = []
for directory, dirs, names in os.walk(source / 'dashboard'):
    dirs[:] = [name for name in dirs if name not in ignored]
    files.extend(Path(directory) / name for name in names if not name.startswith('.env') and name != 'tsconfig.tsbuildinfo')
build_env = json.dumps({key: value for key, value in sorted(os.environ.items()) if key.startswith(('VITE_', 'NEXT_PUBLIC_')) or key == 'NODE_ENV'})
frontend_key = digest('frontend-v1:' + npm_key + build_env, files)
print(python_key, npm_key, frontend_key, sep='\n')
PY
)
mapfile -t keys <<< "$fingerprints"
step 'Prepare Python dependencies'
python_cache="$root/cache/python-${keys[0]}"
if [[ -f $python_cache/.complete && -x $python_cache/bin/python ]]; then
  python_env=$(readlink -f "$python_cache")
  printf '[upgrade] Reuse Python dependencies\n'
else
  # Keep this original path: moving an installed venv breaks script shebangs.
  python_env=$(mktemp -d "$root/cache/python-env-XXXXXX")
  python3 -m venv "$python_env"
  "$python_env/bin/python" -m pip install --disable-pip-version-check -q -r "$stage/requirements.lock"
  "$python_env/bin/python" -m pip check
  "$python_env/bin/python" -c 'import fastapi, uvicorn, eth_account, httpx'
  touch "$python_env/.complete"
  chmod -R a+rX "$python_env"
  ln -sfn "$python_env" "$python_cache"
fi
ln -s "$python_env" "$stage/.venv"
step 'Prepare dashboard'
frontend_cache="$root/cache/frontend-${keys[2]}"
if [[ -f $frontend_cache/.complete && -s $frontend_cache/client/index.html ]]; then
  mkdir -p "$stage/dashboard/dist"
  cp -a --reflink=auto "$frontend_cache/client" "$stage/dashboard/dist/client"
  printf '[upgrade] Reuse dashboard build (source unchanged)\n'
else
  step 'Prepare npm dependencies'
  npm_cache="$root/cache/npm-${keys[1]}"
  if [[ -f $npm_cache/.complete && -d $npm_cache/node_modules ]]; then
    cp -a --reflink=auto "$npm_cache/node_modules" "$stage/dashboard/node_modules"
    printf '[upgrade] Reuse npm dependencies\n'
  else
    (cd "$stage/dashboard"; npm ci --include=dev --no-audit --no-fund --prefer-offline)
    npm_dir=$(mktemp -d "$root/cache/npm-deps-XXXXXX")
    cp -a --reflink=auto "$stage/dashboard/node_modules" "$npm_dir/node_modules"
    touch "$npm_dir/.complete"
    ln -sfn "$npm_dir" "$npm_cache"
  fi
  step 'Check and build dashboard'
  (cd "$stage/dashboard"; npm run typecheck; npm run lint; npm run build)
  test -s "$stage/dashboard/dist/client/index.html"
  frontend_dir=$(mktemp -d "$root/cache/frontend-build-XXXXXX")
  cp -a --reflink=auto "$stage/dashboard/dist/client" "$frontend_dir/client"
  touch "$frontend_dir/.complete"
  ln -sfn "$frontend_dir" "$frontend_cache"
fi
step 'Validate release'
test -s "$stage/dashboard/dist/client/index.html"
(cd "$stage"; .venv/bin/python -c 'from trading.server import create_app')
# Only static assets are served in production; do not retain a dependency copy
# in every release or traverse it again while preparing runtime permissions.
rm -rf -- "$stage/dashboard/node_modules"
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
step 'Switch service and check health'
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
printf '[upgrade] %s: %ss\n[upgrade] Total: %ss\n' "$step_name" "$((SECONDS - step_started))" "$((SECONDS - started))"
printf '\nAster Desk installed. http://127.0.0.1:8765 (use SSH tunnel or HTTPS reverse proxy)\n'
if [[ -n $new_password ]]; then printf 'Dashboard password: %s\n' "$new_password"; fi
printf 'Server environment: /etc/aster-desk/environment\nStatus: sudo aster-desk status\nGuide: %s/DEPLOYMENT.md\n' "$stage"
