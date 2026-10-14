"""Route Codex/OpenAI traffic through a US exit on a GCP free-tier VM, via an SSH tunnel and a local proxy."""

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path

from codex_vpn import guard, phone, proxy

ROOT = Path(__file__).resolve().parents[2]
TF_DIR = ROOT / "terraform"
SECRETS = ROOT / ".secrets"
SSH_KEY = SECRETS / "id_ed25519"
KNOWN_HOSTS = SECRETS / "known_hosts"
TFVARS = TF_DIR / "terraform.tfvars.json"
VM_FILE = SECRETS / "vm.json"
LOG_FILE = ROOT / "logs" / "proxy.log"
GUARD_LOG = ROOT / "logs" / "guard.log"

PROXY_PORT = 8899
SOCKS_PORT = 18899
PROXY_URL = f"http://127.0.0.1:{PROXY_PORT}"
NO_PROXY = "localhost,127.0.0.1,::1"
RUN_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "codex-vpn"
INET_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
SSH_KEYGEN = r"C:\Windows\System32\OpenSSH\ssh-keygen.exe"
SSH = r"C:\Windows\System32\OpenSSH\ssh.exe"
PHONE = phone.Phone(ROOT)

# Remote Desktop relay: the home PC publishes its RDP port on the VM's loopback (ssh -R),
# remote devices reach it with ssh -L as the restricted "rdp" user (see terraform/startup.sh).
RDP_CLIENTS = SECRETS / "rdp-clients"
RDP_LOCAL_PORT = 3389
RDP_RELAY_PORT = 13389
RDP_USER = "rdp"
DEVICE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


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
    tfvars["rdp_public_keys"] = rdp_public_keys()
    tfvars.update(PHONE.tfvars())
    TFVARS.write_text(json.dumps(tfvars, indent=2))
    terraform("init", "-input=false", "-upgrade")
    terraform("apply", "-input=false", "-auto-approve")
    outputs = tf_outputs()
    VM_FILE.write_text(json.dumps(outputs, indent=2))
    print(json.dumps(outputs, indent=2))
    # The startup script (sshd restrictions for the rdp user) only runs at boot; apply it now.
    result = run([*ssh_command(outputs, "-o", "ConnectTimeout=20"), "sudo", "google_metadata_script_runner", "startup"],
                 check=False)
    if result.returncode != 0:
        print("warning: could not re-run the startup script; it runs on the next boot "
              "(or retry with `codex-vpn ssh -- sudo google_metadata_script_runner startup`)")


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


def make_relays(out: dict | None = None) -> list[proxy.Tunnel]:
    """Extra ssh processes next to the Codex tunnel; empty unless the VM has rdp keys."""
    out = vm() if out is None else out
    if not out.get("rdp_enabled"):
        return []
    return [proxy.Tunnel(
        name="rdp-relay", host=out["ip"], user=out["ssh_user"], key=SSH_KEY, known_hosts=KNOWN_HOSTS,
        socks_port=0, ssh=SSH, forwards=["-R", f"127.0.0.1:{RDP_RELAY_PORT}:127.0.0.1:{RDP_LOCAL_PORT}"],
    )]


def setup_logging(path: Path, verbose: bool = False) -> None:
    path.parent.mkdir(exist_ok=True)
    handlers: list[logging.Handler] = [RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")]
    if sys.stderr and sys.stderr.isatty():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=handlers,
    )


def make_phone_relays() -> list[proxy.Tunnel]:
    """When phone mirroring is on, publish ws-scrcpy on the VM's loopback for Caddy."""
    if not PHONE.enabled:
        return []
    out = vm()
    return [proxy.Tunnel(
        host=out["ip"], user=out["ssh_user"], key=SSH_KEY, known_hosts=KNOWN_HOSTS, socks_port=0, ssh=SSH,
        name="phone-relay", forwards=["-R", f"127.0.0.1:{phone.VM_PORT}:127.0.0.1:{phone.PORT}"],
    )]


async def serve_all(tunnel: proxy.Tunnel, relays: list[proxy.Tunnel]) -> None:
    extra = [PHONE.supervise()] if PHONE.enabled else []
    await asyncio.gather(proxy.serve(tunnel, PROXY_PORT, relays=relays), *extra)


