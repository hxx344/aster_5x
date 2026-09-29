"""Configure independent Feishu channels locally; never send a notification."""
from contextlib import contextmanager
import getpass
import http.client
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import warnings


ENVIRONMENT = Path("/etc/aster-desk/environment")
# Coordinate both repeated configuration commands and the existing installer.
LOCK_FILE = Path("/opt/aster-desk/upgrade.lock")
CHANNELS = (("scheduled", "定时报表", "FEISHU_SCHEDULED"),
            ("event", "事件通知", "FEISHU_EVENT"))
KEYS = tuple(prefix + suffix for _, _, prefix in CHANNELS
             for suffix in ("_WEBHOOK_URL", "_SIGN_SECRET"))
ENV_LINE = re.compile(r"^[ \t]*([A-Z][A-Z0-9_]*)[ \t]*=(.*)$")
WEBHOOK = re.compile(r"https://open\.feishu\.cn/open-apis/bot/v2/hook/[A-Za-z0-9_-]{1,256}\Z")
MAX_ENVIRONMENT = 65536
MAX_SECRET = 256
SIGNALS = tuple(getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGHUP")
                if hasattr(signal, name))


class ConfigurationConflict(Exception):
    """Another configuration or installation must finish first."""


class ConfigurationInterrupted(BaseException):
    def __init__(self, signum=signal.SIGINT):
        self.signum = signum


class ConfigurationUpdateFailed(Exception):
    def __init__(self, recovered, *, interrupted=False):
        super().__init__("Feishu configuration update failed")
        self.recovered = recovered
        self.interrupted = interrupted


def validate_webhook(value):
    if not isinstance(value, str) or (value and not WEBHOOK.fullmatch(value)):
        raise ValueError("Invalid Feishu webhook")
    return value


def validate_secret(value):
    if (not isinstance(value, str) or len(value) > MAX_SECRET
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 or char in "\u2028\u2029" for char in value)):
        raise ValueError("Invalid Feishu signing secret")
    return value


def decode_value(raw):
    """Read one-line EnvironmentFile values, without evaluating shell content."""
    raw = raw.strip(" \t")
    if not raw:
        return ""
    if raw[0] == "'":
        end = raw.find("'", 1)
        if end < 0 or raw[end + 1:].strip(" \t"):
            raise ValueError("Invalid quoted environment value")
        return raw[1:end]
    quoted = raw[0] == '"'
    index = 1 if quoted else 0
    result = []
    while index < len(raw):
        char = raw[index]
        if quoted and char == '"':
            if raw[index + 1:].strip(" \t"):
                raise ValueError("Invalid quoted environment value")
            return "".join(result)
        if char == "\\":
            index += 1
            if index == len(raw):
                raise ValueError("Multiline environment values are not supported")
            if quoted and raw[index] not in '\\"$`':
                result.append("\\")
            char = raw[index]
        result.append(char)
        index += 1
    if quoted:
        raise ValueError("Unterminated environment value")
    return "".join(result)


def read_channel_values(text):
    values, counts = {}, {}
    for line in text.splitlines():
        match = ENV_LINE.fullmatch(line)
        if match and match[1] in KEYS:
            key = match[1]
            values[key] = decode_value(match[2])
            counts[key] = counts.get(key, 0) + 1
    return values, counts


def encode_value(value):
    # EnvironmentFile does not perform shell expansion. Escape only its quotes
    # and backslashes, preserving opaque secret bytes instead of JSON \u escapes.
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def update_environment(text, values):
    if set(values) != set(KEYS):
        raise ValueError("All four channel settings are required")
    for _, _, prefix in CHANNELS:
        validate_webhook(values[prefix + "_WEBHOOK_URL"])
        validate_secret(values[prefix + "_SIGN_SECRET"])
    previous, counts = read_channel_values(text)
    if previous == values and all(counts.get(key) == 1 for key in KEYS):
        return text
    retained = []
    for line in text.splitlines(keepends=True):
        match = ENV_LINE.fullmatch(line.rstrip("\r\n"))
        if not match or match[1] not in KEYS:
            retained.append(line)
    result = "".join(retained)
    if result and not result.endswith("\n"):
        result += "\n"
    return result + "".join(key + "=" + encode_value(values[key]) + "\n" for key in KEYS)


def collect_values(text, *, ask=getpass.getpass, tell=print):
    previous, _ = read_channel_values(text)
    if previous:
        tell("当前为独立机器人模式；未配置的通道不会使用旧共享机器人。")
    else:
        tell("将启用独立机器人模式，不会复制旧共享机器人；新通道留空即停用。旧配置行保留，但不再回退使用。")
    tell("输入均隐藏。URL 回车保留该独立通道，输入 - 停用；未改变 URL 时，签名回车保留，- 清空。")
    result = {}
    for _, label, prefix in CHANNELS:
        url_key, secret_key = prefix + "_WEBHOOK_URL", prefix + "_SIGN_SECRET"
        previous_url = previous.get(url_key, "")
        supplied_url = ask(label + "机器人 URL（隐藏）：")
        url = previous_url if supplied_url == "" else "" if supplied_url == "-" else supplied_url
        validate_webhook(url)
        if not url:
            tell(label + "通道停用，签名将清空。")
            secret = ""
        else:
            changed_url = url != previous_url
            if changed_url:
                tell(label + " URL 已改变，请重新输入新机器人的签名；留空表示不签名，不沿用旧密钥。")
            supplied_secret = ask(label + "签名密钥（可选、隐藏）：")
            secret = ("" if changed_url else previous.get(secret_key, "")) if supplied_secret == "" else (
                "" if supplied_secret == "-" else supplied_secret)
        result[url_key] = url
        result[secret_key] = validate_secret(secret)
    return result


