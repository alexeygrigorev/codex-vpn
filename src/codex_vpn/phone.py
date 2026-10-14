"""Mirror and control an Android phone attached over USB from a browser, via ws-scrcpy.

- `setup` clones a pinned ws-scrcpy into `.tools/ws-scrcpy` and builds it without the
  features we never want reachable from the internet (adb shell, file browser, dev tools, iOS),
  patched to listen on 127.0.0.1 only.
- `command()`/`supervise()` run it on 127.0.0.1:8000; `codex-vpn serve` publishes that port
  on the VM's loopback (ssh -R 127.0.0.1:18000) where Caddy serves it over HTTPS with a password.
- `.secrets/phone.json` holds the login; its presence (with "enabled": true) turns the feature on.
"""

import asyncio
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("codex_vpn")

REPO = "https://github.com/NetrisTV/ws-scrcpy"
# Upstream master, not the v0.8.1 release: v0.8.1 bundles scrcpy-server 1.19-ws5, which calls
# SurfaceControl.createDisplay and shows a black screen on Android 14+ (the moto g06 runs 15).
# Master's 1.19-ws8 server adds Android 14/15 support.
COMMIT = "cd6cea6ebde17af6685c2b325183fc24bef9d52b"
PORT = 8000
VM_PORT = 18000
USER = "phone"

# Shell, file listing, dev tools and iOS are left out of the build: anyone who gets past the
# password should only see the screen. scrcpy on the phone listens on its loopback only (we reach
# it through adb), not on the phone's Wi-Fi. All three video decoders stay in; the stream page
# lets you pick one (WebCodecs is the fast one in Chrome).
BUILD_CONFIG = {
    "SCRCPY_LISTENS_ON_ALL_INTERFACES": False,
    "USE_WEBCODECS": True,
    "USE_BROADWAY": True,
    "USE_H264_CONVERTER": True,
    "USE_TINY_H264": True,
    "USE_WDA_MJPEG_SERVER": False,
    "USE_QVH_SERVER": False,
    "INCLUDE_DEV_TOOLS": False,
    "INCLUDE_ADB_SHELL": False,
    "INCLUDE_FILE_LISTING": False,
    "INCLUDE_APPL": False,
    "INCLUDE_GOOG": True,
}
# Native/iOS-only deps that the build above never imports. node-pty 0.10 cannot even be installed
# on Node >= 20 on Windows (its install script spawns a .cmd without a shell -> EINVAL).
DROP_DEPS = ("node-pty", "ios-device-lib", "node-mjpeg-proxy")
# The sources of the left-out features are still type-checked, so give them stub typings.
TYPE_STUBS = {
    "node-pty": "export type IPty = any;\nexport declare function spawn(...args: any[]): any;\n",
    "ios-device-lib": (
        "export declare class IOSDeviceLib {\n    constructor(...args: any[]);\n    [key: string]: any;\n}\n"
        "export declare namespace IOSDeviceLib {\n    type IOSDeviceLib = any;\n    type IDeviceActionInfo = any;\n}\n"
    ),
}
LISTEN_OLD ="server.listen(port, () => {"
LISTEN_NEW = "server.listen(port, process.env.WS_SCRCPY_HOST || '127.0.0.1', () => {"


