"""Local relay setup and authenticated connection import; never execute input."""
import argparse
import getpass
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

SERVER_DIR = Path("/etc/aster-capacity-relay")
DESK_DIR = Path("/etc/aster-desk")
KEYS = ("ASTER_CAPACITY_RELAY_URL", "ASTER_CAPACITY_RELAY_TOKEN", "ASTER_CAPACITY_RELAY_CA_FILE")
ENV_LINE = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=(.*)$")
MAX_CONFIG = 65536


def atomic_write(path, data, mode=0o600):
    path = Path(path)
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd, name = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_environment(path):
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        match = ENV_LINE.fullmatch(line)
        if match:
            words = shlex.split(match[2], comments=False, posix=True)
            values[match[1]] = " ".join(words)
    return values


def update_environment(text, values):
    # Preserve unrelated lines, comments and duplicate unrelated settings exactly.
    retained = []
    for line in text.splitlines(keepends=True):
        match = ENV_LINE.fullmatch(line.rstrip("\r\n"))
        if not match or match[1] not in KEYS:
            retained.append(line)
    result = "".join(retained)
    if values:
        if result and not result.endswith("\n"):
            result += "\n"
        result += "".join(key + "=" + json.dumps(values[key], ensure_ascii=True) + "\n" for key in KEYS)
    return result


def validate_connection(raw):
    if len(raw) > MAX_CONFIG:
        raise ValueError("Connection configuration is too large")
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != {"url", "token", "ca_pem"}:
        raise ValueError("Expected a connection JSON with url, token and ca_pem")
    if not all(isinstance(data[key], str) for key in data):
        raise ValueError("Connection fields must be strings")
    if any(ord(char) <= 32 or ord(char) >= 127 for char in data["url"]):
        raise ValueError("Relay URL must contain only visible ASCII characters")
    parsed = urlsplit(data["url"])
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in ("", "/")
            or not re.fullmatch(r"[A-Za-z0-9.:[\]-]+", parsed.netloc)):
        raise ValueError("Relay URL must be an HTTPS origin without credentials or a path")
    port = parsed.port  # Validate the port, including its range.
    if port == 0 or len(parsed.hostname) > 253:
        raise ValueError("Invalid relay address")
    if not re.fullmatch(r"[!-~]{32,256}", data["token"]):
        raise ValueError("Invalid relay token")
    pem = data["ca_pem"]
    if (len(pem) > 32768 or "PRIVATE KEY" in pem or not pem.startswith("-----BEGIN CERTIFICATE-----\n")
            or not pem.rstrip().endswith("-----END CERTIFICATE-----")):
        raise ValueError("Invalid relay CA certificate")
    context = ssl.create_default_context()
    context.load_verify_locations(cadata=pem)
    data["url"] = data["url"].rstrip("/")
    return data


def request_health(config, *, local=False, path="/health"):
    parsed = urlsplit(config["url"])
    context = ssl.create_default_context()
    context.load_verify_locations(cadata=config["ca_pem"])
    conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=5, context=context)
    try:
        if local:
            target = "::1" if ":" in parsed.hostname else "127.0.0.1"
            conn.sock = context.wrap_socket(socket.create_connection((target, parsed.port or 443), 5),
                                           server_hostname=parsed.hostname)
        conn.request("GET", path, headers={"Authorization": "Bearer " + config["token"], "Accept": "application/json"})
        response = conn.getresponse()
        raw = response.read(MAX_CONFIG + 1)
        if response.status != 200 or len(raw) > MAX_CONFIG:
            raise ValueError("Relay health check failed")
        payload = json.loads(raw)
        if path == "/health" and (not isinstance(payload, dict) or payload.get("status") != "ok"):
            raise ValueError("Relay is not healthy")
        return payload
    finally:
        conn.close()


def server_connection(directory=SERVER_DIR):
    directory = Path(directory)
    env = read_environment(directory / "environment")
    address = str(ipaddress.ip_address((directory / "address").read_text().strip()))
    host = "[" + address + "]" if ":" in address else address
    return validate_connection(json.dumps({"url": f"https://{host}:{int(env['ASTER_RELAY_PORT'])}",
        "token": env["ASTER_RELAY_TOKEN"], "ca_pem": Path(env["ASTER_RELAY_CERT_FILE"]).read_text()}))


