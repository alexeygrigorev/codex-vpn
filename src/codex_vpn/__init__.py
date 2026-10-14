"""Route Codex/OpenAI traffic through a US exit on a GCP free-tier VM, via an SSH tunnel and a local proxy."""

import argparse
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path

from codex_vpn import proxy

ROOT = Path(__file__).resolve().parents[2]
TF_DIR = ROOT / "terraform"
SECRETS = ROOT / ".secrets"
SSH_KEY = SECRETS / "id_ed25519"
KNOWN_HOSTS = SECRETS / "known_hosts"
TFVARS = TF_DIR / "terraform.tfvars.json"
VM_FILE = SECRETS / "vm.json"
LOG_FILE = ROOT / "logs" / "proxy.log"

PROXY_PORT = 8899
SOCKS_PORT = 18899
PROXY_URL = f"http://127.0.0.1:{PROXY_PORT}"
NO_PROXY = "localhost,127.0.0.1,::1"
RUN_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "codex-vpn"
INET_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
SSH_KEYGEN = r"C:\Windows\System32\OpenSSH\ssh-keygen.exe"
SSH = r"C:\Windows\System32\OpenSSH\ssh.exe"


def run(cmd: list[str], check: bool = True, **kw) -> subprocess.CompletedProcess:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run(cmd, check=check, **kw)


def find_terraform() -> str:
    found = shutil.which("terraform")
    if found:
        return found
    winget = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Packages"
    for exe in winget.glob("Hashicorp.Terraform_*/terraform.exe"):
        return str(exe)
    sys.exit("terraform not found; install it with: winget install Hashicorp.Terraform")


def terraform(*tf_args: str, capture: bool = False) -> str:
    result = run([find_terraform(), f"-chdir={TF_DIR}", *tf_args], capture_output=capture, text=True)
    return result.stdout if capture else ""


def tf_outputs() -> dict:
    raw = json.loads(terraform("output", "-json", capture=True))
    if not raw:
        sys.exit("no terraform outputs yet; run `codex-vpn up --project <id>` first")
    return {k: v["value"] for k, v in raw.items()}