class Phone:
    def __init__(self, root: Path):
        self.root = root
        self.src = root / ".tools" / "ws-scrcpy"
        self.dist = self.src / "dist"
        self.config = root / ".tools" / "ws-scrcpy.yaml"
        self.secrets_file = root / ".secrets" / "phone.json"

    # ---- settings -------------------------------------------------------------------------
    def settings(self) -> dict:
        if not self.secrets_file.exists():
            return {}
        return json.loads(self.secrets_file.read_text())

    @property
    def enabled(self) -> bool:
        return bool(self.settings().get("enabled"))

    def ensure_login(self, rotate: bool = False) -> dict:
        """Create (or rotate) the browser login. The VM only ever gets the bcrypt hash."""
        import bcrypt

        data = self.settings()
        if rotate or not data.get("password"):
            data["user"] = data.get("user") or USER
            data["password"] = secrets.token_urlsafe(32)
            data["hash"] = bcrypt.hashpw(data["password"].encode(), bcrypt.gensalt(rounds=12)).decode()
        data.setdefault("enabled", True)
        data.setdefault("use_sslip", False)
        self.secrets_file.parent.mkdir(exist_ok=True)
        self.secrets_file.write_text(json.dumps(data, indent=2))
        return data

    def tfvars(self) -> dict:
        data = self.settings()
        if not data.get("enabled"):
            return {"phone_enabled": False}
        return {
            "phone_enabled": True,
            "phone_auth_user": data["user"],
            "phone_auth_hash": data["hash"],
            "phone_use_sslip": bool(data.get("use_sslip")),
        }

    # ---- tools ----------------------------------------------------------------------------
    @staticmethod
    def find_adb() -> str:
        found = shutil.which("adb")
        if found:
            return found
        winget = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Packages"
        for exe in sorted(winget.glob("Genymobile.scrcpy_*/scrcpy-win64-*/adb.exe"), reverse=True):
            return str(exe)
        sys.exit("adb not found; install it with: winget install Genymobile.scrcpy")

    @staticmethod
    def find_node(name: str = "node") -> str:
        found = shutil.which(name)
        if found:
            return found
        exe = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs" / (
            f"{name}.exe" if name == "node" else f"{name}.cmd"
        )
        if exe.exists():
            return str(exe)
        sys.exit("node/npm not found; install them with: winget install OpenJS.NodeJS.LTS")

    # ---- build ----------------------------------------------------------------------------
    def setup(self) -> None:
        def run(cmd: list[str], cwd: Path | None = None) -> None:
            print("+", " ".join(cmd), flush=True)
            subprocess.run(cmd, check=True, cwd=cwd)

        git = shutil.which("git") or sys.exit("git not found")
        npm = self.find_node("npm")
        if not (self.src / ".git").exists():
            self.src.mkdir(parents=True, exist_ok=True)
            run([git, "-C", str(self.src), "init", "--quiet"])
            run([git, "-C", str(self.src), "remote", "add", "origin", REPO])
        # Back to the pristine pinned tree, so setup can be re-run after a failed or older build.
        run([git, "-C", str(self.src), "fetch", "--quiet", "--depth", "1", "origin", COMMIT])
        run([git, "-C", str(self.src), "checkout", "--quiet", "--force", COMMIT])
        head = subprocess.run([git, "-C", str(self.src), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
        if head.strip() != COMMIT:
            sys.exit(f"ws-scrcpy checkout is at {head.strip()}, expected {COMMIT}")

        (self.src / "build.config.override.json").write_text(json.dumps(BUILD_CONFIG, indent=2) + "\n")
        pkg_path = self.src / "package.json"
        pkg = json.loads(pkg_path.read_text())
        for dep in DROP_DEPS:
            pkg["dependencies"].pop(dep, None)
        pkg.pop("optionalDependencies", None)  # appium (iOS)
        pkg_path.write_text(json.dumps(pkg, indent=2) + "\n")
        for module, stub in TYPE_STUBS.items():
            (self.src / "typings" / module).mkdir(exist_ok=True)
            (self.src / "typings" / module / "index.d.ts").write_text(stub)
        http_server = self.src / "src/server/services/HttpServer.ts"
        code = http_server.read_text()
        if LISTEN_OLD not in code:
            sys.exit(f"cannot patch {http_server}: listen() call not found")
        http_server.write_text(code.replace(LISTEN_OLD, LISTEN_NEW))

        run([npm, "install", "--no-audit", "--no-fund"], cwd=self.src)
        run([npm, "run", "dist"], cwd=self.src)
        run([npm, "install", "--omit=dev", "--no-audit", "--no-fund"], cwd=self.dist)
        self.write_config()
        print(f"built ws-scrcpy {COMMIT[:7]} into {self.dist}")

    def write_config(self) -> None:
        self.config.write_text(
            "# Written by codex-vpn; see src/codex_vpn/phone.py\n"
            "runGoogTracker: true\n"
            "announceGoogTracker: true\n"
            "runApplTracker: false\n"
            "announceApplTracker: false\n"
            "server:\n"
            "  - secure: false\n"
            f"    port: {PORT}\n"
        )

    # ---- run ------------------------------------------------------------------------------
    def command(self) -> tuple[list[str], dict]:
        index = self.dist / "index.js"
        if not index.exists():
            sys.exit(f"{index} not found; run `codex-vpn phone setup` first")
        if not self.config.exists():
            self.write_config()
        adb = self.find_adb()
        env = dict(os.environ)
        env["PATH"] = str(Path(adb).parent) + os.pathsep + env.get("PATH", "")
        env["WS_SCRCPY_CONFIG"] = str(self.config)
        env["WS_SCRCPY_HOST"] = "127.0.0.1"
        env["NODE_ENV"] = "production"
        # adbkit talks to the adb server on 127.0.0.1:5037 but does not start it.
        subprocess.run([adb, "start-server"], check=False, capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return [self.find_node("node"), str(index)], env

    def serve(self) -> None:
        cmd, env = self.command()
        print(f"ws-scrcpy on http://127.0.0.1:{PORT}", flush=True)
        sys.exit(subprocess.run(cmd, env=env, cwd=self.dist).returncode)

    async def supervise(self) -> None:
        """Keep ws-scrcpy running (restart with backoff), logging its output."""
        delay = 1
        while True:
            started = time.monotonic()
            cmd, env = self.command()
            log.info("phone: starting ws-scrcpy on 127.0.0.1:%d", PORT)
            proc = await asyncio.create_subprocess_exec(
                *cmd, env=env, cwd=self.dist,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                assert proc.stdout
                async for line in proc.stdout:
                    text = line.decode(errors="replace").rstrip()
                    if text:
                        log.debug("phone: %s", text)
                code = await proc.wait()
            finally:
                if proc.returncode is None:
                    proc.kill()
            log.error("phone: ws-scrcpy exited with code %s", code)
            delay = 1 if time.monotonic() - started > 60 else min(delay * 2, 30)
            await asyncio.sleep(delay)

    # ---- route the phone's traffic through the proxy ---------------------------------------
    # `adb reverse` makes the phone's 127.0.0.1:<port> reach the PC's proxy over USB, and Android's
    # global HTTP proxy points there, so OpenAI hosts take the tunnel like on the PC (the rest goes out
    # direct from the PC). The setting survives unplugging: while the phone is off USB, apps that use
    # the system proxy have no network until it is plugged back in (`phone proxy off` undoes it).
    @property
    def proxy_enabled(self) -> bool:
        return bool(self.settings().get("proxy"))

    def set_proxy_enabled(self, on: bool) -> None:
        data = self.settings()
        data["proxy"] = on
        self.secrets_file.parent.mkdir(exist_ok=True)
        self.secrets_file.write_text(json.dumps(data, indent=2))

    def adb(self, *args: str, serial: str | None = None) -> str:
        cmd = [self.find_adb(), *(["-s", serial] if serial else []), *args]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return result.stdout.strip()

    def devices(self) -> list[str]:
        lines = self.adb("devices").splitlines()[1:]
        return [line.split("\t")[0] for line in lines if line.endswith("\tdevice")]

    def apply_proxy(self, port: int) -> list[str]:
        """Make sure every attached phone has the reverse port and the global proxy; return what changed."""
        changes = []
        for serial in self.devices():
            if f"tcp:{port} tcp:{port}" not in self.adb("reverse", "--list", serial=serial):
                self.adb("reverse", f"tcp:{port}", f"tcp:{port}", serial=serial)
                changes.append(f"{serial}: adb reverse tcp:{port}")
            if self.adb("shell", "settings", "get", "global", "http_proxy", serial=serial) != f"127.0.0.1:{port}":
                self.adb("shell", "settings", "put", "global", "http_proxy", f"127.0.0.1:{port}", serial=serial)
                changes.append(f"{serial}: global http_proxy -> 127.0.0.1:{port}")
        return changes

    def clear_proxy(self, port: int) -> list[str]:
        serials = self.devices()
        for serial in serials:
            self.adb("shell", "settings", "put", "global", "http_proxy", ":0", serial=serial)
            self.adb("reverse", "--remove", f"tcp:{port}", serial=serial)
        return serials

    def proxy_state(self) -> dict:
        return {serial: self.adb("shell", "settings", "get", "global", "http_proxy", serial=serial)
                for serial in self.devices()}

    async def keep_proxy(self, port: int, every: int = 10) -> None:
        """Re-apply after the phone is replugged or rebooted (a reboot drops `adb reverse`)."""
        while True:
            try:
                for change in await asyncio.to_thread(self.apply_proxy, port):
                    log.info("phone: %s", change)
            except (Exception, SystemExit) as e:  # find_adb() exits when adb is missing; keep serving
                log.warning("phone: proxy setup failed: %r", e)
            await asyncio.sleep(every)

    def url(self, ip: str | None) -> str:
        if not ip:
            return "https://<vm-ip>"
        if self.settings().get("use_sslip"):
            return f"https://{ip.replace('.', '-')}.sslip.io"
        return f"https://{ip}"