def cmd_serve(args: argparse.Namespace) -> None:
    setup_logging(LOG_FILE, args.verbose)
    asyncio.run(serve_all(make_tunnel(), make_relays() + make_phone_relays()))


def cmd_guard(args: argparse.Namespace) -> None:
    setup_logging(GUARD_LOG)
    guard.run(f"{PROXY_URL}/status", lambda: start_background("serve"), dry_run=args.dry_run)


def background_command(sub: str) -> str:
    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    return f'"{pythonw}" -m codex_vpn {sub}'


def start_background(sub: str) -> None:
    flags = (
        subprocess.DETACHED_PROCESS
        | subprocess.CREATE_NEW_PROCESS_GROUP
        | subprocess.CREATE_BREAKAWAY_FROM_JOB
    )
    subprocess.Popen(background_command(sub), creationflags=flags, close_fds=True, cwd=ROOT,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def reg(*args: str) -> None:
    run(["reg", *args, "/f"], capture_output=True)


def stop_background(*subs: str) -> None:
    pattern = "|".join(f"codex_vpn {s}|codex-vpn.exe.? {s}" for s in subs)
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' or Name='codex-vpn.exe'\" | "
         f"Where-Object CommandLine -match '{pattern}' | "
         "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
        check=False,
    )


def cmd_install(args: argparse.Namespace) -> None:
    """Run the guard (which keeps the proxy up) at logon and point Codex (env proxy) and Chromium apps (PAC) at it."""
    reg("add", RUN_KEY, "/v", RUN_NAME, "/t", "REG_SZ", "/d", background_command("guard"))
    for name in ("HTTPS_PROXY", "HTTP_PROXY"):
        run(["setx", name, PROXY_URL], capture_output=True)
    run(["setx", "NO_PROXY", NO_PROXY], capture_output=True)
    reg("add", INET_KEY, "/v", "AutoConfigURL", "/t", "REG_SZ", "/d", f"{PROXY_URL}/proxy.pac")
    stop_background("guard", "serve")
    start_background("serve")
    start_background("guard")
    print("installed; restart Codex (and terminals) so they pick up the proxy settings")


def cmd_uninstall(args: argparse.Namespace) -> None:
    reg("delete", RUN_KEY, "/v", RUN_NAME)
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"):
        run(["reg", "delete", r"HKCU\Environment", "/v", name, "/f"], check=False, capture_output=True)
    run(["reg", "delete", INET_KEY, "/v", "AutoConfigURL", "/f"], check=False, capture_output=True)
    # setx of a dummy var broadcasts WM_SETTINGCHANGE so new processes see the removal.
    run(["setx", "CODEX_VPN_UNINSTALLED", "1"], capture_output=True)
    run(["reg", "delete", r"HKCU\Environment", "/v", "CODEX_VPN_UNINSTALLED", "/f"], check=False, capture_output=True)
    stop_background("guard", "serve")
    print("uninstalled")


def cmd_status(args: argparse.Namespace) -> None:
    try:
        with urllib.request.urlopen(f"{PROXY_URL}/status", timeout=5) as r:
            print(r.read().decode())
    except OSError as e:
        sys.exit(f"proxy not reachable at {PROXY_URL}: {e}\nstart it with `codex-vpn serve` or `codex-vpn install`")


def cmd_logs(args: argparse.Namespace) -> None:
    path = GUARD_LOG if args.guard else LOG_FILE
    if not path.exists():
        sys.exit(f"no log at {path}")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(lines[-args.n:]))


def ssh_command(out: dict, *options: str) -> list[str]:
    return [SSH, "-i", str(SSH_KEY), "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
            "-o", "StrictHostKeyChecking=accept-new", *options, f"{out['ssh_user']}@{out['ip']}"]


def cmd_ssh(args: argparse.Namespace) -> None:
    result = subprocess.run([*ssh_command(vm()), *args.command])
    sys.exit(result.returncode)


# --- Remote Desktop client bundles -------------------------------------------------------

def rdp_public_keys(clients: Path = RDP_CLIENTS) -> list[str]:
    """Public keys of all device bundles, sorted by device name."""
    if not clients.is_dir():
        return []
    pubs = [d / "id_ed25519.pub" for d in sorted(clients.iterdir(), key=lambda d: d.name) if d.is_dir()]
    return [p.read_text().strip() for p in pubs if p.exists()]