def ensure_ssh_key() -> str:
    if not SSH_KEY.exists():
        SECRETS.mkdir(exist_ok=True)
        run([SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", "codex-vpn", "-f", str(SSH_KEY)])
    return SSH_KEY.with_suffix(".pub").read_text().strip()


def cmd_up(args: argparse.Namespace) -> None:
    tfvars = json.loads(TFVARS.read_text()) if TFVARS.exists() else {}
    for name in ("project", "region", "zone"):
        if getattr(args, name):
            tfvars[name] = getattr(args, name)
    if "project" not in tfvars:
        sys.exit("pass --project the first time")
    tfvars = {k: v for k, v in tfvars.items() if k in ("project", "region", "zone", "name")}
    tfvars["ssh_public_key"] = ensure_ssh_key()
    TFVARS.write_text(json.dumps(tfvars, indent=2))
    terraform("init", "-input=false", "-upgrade")
    terraform("apply", "-input=false", "-auto-approve")
    outputs = tf_outputs()
    VM_FILE.write_text(json.dumps(outputs, indent=2))
    print(json.dumps(outputs, indent=2))


def cmd_down(args: argparse.Namespace) -> None:
    terraform("destroy", "-input=false", "-auto-approve")


def vm() -> dict:
    if not VM_FILE.exists():
        VM_FILE.write_text(json.dumps(tf_outputs(), indent=2))
    return json.loads(VM_FILE.read_text())


def make_tunnel() -> proxy.Tunnel:
    out = vm()
    return proxy.Tunnel(
        host=out["ip"], user=out["ssh_user"], key=SSH_KEY, known_hosts=KNOWN_HOSTS, socks_port=SOCKS_PORT, ssh=SSH
    )


def cmd_serve(args: argparse.Namespace) -> None:
    LOG_FILE.parent.mkdir(exist_ok=True)
    handlers: list[logging.Handler] = [RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8")]
    if sys.stderr and sys.stderr.isatty():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=handlers,
    )
    asyncio.run(proxy.serve(make_tunnel(), PROXY_PORT))


def serve_command() -> str:
    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    return f'"{pythonw}" -m codex_vpn serve'


def start_server() -> None:
    flags = (
        subprocess.DETACHED_PROCESS
        | subprocess.CREATE_NEW_PROCESS_GROUP
        | subprocess.CREATE_BREAKAWAY_FROM_JOB
    )
    subprocess.Popen(serve_command(), creationflags=flags, close_fds=True, cwd=ROOT,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def reg(*args: str) -> None:
    run(["reg", *args, "/f"], capture_output=True)


def stop_server() -> None:
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' or Name='codex-vpn.exe'\" | "
         "Where-Object CommandLine -match 'codex_vpn serve|codex-vpn.exe.? serve' | "
         "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
        check=False,
    )


def cmd_install(args: argparse.Namespace) -> None:
    """Start the proxy at logon and point Codex (env proxy) and Chromium apps (PAC) at it."""
    reg("add", RUN_KEY, "/v", RUN_NAME, "/t", "REG_SZ", "/d", serve_command())
    for name in ("HTTPS_PROXY", "HTTP_PROXY"):
        run(["setx", name, PROXY_URL], capture_output=True)
    run(["setx", "NO_PROXY", NO_PROXY], capture_output=True)
    reg("add", INET_KEY, "/v", "AutoConfigURL", "/t", "REG_SZ", "/d", f"{PROXY_URL}/proxy.pac")
    stop_server()
    start_server()
    print("installed; restart Codex (and terminals) so they pick up the proxy settings")


def cmd_uninstall(args: argparse.Namespace) -> None:
    reg("delete", RUN_KEY, "/v", RUN_NAME)
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"):
        run(["reg", "delete", r"HKCU\Environment", "/v", name, "/f"], check=False, capture_output=True)
    run(["reg", "delete", INET_KEY, "/v", "AutoConfigURL", "/f"], check=False, capture_output=True)
    # setx of a dummy var broadcasts WM_SETTINGCHANGE so new processes see the removal.
    run(["setx", "CODEX_VPN_UNINSTALLED", "1"], capture_output=True)
    run(["reg", "delete", r"HKCU\Environment", "/v", "CODEX_VPN_UNINSTALLED", "/f"], check=False, capture_output=True)
    stop_server()
    print("uninstalled")


def cmd_status(args: argparse.Namespace) -> None:
    try:
        with urllib.request.urlopen(f"{PROXY_URL}/status", timeout=5) as r:
            print(r.read().decode())
    except OSError as e:
        sys.exit(f"proxy not reachable at {PROXY_URL}: {e}\nstart it with `codex-vpn serve` or `codex-vpn install`")


def cmd_logs(args: argparse.Namespace) -> None:
    if not LOG_FILE.exists():
        sys.exit(f"no log at {LOG_FILE}")
    lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(lines[-args.n:]))


def cmd_ssh(args: argparse.Namespace) -> None:
    out = vm()
    result = subprocess.run([SSH, "-i", str(SSH_KEY), "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
                             "-o", "StrictHostKeyChecking=accept-new", f"{out['ssh_user']}@{out['ip']}", *args.command])
    sys.exit(result.returncode)


def main() -> None:
    parser = argparse.ArgumentParser(prog="codex-vpn", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("up", help="create/update the VM with terraform")
    up.add_argument("--project")
    up.add_argument("--region")
    up.add_argument("--zone")
    up.set_defaults(func=cmd_up)

    sub.add_parser("down", help="destroy the VM").set_defaults(func=cmd_down)

    serve = sub.add_parser("serve", help="run the local proxy and ssh tunnel in the foreground")
    serve.add_argument("-v", "--verbose", action="store_true", help="also log direct connections")
    serve.set_defaults(func=cmd_serve)

    sub.add_parser("install", help="run the proxy at logon and point Codex at it").set_defaults(func=cmd_install)
    sub.add_parser("uninstall", help="undo install").set_defaults(func=cmd_uninstall)
    sub.add_parser("status", help="tunnel status and exit location").set_defaults(func=cmd_status)

    logs = sub.add_parser("logs", help="show the end of the proxy log")
    logs.add_argument("-n", type=int, default=40)
    logs.set_defaults(func=cmd_logs)

    ssh = sub.add_parser("ssh", help="ssh into the VM")
    ssh.add_argument("command", nargs="*")
    ssh.set_defaults(func=cmd_ssh)

    args = parser.parse_args()
    args.func(args)
