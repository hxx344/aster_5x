#!/usr/bin/env bash
# Build before switching the running Linux service; preserve credentials and SQLite.
set -euo pipefail
umask 077
cleanup_only=0
case ${1:-} in
  --cleanup) cleanup_only=1; shift ;;
  --help|-h) printf 'Usage: install-trading.sh [--cleanup]\n--cleanup: reclaim old releases and unused build caches without restarting.\n'; exit 0 ;;
esac
[[ $# -eq 0 ]] || { printf 'Unknown argument. Use --help.\n' >&2; exit 1; }
[[ ${EUID} -eq 0 ]] || { printf 'Run with sudo or as root.\n' >&2; exit 1; }
command -v systemctl >/dev/null || { printf 'systemd is required.\n' >&2; exit 1; }
[[ -d /run/systemd/system ]] || { printf 'systemd must be running.\n' >&2; exit 1; }
root=/opt/aster-desk
stage='' scratch='' old='' storage_running='' source_revision=''
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-/nonexistent}")" 2>/dev/null && pwd || true)
storage_gc() {
  local pressure=${1:-0} active_stage=${2-$stage} pid data_path
  if ! pid=$(systemctl show --property=MainPID --value aster-desk 2>/dev/null); then
    [[ ! -e $root/current && ! -f /etc/systemd/system/aster-desk.service ]] || { printf 'Cannot inspect installed service; cleanup skipped.\n' >&2; return 1; }
  fi
  storage_running=''
  if [[ $pid =~ ^[1-9][0-9]*$ ]]; then
    storage_running=$(readlink -f "/proc/$pid/cwd") || return 1
  elif [[ $pid != 0 && ( -n $pid || -e $root/current || -f /etc/systemd/system/aster-desk.service ) ]]; then
    printf 'Cannot identify the running service; storage cleanup skipped.\n' >&2; return 1
  fi
  data_path=/var/lib/aster-desk
  if [[ -f /etc/aster-desk/environment ]]; then
    data_path=$(systemd-run --quiet --wait --pipe --collect --unit="aster-desk-storage-$$" \
      --property=EnvironmentFile=/etc/aster-desk/environment \
      --setenv=ASTER_TRADING_RUNTIME=/var/lib/aster-desk /usr/bin/printenv ASTER_TRADING_RUNTIME) || return 1
    [[ $data_path == /* ]] || { printf 'Runtime directory must be absolute; cleanup skipped.\n' >&2; return 1; }
  fi
  python3 - "$root" "$storage_running" "$source_dir" "$active_stage" "$old" "$data_path" "$pressure" <<'STORAGE_PY'
# STORAGE_BEGIN: extracted by native fixture tests, never starts the application.
import os, re, shutil, sys
from pathlib import Path

root = Path(sys.argv[1]).absolute()
running, source, stage, previous, data = [Path(p).resolve() if p else None for p in sys.argv[2:7]]
data_requested = Path(os.path.abspath(sys.argv[6])) if sys.argv[6] else None
pressure = sys.argv[7] == '1'
if root.is_symlink() or root.resolve() != root:
    raise SystemExit('Refusing storage cleanup through a linked installation root')
releases, cache = root / 'releases', root / 'cache'
current = (root / 'current').resolve() if (root / 'current').exists() else None
release_pattern = re.compile(r'release-\d{8}T\d{6}Z-[A-Za-z0-9]{6}')
cache_pattern = re.compile(r'(python-env|npm-deps|frontend-build)-[A-Za-z0-9]{6}')
alias_pattern = re.compile(r'(python|npm|frontend)-[a-f0-9]{64}')
user_references = []
for directory, pattern in ((releases, release_pattern), (cache, alias_pattern)):
    if directory.is_dir() and not directory.is_symlink() and directory.resolve() == directory:
        for path in directory.iterdir():
            if path.is_symlink() and not pattern.fullmatch(path.name):
                user_references.append(path.resolve())
mounts = set()
if sys.platform == 'linux':
    # st_dev alone cannot recognize bind mounts on the same filesystem.
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        encoded = line.split()[4]
        mounts.add(Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), encoded)))

def within(path, parent):
    return path == parent or parent in path.parents

def real_directory(path):
    return path.is_dir() and not path.is_symlink() and path.resolve() == path

def protected(path):
    return any(p and within(p, path) for p in (current, running, source, stage, *user_references)) or any(p and (within(path, p) or within(p, path)) for p in (data, data_requested))

def managed_release(path):
    return bool(release_pattern.fullmatch(path.name) and real_directory(path) and
                ((path / '.install-owned').is_file() or all((path / p).is_file() for p in
                 ('trading/server.py', 'dashboard/package-lock.json', 'deploy/aster-desk.service'))))

def complete(path):
    return (path / '.install-ready').is_file()

def legacy_complete(path):
    return not (path / '.install-owned').exists() and (path / '.venv/bin/python').is_file() and (path / 'dashboard/dist/client/index.html').is_file()

def timestamp(path):
    marker = path / '.install-ready'
    return (marker if marker.is_file() else path).stat().st_mtime_ns, path.name

def removable_tree(path):
    # A nested mount/junction is not part of an installer artifact. Do not cross it.
    if os.path.ismount(path) or any(within(mount, path) for mount in mounts):
        return False
    device = path.stat().st_dev
    for directory, dirs, _ in os.walk(path, followlinks=False):
        for name in dirs:
            child = Path(directory) / name
            if not child.is_symlink() and (os.path.ismount(child) or child.stat().st_dev != device or getattr(child, 'is_junction', lambda: False)()):
                return False
    return True

removed = 0
def remove(path):
    global removed
    if protected(path) or not removable_tree(path):
        return
    shutil.rmtree(path)
    removed += 1

if real_directory(releases):
    candidates = [p for p in releases.iterdir() if managed_release(p)]
    backups = [p for p in candidates if p != current and complete(p)]
    # On the first upgrade from the old installer, retain one complete legacy
    # candidate conservatively; only new health-checked installs get ready markers.
    if not backups:
        backups = [p for p in candidates if p != current and legacy_complete(p)]
    keep = previous if previous in backups else max(backups, key=timestamp, default=None)
    for path in candidates:
        if path != keep:
            remove(path)

if real_directory(cache):
    referenced = set()
    # Include unrecognized/linked releases too: their environment may still be used.
    if real_directory(releases):
        for release in releases.iterdir():
            if (release / '.venv').exists():
                referenced.add((release / '.venv').resolve())
    for location in (current, running, source, stage):
        if location:
            for ancestor in (location, *location.parents):
                if ancestor == root.parent:
                    break
                if (ancestor / '.venv').exists():
                    referenced.add((ancestor / '.venv').resolve())
    candidates = []
    for path in cache.iterdir():
        match = cache_pattern.fullmatch(path.name)
        if not match or not real_directory(path):
            continue
        kind = match[1]
        legacy = ((kind == 'python-env' and (path / 'pyvenv.cfg').is_file() and (path / 'bin/python').is_file()) or
                  (kind == 'npm-deps' and (path / '.complete').is_file() and (path / 'node_modules').is_dir()) or
                  (kind == 'frontend-build' and (path / '.complete').is_file() and (path / 'client/index.html').is_file()))
        if not (path / '.install-owned').is_file() and not legacy:
            continue
        if any(within(reference, path) for reference in referenced) or protected(path):
            continue
        candidates.append((path, kind))
    # At most one unused completed cache of each type, at most 1 GiB combined.
    # Referenced environments are mandatory runtime assets and exempt from this cap.
    retained, budget = set(), (0 if pressure else 1024 * 1024 * 1024)
    for path, kind in sorted(candidates, key=lambda item: timestamp(item[0]), reverse=True):
        size = sum((Path(directory) / name).lstat().st_size for directory, _, names in os.walk(path, followlinks=False) for name in names)
        if not pressure and (path / '.complete').is_file() and kind not in retained and size <= budget:
            retained.add(kind)
            budget -= size
        else:
            remove(path)
    for alias in cache.iterdir():
        if not alias_pattern.fullmatch(alias.name) or not alias.is_symlink() or protected(alias):
            continue
        target = alias.resolve()
        if target.parent == cache and cache_pattern.fullmatch(target.name) and not target.exists():
            alias.unlink()
print(f'[storage] Reclaimed {removed} obsolete release/cache directories; running and rollback environments preserved.')
# STORAGE_END
STORAGE_PY
}
require_space() {
  local path=$1 needed_kb=$2 needed_inodes=$3 free_kb free_inodes pass
  while [[ ! -e $path && $path != / ]]; do path=${path%/*}; [[ -n $path ]] || path=/; done
  for pass in 1 2; do
    free_kb=$(df -Pk -- "$path" | awk 'END {print $4}')
    free_inodes=$(df -Pi -- "$path" | awk 'END {print $4}')
    [[ $free_kb =~ ^[0-9]+$ && ( $free_inodes =~ ^[0-9]+$ || $free_inodes == - ) ]] || { printf 'Cannot read available disk space.\n' >&2; return 1; }
    if ((free_kb >= needed_kb)) && { [[ $free_inodes == - ]] || ((free_inodes >= needed_inodes)); }; then return 0; fi
    if [[ $pass == 1 ]] && command -v python3 >/dev/null; then storage_gc 1 || return 1; fi
  done
  printf 'Insufficient disk space/inodes: %s has %s MiB and %s inodes; need %s MiB and %s inodes. Existing service not switched.\n' "$path" "$((free_kb / 1024))" "$free_inodes" "$((needed_kb / 1024))" "$needed_inodes" >&2
  return 1
}
installed_state() {
  sha256sum /etc/aster-desk/environment /etc/systemd/system/aster-desk.service /usr/local/bin/aster-desk | sha256sum | cut -d ' ' -f1
}
started=$SECONDS
step_started=$SECONDS
step_name=''
step() {
  if [[ -n $step_name ]]; then printf '[upgrade] %s: %ss\n' "$step_name" "$((SECONDS - step_started))"; fi
  step_name=$1
  step_started=$SECONDS
  printf '[upgrade] %s...\n' "$step_name"
}
mkdir -p "$root"
# Lock before collection: another installer may still be building in a cache.
exec 9>"$root/upgrade.lock"
flock -n 9 || { printf 'Another Aster Desk upgrade is running.\n' >&2; exit 1; }
if [[ -L $root/current && -d $root/current ]]; then old=$(readlink -f "$root/current"); fi
if command -v python3 >/dev/null; then storage_gc; fi
if ((cleanup_only)); then
  command -v python3 >/dev/null || { printf 'Python is required for safe cleanup.\n' >&2; exit 1; }
  storage_gc 1
  df -h "$root" /tmp /var
  df -i "$root" /tmp /var
  exit 0
fi
step 'Check system dependencies'
system_ready() {
  local tool
  for tool in python3 curl tar xz sha256sum flock; do command -v "$tool" >/dev/null || return 1; done
  python3 -c 'import sys, venv, ensurepip, ssl, os; assert sys.version_info >= (3, 10); assert os.path.isfile(ssl.get_default_verify_paths().cafile or "")' 2>/dev/null
}
if ! system_ready; then
  require_space /var 524288 10000
  if command -v apt-get >/dev/null; then
    apt-get update -qq
    apt-get install -y -qq python3 python3-venv curl ca-certificates tar xz-utils util-linux
  elif command -v dnf >/dev/null; then
    dnf install -y python3 python3-pip curl ca-certificates tar xz util-linux
  fi
fi
system_ready || { printf 'Python 3.10+, venv, CA certificates, curl, tar, xz and flock are required.\n' >&2; exit 1; }
mkdir -p "$root/releases" "$root/cache" /etc/aster-desk /var/lib/aster-desk
chmod 755 "$root" "$root/releases" "$root/cache"
switched=0
was_active=0
systemctl is-active --quiet aster-desk && was_active=1
had_unit=0
if [[ -f /etc/systemd/system/aster-desk.service ]]; then
  had_unit=1
fi
cleanup() {
  result=$?
  # Once rollback starts, repeated termination signals must not leave it half-done.
  trap '' HUP INT TERM
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
    printf 'Upgrade failed; previous service restored.\n' >&2
  fi
  # Do not mask the original result, and never delete a release still running
  # after an unsuccessful rollback. Collection rereads MainPID/current first.
  if [[ $result -ne 0 ]]; then storage_gc 0 '' || true; fi
  if [[ -n $scratch && $scratch == "$root/.source-"* && ! -L $scratch ]]; then rm -rf --one-file-system -- "$scratch"; fi
  exit "$result"
}
trap cleanup EXIT
# Bash does not run an EXIT trap when an unhandled signal terminates it.
# Route interrupted upgrades through the same rollback as a failed command.
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
step 'Download source'
if [[ ! -f $source_dir/trading/server.py || ! -f $source_dir/dashboard/package-lock.json ]]; then
  source_revision=$(curl --retry 3 -fsSL https://api.github.com/repos/hxx344/aster_5x/commits/main | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])')
  [[ $source_revision =~ ^[a-f0-9]{40}$ ]] || { printf 'Invalid source revision.\n' >&2; exit 1; }
  if [[ -n $old && -f $old/.install-ready && $(cat "$old/.install-revision" 2>/dev/null || true) == "$source_revision" ]]; then
    source_dir=$old
    printf '[upgrade] Source revision unchanged; skipping archive download\n'
  else
    require_space "$root" 262144 5000
    scratch=$(mktemp -d "$root/.source-XXXXXX")
    curl --retry 3 -fsSL "https://codeload.github.com/hxx344/aster_5x/tar.gz/$source_revision" -o "$scratch/source.tar.gz"
    mkdir "$scratch/source"
    tar -xzf "$scratch/source.tar.gz" -C "$scratch/source" --strip-components=1
    rm "$scratch/source.tar.gz"
    source_dir=$scratch/source
  fi
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
  require_space "$root" 524288 15000
  case "$(uname -m)" in x86_64) arch=x64 ;; aarch64|arm64) arch=arm64 ;; *) printf 'Unsupported Node architecture\n' >&2; exit 1 ;; esac
  archive="node-$version-linux-$arch.tar.xz"
  mkdir -p "$root/node-$version"
  [[ -n $scratch ]] || scratch=$(mktemp -d "$root/.source-XXXXXX")
  curl --retry 3 -fsSL "https://nodejs.org/dist/$version/$archive" -o "$scratch/$archive"
  curl --retry 3 -fsSL "https://nodejs.org/dist/$version/SHASUMS256.txt" -o "$scratch/SHASUMS256.txt"
  (cd "$scratch"; awk -v filename="$archive" '$2 == filename' SHASUMS256.txt | sha256sum --check --status)
  tar -xJf "$scratch/$archive" -C "$root/node-$version" --strip-components=1
  rm "$scratch/$archive" "$scratch/SHASUMS256.txt"
  export PATH="$root/node-$version/bin:$PATH"
fi
node_ready || { printf 'Node or npm is unavailable.\n' >&2; exit 1; }
node_identity=$(node -p 'JSON.stringify({version:process.version,abi:process.versions.modules,platform:process.platform,arch:process.arch})')
npm_identity=$(npm --version)
# Hash paths and contents, including public assets/configuration, not mtimes.
# Bump the recipe prefix when changing how dependencies or assets are prepared.
fingerprints=$(python3 - "$source_dir" "$node_identity" "$npm_identity" <<'PY'
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
release_files = [source / name for name in ('requirements.txt', 'requirements.lock', 'monitor.py', 'config.json', 'DEPLOYMENT.md', 'install-trading.sh')]
release_files += files
for folder in ('trading', 'deploy'):
    for directory, dirs, names in os.walk(source / folder):
        dirs[:] = [name for name in dirs if name not in ignored]
        release_files.extend(Path(directory) / name for name in names if not name.startswith('.env'))
print(digest('release-v2:' + python_key + frontend_key, release_files))
PY
)
mapfile -t keys <<< "$fingerprints"
if [[ -n $old && -f $old/.install-ready && -f $old/.venv/.complete && -s $old/dashboard/dist/client/index.html &&
      $(cat "$old/.install-key" 2>/dev/null || true) == "${keys[3]}" &&
      $(cat "$old/.install-state" 2>/dev/null || true) == "$(installed_state 2>/dev/null || true)" &&
      $storage_running == "$old" ]] &&
    systemctl is-active --quiet aster-desk && systemctl is-enabled --quiet aster-desk &&
    [[ $(systemctl show --property=NeedDaemonReload --value aster-desk) == no ]] &&
    curl --max-time 2 -fsS http://127.0.0.1:8765/api/health 2>/dev/null | python3 -c 'import json,sys; assert json.load(sys.stdin)["status"] == "ok"' 2>/dev/null; then
  printf '[upgrade] Code, dependencies and service configuration unchanged; skipped build, release creation and restart.\n'
  exit 0
fi
# Reserve ordinary copies (reflinks are an optional optimization), a new Python
# environment, npm cache and frontend build. Never stop the old service to build.
require_space "$root" 2621440 140000
require_space /tmp 262144 5000
stage=$(mktemp -d "$root/releases/release-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
touch "$stage/.install-owned"
if ((had_unit)); then cp /etc/systemd/system/aster-desk.service "$stage/previous.service"; fi
tar -C "$source_dir" --exclude='*/node_modules' --exclude='*/dist' --exclude='*/.wrangler' --exclude='*/.vinext' \
  --exclude='*/__pycache__' --exclude='*/.env*' --exclude='*/.next' --exclude='*/tsconfig.tsbuildinfo' \
  -cf - trading dashboard requirements.txt requirements.lock monitor.py config.json deploy DEPLOYMENT.md install-trading.sh | tar -C "$stage" -xf -
step 'Prepare Python dependencies'
python_cache="$root/cache/python-${keys[0]}"
if [[ -f $python_cache/.complete && -x $python_cache/bin/python ]]; then
  python_env=$(readlink -f "$python_cache")
  printf '[upgrade] Reuse Python dependencies\n'
else
  # Keep this original path: moving an installed venv breaks script shebangs.
  python_env=$(mktemp -d "$root/cache/python-env-XXXXXX")
  touch "$python_env/.install-owned"
  python3 -m venv "$python_env"
  "$python_env/bin/python" -m pip install --no-cache-dir --disable-pip-version-check -q -r "$stage/requirements.lock"
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
    (cd "$stage/dashboard"; npm ci --include=dev --no-audit --no-fund --prefer-offline --cache "$stage/.npm")
    rm -rf --one-file-system -- "$stage/.npm"
    npm_dir=$(mktemp -d "$root/cache/npm-deps-XXXXXX")
    touch "$npm_dir/.install-owned"
    cp -a --reflink=auto "$stage/dashboard/node_modules" "$npm_dir/node_modules"
    touch "$npm_dir/.complete"
    ln -sfn "$npm_dir" "$npm_cache"
  fi
  step 'Check and build dashboard'
  (cd "$stage/dashboard"; npm run typecheck; npm run lint; npm run build)
  test -s "$stage/dashboard/dist/client/index.html"
  frontend_dir=$(mktemp -d "$root/cache/frontend-build-XXXXXX")
  touch "$frontend_dir/.install-owned"
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
require_space "$root" 65536 1000
switched=1
if [[ $was_active -eq 1 ]]; then systemctl stop aster-desk; fi
ln -sfn "$stage" "$root/current.next"
mv -Tf "$root/current.next" "$root/current"
install -m 644 "$stage/deploy/aster-desk.service" /etc/systemd/system/aster-desk.service
install -m 755 "$stage/deploy/aster-desk" /usr/local/bin/aster-desk
systemctl daemon-reload
systemctl enable --now aster-desk
healthy=0
# systemd can report active before the HTTP listener is ready. Retry quietly;
# only the final timeout is an upgrade failure, with service logs below.
for attempt in $(seq 1 30); do
  if systemctl is-active --quiet aster-desk && curl --max-time 2 -fsS http://127.0.0.1:8765/api/health 2>/dev/null | python3 -c 'import json,sys; assert json.load(sys.stdin)["status"] == "ok"' 2>/dev/null; then
    healthy=1; break
  fi
  sleep 2
done
if [[ $healthy -ne 1 ]]; then
  printf '[upgrade] Service health check failed after 30 attempts; restoring the previous service. Recent service logs:\n' >&2
  journalctl -u aster-desk --no-pager -n 15
  exit 1
fi
switched=0
printf '%s\n' "${keys[3]}" > "$stage/.install-key"
printf '%s\n' "$source_revision" > "$stage/.install-revision"
installed_state > "$stage/.install-state"
touch "$stage/.install-ready"
storage_gc
if systemctl list-unit-files aster-5x.service --no-legend 2>/dev/null | grep -q '^aster-5x.service'; then
  systemctl disable --now aster-5x
fi
printf '[upgrade] %s: %ss\n[upgrade] Total: %ss\n' "$step_name" "$((SECONDS - step_started))" "$((SECONDS - started))"
printf '\nAster Desk installed. http://127.0.0.1:8765 (use SSH tunnel or HTTPS reverse proxy)\n'
if [[ -n $new_password ]]; then printf 'Dashboard password: %s\n' "$new_password"; fi
printf 'Server environment: /etc/aster-desk/environment\nStatus: sudo aster-desk status\nGuide: %s/DEPLOYMENT.md\n' "$stage"