def host_key_lines(ip: str, known_hosts: Path = KNOWN_HOSTS) -> list[str]:
    """known_hosts lines for the VM's address (plain, unhashed entries as written by Windows OpenSSH)."""
    if not known_hosts.exists():
        return []
    lines = []
    for line in known_hosts.read_text(encoding="utf-8", errors="replace").splitlines():
        hosts = line.split(" ", 1)[0].split(",") if line.strip() and not line.startswith("#") else []
        if ip in hosts or f"[{ip}]:22" in hosts:
            lines.append(line.strip())
    return lines


def rdp_scripts(device: str, ip: str, strict: bool) -> dict[str, str]:
    """Text of the connect scripts and README for one device (content only, no I/O)."""
    hostkey = "yes" if strict else "accept-new"
    fwd = f"{RDP_RELAY_PORT}:127.0.0.1:{RDP_RELAY_PORT}"
    cmd = "\r\n".join([
        "@echo off",
        f"rem codex-vpn: Remote Desktop to the home PC through {ip} (device: {device}).",
        "rem OpenSSH refuses keys that other users can read; restrict the copied key to you.",
        'icacls "%~dp0id_ed25519" /inheritance:r /grant:r "%USERNAME%:R" >nul',
        f'start "codex-vpn rdp tunnel" /min ssh -N -L {fwd} -i "%~dp0id_ed25519" -o IdentitiesOnly=yes '
        f'-o UserKnownHostsFile="%~dp0known_hosts" -o StrictHostKeyChecking={hostkey} '
        f"-o ExitOnForwardFailure=yes -o ServerAliveInterval=15 {RDP_USER}@{ip}",
        "timeout /t 3 /nobreak >nul",
        f"mstsc /v:localhost:{RDP_RELAY_PORT}",
        "",
    ])
    sh = "\n".join([
        "#!/bin/sh",
        f"# codex-vpn: Remote Desktop to the home PC through {ip} (device: {device}).",
        'cd "$(dirname "$0")" || exit 1',
        "chmod 600 id_ed25519",
        f'echo "Forwarding localhost:{RDP_RELAY_PORT} to the home PC. Leave this running; Ctrl+C to disconnect."',
        f'echo "Then connect a Remote Desktop client to localhost:{RDP_RELAY_PORT}'
        f' (macOS: Windows App; Linux: xfreerdp /v:localhost:{RDP_RELAY_PORT})."',
        f"exec ssh -N -L {fwd} -i id_ed25519 -o IdentitiesOnly=yes -o UserKnownHostsFile=known_hosts "
        f"-o StrictHostKeyChecking={hostkey} -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 {RDP_USER}@{ip}",
        "",
    ])
    readme = "\r\n".join([
        f"codex-vpn Remote Desktop bundle for device '{device}'",
        "",
        f"The home PC's Remote Desktop is reachable only through an SSH tunnel to {ip} as user '{RDP_USER}'.",
        "The key in this folder can open exactly one forward (to the home PC's Remote Desktop) and nothing else.",
        "Treat id_ed25519 like a password. Sign in to Windows with the home PC's account (password, not PIN).",
        "",
        "Windows laptop",
        "  Double-click connect-rdp.cmd. It opens the tunnel in a minimized window and starts Remote Desktop",
        f"  (mstsc) to localhost:{RDP_RELAY_PORT}. Close the minimized 'codex-vpn rdp tunnel' window to disconnect.",
        "",
        "macOS / Linux",
        "  sh connect-rdp.sh   (keep it running), then connect your RDP client to "
        f"localhost:{RDP_RELAY_PORT}.",
        "",
        "Phone (iOS / Android)",
        "  1. Install Termius and Microsoft 'Windows App' (formerly Remote Desktop).",
        "  2. Termius > Keychain > Import key: id_ed25519 from this folder.",
        f"  3. Termius > Hosts > New host: address {ip}, port 22, username {RDP_USER}, key: the imported key.",
        "  4. Termius > Port Forwarding > New rule: Local, host = the host above,",
        f"     local port {RDP_RELAY_PORT}, bind address 127.0.0.1, destination 127.0.0.1, destination port {RDP_RELAY_PORT}.",
        "     Start the rule (accept the host key if it matches the fingerprint below). Keep Termius open in the",
        "     background while connected (iOS may suspend it after a few minutes).",
        f"  5. Windows App > Add PC: localhost:{RDP_RELAY_PORT}, user account = the home PC's Windows account.",
        "",
        "VM host key fingerprint(s):",
        "  {fingerprints}",
        "",
        "If the home PC is off, asleep, or codex-vpn is not running there, the tunnel connects but RDP fails.",
        "",
    ])
    return {"connect-rdp.cmd": cmd, "connect-rdp.sh": sh, "README.txt": readme}


