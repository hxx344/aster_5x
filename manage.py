"""Small Linux administration CLI installed as aster-5x."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import getpass
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

import monitor as m

SERVICE = "aster-5x.service"


def config_path():
    return Path(os.environ.get("ASTER_CONFIG_FILE", "/etc/aster-5x/config.json"))


def require_root():
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise m.MonitorError("请使用 sudo aster-5x 执行此命令。")


def systemctl(*args, check=True):
    return subprocess.run(["systemctl", *args, SERVICE], check=check, capture_output=True, text=True)


def save_config(path, config):
    """Validate before replacing; preserve owner/group and keep secrets private."""
    defaults = json.loads((m.ROOT / "config.json").read_text(encoding="utf-8"))
    m.validate_config({**defaults, **config})
    parent = path.parent
    fd, temporary = tempfile.mkstemp(prefix=".config-", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(config, out, ensure_ascii=False, indent=2, default=str)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
            os.fchmod(out.fileno(), 0o640)
            stat = path.stat() if path.exists() else parent.stat()
            os.fchown(out.fileno(), stat.st_uid, stat.st_gid)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def configure(restart=True):
    require_root()
    path = config_path()
    overrides = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    current = m.load_config()
    print(f"监控 {', '.join(current['symbols'])} · {', '.join(str(v) + 'x' for v in current['leverages'])}；按回车保留当前设置。")
    threshold = input(f"提醒阈值 USD1 [{current['threshold']}]：").strip()
    interval = input(f"检查间隔秒，最少 5 [{current['poll_seconds']:g}]：").strip()
    if threshold:
        overrides["threshold"] = str(m.number(threshold))
    if interval:
        overrides["poll_seconds"] = float(m.number(interval))
    state = "已启用" if current["feishu_enabled"] else "已关闭"
    webhook = getpass.getpass(f"飞书 Webhook（{state}；回车保留，输入 - 关闭；输入隐藏）：").strip()
    if webhook == "-":
        overrides.update(feishu_enabled=False, feishu_webhook="", feishu_sign_secret="")
    elif webhook:
        secret = getpass.getpass("飞书签名密钥（未开启签名校验请直接回车；输入隐藏）：").strip()
        overrides.update(feishu_enabled=True, feishu_webhook=webhook, feishu_sign_secret=secret)
    save_config(path, overrides)
    print(f"配置已保存：{path}")
    if restart:
        systemctl("restart")
        return wait_ready()
    return 0


def read_status():
    try:
        return json.loads((m.runtime_dir() / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def fresh(status):
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(status["checked_at"])).total_seconds()
        return 0 <= age <= 60
    except (KeyError, TypeError, ValueError):
        return False


def show_status():
    active = systemctl("is-active", check=False).stdout.strip()
    status = read_status()
    print(f"服务：{active or 'unknown'}")
    config = m.load_config()
    print(f"规则：{', '.join(str(v) + 'x' for v in config['leverages'])}，> {config['threshold']:,.2f} USD1，每个标的每 {config['poll_seconds']:g} 秒")
    print(f"飞书：{'已启用' if config['feishu_enabled'] else '关闭，仅本地记录'}")
    markets = status.get("markets", {status.get("symbol"): status})
    healthy = active == "active" and status.get("status") == "ok"
    for symbol, leverage in ((s, v) for s in config["symbols"] for v in config["leverages"]):
        row = markets.get(m.market_key(symbol, leverage), {})
        value = f"{Decimal(row['value']):,.2f} USD1" if "value" in row else "无数据"
        print(f"{symbol} {leverage}x：{value}；状态 {row.get('status', '尚无记录')}；{'最近一分钟内' if fresh(row) else '尚无新鲜数据'}")
        print(f"  最近成功检查：{row.get('checked_at', '无')}")
        if row.get("error"):
            print(f"  最近错误：{row['error']}")
        healthy = healthy and row.get("status") == "ok" and fresh(row)
    return 0 if healthy else 1


def wait_ready(timeout=30):
    pid = systemctl("show", "--property=MainPID", "--value", check=False).stdout.strip()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = read_status()
        markets = status.get("markets", {status.get("symbol"): status})
        if str(status.get("pid")) == pid and markets and all(row.get("status") in ("ok", "error") for row in markets.values()):
            return show_status()
        time.sleep(0.5)
    print("服务已启动，但尚未完成首次查询。请执行 sudo aster-5x logs 排查。")
    return 1


def main():
    parser = argparse.ArgumentParser(description="Aster Linux 监控管理")
    parser.add_argument("command", choices=["configure", "status", "logs", "start", "stop", "restart", "test-feishu"])
    parser.add_argument("--no-restart", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    # Explicit environment paths let tests use isolated directories.
    os.environ.setdefault("ASTER_CONFIG_FILE", str(config_path()))
    os.environ.setdefault("ASTER_RUNTIME_DIR", "/var/lib/aster-5x")
    try:
        if args.command == "configure":
            return configure(not args.no_restart)
        if args.command == "status":
            return show_status()
        if args.command == "logs":
            return subprocess.call(["journalctl", "-u", SERVICE, "-n", "50", "-f", "--no-pager"])
        require_root()
        if args.command == "test-feishu":
            config = m.load_config()
            if not config["feishu_enabled"]:
                raise m.MonitorError("飞书尚未启用，请先运行 sudo aster-5x configure。")
            m.send_feishu(config, "Aster 监控测试消息：Linux 服务器飞书推送连接成功。这是一条测试消息，并非额度达标提醒。")
            print("测试消息已被飞书确认接收。")
            return 0
        systemctl(args.command)
        if args.command == "stop":
            print("监控已停止；执行 sudo aster-5x start 恢复。")
            return 0
        return wait_ready()
    except (m.MonitorError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        if isinstance(exc, m.MonitorError):
            print(f"错误：{exc}")
        else:
            print("操作失败，请检查配置文件权限、JSON 格式或执行 sudo systemctl status aster-5x。")
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\n已取消；未完成的配置不会保存。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
