#!/usr/bin/env bash
set -Eeuo pipefail
umask 027

die() { printf '%s\n' "$*" >&2; exit 1; }
[[ $(uname -s) == Linux ]] || die '此安装脚本用于 Linux。'
[[ $EUID -eq 0 ]] || die '请使用 sudo bash install.sh，或以 root 运行。'
[[ -d /run/systemd/system ]] || die '需要运行 systemd 的 Linux 服务器。'
interactive=1
if [[ ${1:-} == --non-interactive ]]; then
    interactive=0
    shift
fi
[[ $# -eq 0 ]] || die '用法：sudo bash install.sh [--non-interactive]'

missing=0
for dependency in python3 curl tar; do
    command -v "$dependency" >/dev/null || missing=1
done
if [[ $missing -eq 1 || ! -x /usr/bin/python3 ]]; then
    if command -v apt-get >/dev/null; then
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y python3 curl tar ca-certificates
    elif command -v dnf >/dev/null; then
        dnf install -y python3 curl tar ca-certificates
    elif command -v yum >/dev/null; then
        yum install -y python3 curl tar ca-certificates
    else
        die '请先安装 Python 3.9+、curl、tar 和 CA 证书，然后重试。'
    fi
fi
/usr/bin/python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || die '需要 /usr/bin/python3 版本至少 3.9；推荐 Ubuntu 22.04+、Debian 12+ 或 RHEL 9+。'

work=$(mktemp -d /tmp/aster-5x-install.XXXXXXXX)
trap 'rm -rf -- "$work"' EXIT
script=${BASH_SOURCE[0]:-}
source_dir=''
if [[ -n $script && -f $script ]]; then
    source_dir=$(cd -- "$(dirname -- "$script")" && pwd)
fi
if [[ -z $source_dir || ! -f $source_dir/monitor.py || ! -f $source_dir/manage.py ]]; then
    printf '%s\n' '下载 Aster 监控程序…'
    curl --fail --silent --show-error --location --retry 3 --connect-timeout 15 --max-time 120 \
        'https://github.com/hxx344/aster_5x/archive/refs/heads/main.tar.gz' -o "$work/source.tar.gz"
    mkdir "$work/source"
    tar -xzf "$work/source.tar.gz" --strip-components=1 -C "$work/source"
    source_dir=$work/source
fi
for filename in monitor.py manage.py config.json deploy/aster-5x.service; do
    [[ -f $source_dir/$filename ]] || die "安装文件缺失：$filename"
done
/usr/bin/python3 - "$source_dir" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
for name in ('monitor.py', 'manage.py'):
    compile((root / name).read_text(encoding='utf-8'), name, 'exec')
json.loads((root / 'config.json').read_text(encoding='utf-8'))
PY

if ! getent passwd aster-5x >/dev/null; then
    useradd --system --user-group --home-dir /var/lib/aster-5x --no-create-home --shell /usr/sbin/nologin aster-5x
fi
install -d -m 755 /opt/aster-5x
install -d -m 750 -o root -g aster-5x /etc/aster-5x
install -d -m 750 -o aster-5x -g aster-5x /var/lib/aster-5x
first_install=0
if [[ ! -f /etc/aster-5x/config.json ]]; then
    printf '{}\n' > "$work/config.json"
    install -m 640 -o root -g aster-5x "$work/config.json" /etc/aster-5x/config.json
    first_install=1
fi
# Validate preserved configuration before stopping a working service.
ASTER_CONFIG_FILE=/etc/aster-5x/config.json /usr/bin/python3 - "$source_dir" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import monitor
monitor.load_config()
PY
if systemctl is-active --quiet aster-5x.service; then
    systemctl stop aster-5x.service
fi
for filename in monitor.py manage.py config.json; do
    install -m 644 "$source_dir/$filename" "/opt/aster-5x/$filename"
done
install -m 644 "$source_dir/deploy/aster-5x.service" /etc/systemd/system/aster-5x.service
cat > "$work/aster-5x" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
export ASTER_CONFIG_FILE=/etc/aster-5x/config.json
export ASTER_RUNTIME_DIR=/var/lib/aster-5x
exec /usr/bin/python3 /opt/aster-5x/manage.py "$@"
SH
install -m 755 "$work/aster-5x" /usr/local/bin/aster-5x
systemctl daemon-reload
if [[ $interactive -eq 1 && $first_install -eq 1 ]] && { true </dev/tty; } 2>/dev/null; then
    /usr/local/bin/aster-5x configure --no-restart </dev/tty
else
    printf '%s\n' '保留已有配置；首次安装默认阈值 10000、每 5 秒检查、飞书关闭。'
fi
systemctl enable aster-5x.service
/usr/local/bin/aster-5x restart
printf '\n%s\n' '安装完成。修改配置：sudo aster-5x configure' \
    '查看状态：sudo aster-5x status' '查看日志：sudo aster-5x logs' \
    '服务已设置开机启动，进程异常退出时自动重启。'