def read_environment(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Environment must be a regular file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ENVIRONMENT:
            raise ValueError("Invalid environment file")
        data = stream.read(MAX_ENVIRONMENT + 1)
    if len(data) > MAX_ENVIRONMENT:
        raise ValueError("Environment file is too large")
    data.decode("utf-8")
    return data


def atomic_write(path, data):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.chmod(temporary, 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def interruption_handlers(*, ignore=False):
    saved = {}
    def interrupted(signum, _frame):
        raise ConfigurationInterrupted(signum)
    try:
        for signum in SIGNALS:
            saved[signum] = signal.getsignal(signum)
            signal.signal(signum, signal.SIG_IGN if ignore else interrupted)
        yield
    finally:
        for signum, handler in saved.items():
            signal.signal(signum, handler)


@contextmanager
def configuration_lock(path=LOCK_FILE):
    # Imported only for the Linux CLI; pure validation/tests also run on Windows.
    import fcntl
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ConfigurationConflict()
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigurationConflict() from None
        yield
    finally:
        os.close(descriptor)


def restart_desk(*, attempts=20):
    subprocess.run(["systemctl", "restart", "aster-desk"], check=True, timeout=30,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for attempt in range(attempts):
        connection = http.client.HTTPConnection("127.0.0.1", 8765, timeout=2)
        try:
            connection.request("GET", "/api/health")
            response = connection.getresponse()
            body = response.read(16385)
            payload = json.loads(body) if response.status == 200 and len(body) <= 16384 else None
            if isinstance(payload, dict) and payload.get("status") == "ok":
                subprocess.run(["systemctl", "is-active", "--quiet", "aster-desk"], check=True,
                               timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
        except (OSError, ValueError, http.client.HTTPException, subprocess.SubprocessError):
            pass
        finally:
            connection.close()
        if attempt + 1 < attempts:
            time.sleep(1)
    raise ValueError("Local Aster Desk health check failed")


def apply_configuration(path, values, *, restart=restart_desk, expected=None):
    path = Path(path)
    original = read_environment(path)
    if expected is not None and original != expected:
        raise ConfigurationConflict()
    updated = update_environment(original.decode("utf-8"), values).encode("utf-8")
    if updated == original:
        return False
    restart_started = False
    try:
        atomic_write(path, updated)
        restart_started = True
        restart()
        return True
    except BaseException as error:
        interrupted = isinstance(error, (ConfigurationInterrupted, KeyboardInterrupt))
        with interruption_handlers(ignore=True):
            try:
                # A signal can arrive after os.replace but before the call returns.
                changed = read_environment(path) != original
            except Exception:
                changed = True
            try:
                if changed:
                    atomic_write(path, original)
                if changed or restart_started:
                    restart()
                recovered = True
            except BaseException:
                recovered = False
        raise ConfigurationUpdateFailed(recovered, interrupted=interrupted) from None


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if arguments in (["--help"], ["-h"]):
        print("用法：sudo aster-desk feishu-configure（交互隐藏输入；只检查本机健康，不发送飞书消息）")
        return 0
    if arguments:
        print("此命令不接受凭据参数；请运行 sudo aster-desk feishu-configure。", file=sys.stderr)
        return 2
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        print("请在 Linux 服务器以 root 运行 sudo aster-desk feishu-configure。", file=sys.stderr)
        return 1
    if not sys.stdin.isatty():
        print("需要交互终端以隐藏输入，请在 SSH 终端运行此命令。", file=sys.stderr)
        return 1
    try:
        with interruption_handlers(), configuration_lock(), warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            original = read_environment(ENVIRONMENT)
            values = collect_values(original.decode("utf-8"))
            changed = apply_configuration(ENVIRONMENT, values, expected=original)
        print("双通道配置已更新，服务已重启并通过本机健康检查；未发送飞书消息。" if changed
              else "双通道配置未变化，未写文件、未重启服务。")
    except ConfigurationConflict:
        print("已有安装或配置操作正在进行，或配置文件已改变；本次未写入，请稍后重试。", file=sys.stderr)
        return 1
    except ConfigurationUpdateFailed as error:
        print("配置应用未完成；已恢复原配置及服务。" if error.recovered
              else "配置应用未完成且自动恢复未完成，请检查本机环境文件与服务状态。", file=sys.stderr)
        return 130 if error.interrupted else 1
    except (ConfigurationInterrupted, KeyboardInterrupt):
        print("配置操作已中断；请重新运行命令确认当前设置。", file=sys.stderr)
        return 130
    except Exception:
        # Input/network/process exceptions may embed tokens: never display them.
        print("配置未完成；请检查输入格式、环境文件和本机服务状态。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