def create_rdp_bundle(device: str, ip: str, clients: Path = RDP_CLIENTS, known_hosts: Path = KNOWN_HOSTS,
                      keygen: str = SSH_KEYGEN) -> Path:
    if not DEVICE_NAME.match(device):
        sys.exit(f"bad device name {device!r}: use letters, digits, '-' or '_' (e.g. laptop, phone)")
    bundle = clients / device
    if bundle.exists():
        sys.exit(f"{bundle} already exists; remove it first with `codex-vpn rdp-client --remove {device}`")
    bundle.mkdir(parents=True)
    key = bundle / "id_ed25519"
    run([keygen, "-q", "-t", "ed25519", "-N", "", "-C", f"codex-vpn-rdp-{device}", "-f", str(key)])
    hostkeys = host_key_lines(ip, known_hosts)
    (bundle / "known_hosts").write_text("".join(f"{line}\n" for line in hostkeys), newline="\n")
    fingerprints = "(unknown: no host key in .secrets/known_hosts yet)"
    if hostkeys:
        fp = subprocess.run([keygen, "-l", "-f", str(bundle / "known_hosts")], capture_output=True, text=True)
        fingerprints = "\r\n  ".join(fp.stdout.strip().splitlines()) or fingerprints
    else:
        print(f"warning: no host key for {ip} in {known_hosts}; the scripts will trust the VM on first connect")
    for name, text in rdp_scripts(device, ip, strict=bool(hostkeys)).items():
        text = text.replace("{fingerprints}", fingerprints)
        (bundle / name).write_bytes(text.encode("utf-8"))
    return bundle


def remove_rdp_bundle(device: str, clients: Path = RDP_CLIENTS) -> Path:
    bundle = clients / device
    if not DEVICE_NAME.match(device) or not bundle.is_dir():
        sys.exit(f"no rdp client {device!r} in {clients}")
    shutil.rmtree(bundle)
    return bundle


def cmd_rdp_client(args: argparse.Namespace) -> None:
    if args.list:
        applied = set(json.loads(TFVARS.read_text()).get("rdp_public_keys", [])) if TFVARS.exists() else set()
        devices = sorted(d for d in RDP_CLIENTS.iterdir() if d.is_dir()) if RDP_CLIENTS.is_dir() else []
        if not devices:
            print("no rdp clients; create one with `codex-vpn rdp-client <device>`")
        for d in devices:
            pub = d / "id_ed25519.pub"
            state = "on VM" if pub.exists() and pub.read_text().strip() in applied else "pending `codex-vpn up`"
            print(f"{d.name:<20} {state:<24} {d}")
    elif args.remove:
        bundle = remove_rdp_bundle(args.remove)
        print(f"removed {bundle}\nrun `codex-vpn up` to revoke its key on the VM")
    elif args.device:
        if not DEVICE_NAME.match(args.device):
            sys.exit(f"bad device name {args.device!r}: use letters, digits, '-' or '_' (e.g. laptop, phone)")
        bundle = create_rdp_bundle(args.device, vm()["ip"])
        print(f"\nRemote Desktop bundle for {args.device!r}: {bundle}\n"
              "  1. run `codex-vpn up` so the VM accepts this device's key\n"
              "  2. if this is the first device, run `codex-vpn install` to restart serve with the rdp relay\n"
              "  3. copy the folder to the device and follow README.txt in it")
    else:
        sys.exit("give a device name (e.g. laptop, phone), --list or --remove <device>")