def prepare_server(directory, address):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    previous_address = directory / "address"
    if previous_address.exists():
        saved = str(ipaddress.ip_address(previous_address.read_text().strip()))
        if address and str(ipaddress.ip_address(address)) != saved:
            raise ValueError("Existing address and certificate are preserved; --address must match the installed address")
        address = saved
    elif not address:
        raise ValueError("First installation requires --address with the relay server's public IP")
    address = str(ipaddress.ip_address(address))
    cert, key = directory / "server.crt", directory / "server.key"
    if cert.exists() != key.exists():
        raise ValueError("Existing certificate/key pair is incomplete; installation has not replaced it")
    if not cert.exists():
        with tempfile.TemporaryDirectory(prefix=".tls-", dir=directory) as temporary:
            scratch = Path(temporary)
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes",
                "-days", "3650", "-subj", "/CN=Aster capacity relay", "-addext", "subjectAltName=IP:" + address,
                "-addext", "basicConstraints=critical,CA:FALSE", "-addext", "extendedKeyUsage=serverAuth",
                "-addext", "keyUsage=critical,digitalSignature,keyEncipherment",
                "-keyout", str(scratch / "key"), "-out", str(scratch / "cert")],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            atomic_write(key, (scratch / "key").read_bytes())
            atomic_write(cert, (scratch / "cert").read_bytes(), 0o644)
    ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key)
    # A reused certificate must still cover the chosen IP address.
    decoded = ssl._ssl._test_decode_cert(str(cert))
    if not any(kind == "IP Address" and ipaddress.ip_address(value) == ipaddress.ip_address(address)
               for kind, value in decoded.get("subjectAltName", ())):
        raise ValueError("Existing certificate does not cover the relay address")
    if not previous_address.exists():
        atomic_write(previous_address, address + "\n")
    environment = directory / "environment"
    if not environment.exists():
        atomic_write(environment, "# Managed relay settings; preserved on upgrades.\n" + "\n".join([
            "ASTER_RELAY_TOKEN=" + secrets.token_urlsafe(32), "ASTER_RELAY_HOST=" + ("::" if ":" in address else "0.0.0.0"), "ASTER_RELAY_PORT=8766",
            "ASTER_RELAY_SYMBOLS=XAUUSD1", "ASTER_RELAY_INTERVAL=0.2", "ASTER_RELAY_BRACKETS_INTERVAL=60",
            "ASTER_RELAY_CERT_FILE=" + json.dumps(str(cert)), "ASTER_RELAY_KEY_FILE=" + json.dumps(str(key)), ""]))
    config = server_connection(directory)
    connection_file = directory / "connection.json"
    encoded = json.dumps(config, ensure_ascii=True, separators=(",", ":")) + "\n"
    if not connection_file.exists() or connection_file.read_text() != encoded:
        atomic_write(connection_file, encoded)


def restart_desk():
    subprocess.run(["systemctl", "restart", "aster-desk"], check=True)
    for _ in range(20):
        conn = http.client.HTTPConnection("127.0.0.1", 8765, timeout=2)
        try:
            conn.request("GET", "/api/health")
            response = conn.getresponse()
            if response.status == 200 and json.loads(response.read(16384)).get("status") == "ok":
                return
        except (OSError, ValueError, http.client.HTTPException):
            pass
        finally:
            conn.close()
        time.sleep(1)
    raise ValueError("Aster Desk health check failed")


def apply_connection(config, directory=DESK_DIR, *, restart=restart_desk, verify=request_health, service_group="aster-desk"):
    directory = Path(directory)
    environment, ca_file = directory / "environment", directory / "relay-ca.pem"
    if not environment.is_file():
        raise ValueError("Install Aster Desk before importing a relay connection")
    if config is not None:
        verify(config)
    original = environment.read_bytes()
    previous_ca = ca_file.read_bytes() if ca_file.exists() else None
    values = dict(zip(KEYS, (config["url"], config["token"], str(ca_file)))) if config else None
    updated = update_environment(original.decode("utf-8"), values).encode("utf-8")
    new_ca = config["ca_pem"].encode("utf-8") if config else None
    if original == updated and previous_ca == new_ca:
        return False
    if service_group is not None and os.name == "posix":
        import grp
        group = grp.getgrnam(service_group).gr_gid
        os.chown(directory, 0, group)
        os.chmod(directory, 0o750)
    else:
        group = None
    try:
        if new_ca is not None:
            atomic_write(ca_file, new_ca, 0o640)
            if group is not None:
                os.chown(ca_file, 0, group)
        atomic_write(environment, updated)
        restart()
        if config is None and ca_file.exists():
            ca_file.unlink()
    except BaseException:
        atomic_write(environment, original)
        if previous_ca is not None:
            atomic_write(ca_file, previous_ca, 0o640)
            if group is not None:
                os.chown(ca_file, 0, group)
        elif ca_file.exists():
            ca_file.unlink()
        try:
            restart()
        except Exception:
            print("Previous configuration restored; service recovery still needs attention.", file=sys.stderr)
        raise
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare-server", "export-connect", "health", "status", "connect", "disconnect"))
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--address")
    parser.add_argument("--file", type=Path)
    args = parser.parse_args()
    try:
        if args.command in ("connect", "disconnect"):
            config = None
            if args.command == "connect":
                if args.file:
                    raw = args.file.read_text(encoding="utf-8")
                elif sys.stdin.isatty():
                    raw = getpass.getpass("Paste the relay connection JSON (hidden), then press Enter: ")
                else:
                    raw = sys.stdin.read(MAX_CONFIG + 1)
                config = validate_connection(raw)
            changed = apply_connection(config, args.directory or DESK_DIR)
            print("Relay connection updated; Aster Desk restarted." if changed else "Relay connection unchanged; restart skipped.")
        elif args.command == "prepare-server":
            prepare_server(args.directory or SERVER_DIR, args.address)
        else:
            config = server_connection(args.directory or SERVER_DIR)
            if args.command == "export-connect":
                print(json.dumps(config, separators=(",", ":")))
            else:
                print(json.dumps(request_health(config, local=True,
                    path="/v1/status" if args.command == "status" else "/health")))
    except (OSError, ValueError, KeyError, ssl.SSLError, subprocess.SubprocessError, http.client.HTTPException):
        # Network/JSON/process exceptions can contain the input or sensitive headers.
        print("Relay configuration or health check failed. Check the address, certificate, token and service status; existing settings were preserved or restored.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