def cmd_phone(args: argparse.Namespace) -> None:
    if args.phone_command == "setup":
        PHONE.setup()
        data = PHONE.ensure_login(rotate=args.rotate_password)
        if args.sslip is not None:
            data["use_sslip"] = args.sslip
        data["enabled"] = True
        PHONE.secrets_file.write_text(json.dumps(data, indent=2))
        print(f"login saved in {PHONE.secrets_file}; run `codex-vpn up` to configure the VM, "
              "then restart `codex-vpn serve` (or re-run `install`)")
    elif args.phone_command == "rotate-password":
        PHONE.ensure_login(rotate=True)
        print("new password saved; run `codex-vpn up` to push it to the VM, then `codex-vpn phone url`")
    elif args.phone_command == "disable":
        data = PHONE.settings()
        if data:
            data["enabled"] = False
            PHONE.secrets_file.write_text(json.dumps(data, indent=2))
        print("disabled; run `codex-vpn up` (closes ports 80/443, stops Caddy) and restart `codex-vpn serve`")
    elif args.phone_command == "serve":
        PHONE.serve()
    elif args.phone_command == "url":
        data = PHONE.settings()
        if not data.get("password"):
            sys.exit(f"no {PHONE.secrets_file}; run `codex-vpn phone setup` first")
        ip = json.loads(VM_FILE.read_text()).get("ip") if VM_FILE.exists() else None
        print(f"url:      {PHONE.url(ip)}")
        print(f"user:     {data['user']}")
        print(f"password: {data['password']}")
        if not data.get("enabled"):
            print("(phone mirroring is disabled)")


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

    guard_p = sub.add_parser("guard", help="keep the proxy up and close Codex if it could bypass the tunnel")
    guard_p.add_argument("--dry-run", action="store_true", help="log what would be closed, close nothing")
    guard_p.set_defaults(func=cmd_guard)

    sub.add_parser("install", help="run the guard + proxy at logon and point Codex at it").set_defaults(func=cmd_install)
    sub.add_parser("uninstall", help="undo install").set_defaults(func=cmd_uninstall)
    sub.add_parser("status", help="tunnel status and exit location").set_defaults(func=cmd_status)

    logs = sub.add_parser("logs", help="show the end of the proxy log")
    logs.add_argument("-n", type=int, default=40)
    logs.add_argument("--guard", action="store_true", help="show the guard log instead of the proxy log")
    logs.set_defaults(func=cmd_logs)

    ssh = sub.add_parser("ssh", help="ssh into the VM")
    ssh.add_argument("command", nargs="*")
    ssh.set_defaults(func=cmd_ssh)

    rdp = sub.add_parser("rdp-client", help="create/list/remove a device bundle for Remote Desktop from anywhere",
                         description="Each device gets its own ssh key that can only reach the home PC's Remote "
                                     "Desktop through the VM. Run `codex-vpn up` after adding or removing one.")
    rdp.add_argument("device", nargs="?", help="device name, e.g. laptop or phone")
    rdp.add_argument("--list", action="store_true", help="list device bundles and whether their key is on the VM")
    rdp.add_argument("--remove", metavar="DEVICE", help="delete a device bundle (then `codex-vpn up` revokes it)")
    rdp.set_defaults(func=cmd_rdp_client)
    phone_p = sub.add_parser("phone", help="control the USB-attached Android phone from a browser (ws-scrcpy)")
    phone_sub = phone_p.add_subparsers(dest="phone_command", required=True)
    setup_p = phone_sub.add_parser("setup", help="build ws-scrcpy into .tools/ and create the login")
    setup_p.add_argument("--rotate-password", action="store_true", help="replace the existing password")
    setup_p.add_argument("--sslip", action=argparse.BooleanOptionalAction, default=None,
                         help="serve on <ip>.sslip.io instead of the bare IP (fallback)")
    phone_sub.add_parser("serve", help="run ws-scrcpy on 127.0.0.1:8000 in the foreground")
    phone_sub.add_parser("url", help="print the URL, user and password")
    phone_sub.add_parser("rotate-password", help="generate a new password")
    phone_sub.add_parser("disable", help="turn phone mirroring off (then run `up`)")
    phone_p.set_defaults(func=cmd_phone)

    args = parser.parse_args()
    args.func(args)
