# codex-vpn

Route Codex / OpenAI traffic through a US exit IP, without a VPN client.

```
Codex app / codex CLI / browser
        │  (PAC file + HTTPS_PROXY)
        ▼
local proxy 127.0.0.1:8899  ──────────────►  direct (everything else)
        │ openai.com, chatgpt.com, oaistatic.com, oaiusercontent.com
        ▼
ssh -D 127.0.0.1:18899  ══ SSH ══►  e2-micro VM in us-east1  ──►  OpenAI
```

- **Infra**: Terraform creates one free-tier `e2-micro` VM (Debian 12) with a static IP and an SSH-only firewall rule. Login is key-only for a dedicated `codexvpn` user.
- **Transport**: Windows' built-in OpenSSH (`ssh -D`), restarted automatically if it drops.
- **Local proxy** (`src/codex_vpn/proxy.py`, stdlib only): HTTP CONNECT/HTTP proxy that sends OpenAI domains through the tunnel and everything else direct. It is **fail-closed**: when the tunnel is down, OpenAI hosts get `502` instead of leaking out over your own IP.
- **Guard** (`src/codex_vpn/guard.py`): Codex never starts or keeps running without the tunnel. Every 2 s it restarts the proxy if it is gone, closes Codex immediately when the proxy is not running (PAC-based apps would fall back to direct), closes it when the tunnel has been down for 6 s, and closes any `codex.exe` that holds a direct, non-proxied connection (for example a CLI started before `HTTPS_PROXY` was set).
- **Debuggable**: every tunneled connection is logged (`logs/proxy.log`), and `/status` shows whether the tunnel is up and where it exits (checked through Cloudflare's `/cdn-cgi/trace` every 30 s).

## Requirements

- Windows 10/11 (uses the built-in OpenSSH client)
- [uv](https://docs.astral.sh/uv/), [Terraform](https://developer.hashicorp.com/terraform/install), [gcloud](https://cloud.google.com/sdk/docs/install)
- A GCP project with billing and the Compute Engine API enabled

## Usage

```bash
gcloud auth login --update-adc               # terraform uses application default credentials
uv run codex-vpn up --project <gcp-project>   # ssh key + terraform apply
uv run codex-vpn install                      # guard + proxy at logon, set PAC + HTTPS_PROXY
uv run codex-vpn status                       # tunnel up? exit ip/location?
```

Restart Codex (and terminals) after `install` so they pick up the proxy settings.

| Command | What it does |
|---|---|
| `up [--project --region --zone]` | Generates `.secrets/id_ed25519` if needed and runs `terraform apply` |
| `serve [-v]` | Runs the proxy + tunnel in the foreground (`-v` also logs direct connections) |
| `guard [--dry-run]` | Runs the guard in the foreground (`--dry-run` logs instead of closing anything) |
| `install` / `uninstall` | Adds/removes the logon entry (the guard, which starts the proxy), `HTTP(S)_PROXY`/`NO_PROXY` user variables and the PAC `AutoConfigURL` |
| `status` | Prints `/status` from the running proxy |
| `logs [-n N] [--guard]` | Tail of `logs/proxy.log` (or `logs/guard.log`) |
| `ssh [cmd]` | SSH into the VM |
| `rdp-client <device>` / `--list` / `--remove <device>` | Create, list or delete a Remote Desktop bundle for a device (see below) |
| `down` | `terraform destroy` |

## How apps are pointed at the proxy

- **Chromium-based apps** (the Codex desktop app, Chrome, Edge) read the system PAC file at `http://127.0.0.1:8899/proxy.pac`, which only sends the OpenAI domains to the proxy. If the proxy is not running at all, Chromium falls back to direct for PAC failures, which is why the guard closes Codex in that case.
- **`codex.exe` and other CLI tools** read `HTTPS_PROXY` / `HTTP_PROXY`. The proxy sends non-OpenAI hosts direct, so other tools keep working, but they need the proxy to be running.

## Debugging

```bash
uv run codex-vpn status          # "tunnel_up", "exit": {"ip": ..., "loc": "US"}, last ssh error
uv run codex-vpn logs -n 100     # per-connection log: route, host, bytes, duration, failures
uv run codex-vpn logs --guard    # what the guard closed and why
curl -x http://127.0.0.1:8899 https://chatgpt.com/cdn-cgi/trace   # should show loc=US
uv run codex-vpn ssh -- uptime   # is the VM reachable?
```

## Remote Desktop from anywhere

The same VM relays Remote Desktop to this PC, so you can reach it from a laptop or phone outside the home network without opening any port on the router or exposing RDP to the internet.

```
laptop / phone                           e2-micro VM                          home PC
mstsc → localhost:13389                                                       Remote Desktop :3389
   │                                                                               ▲
ssh -L 13389 ══ SSH (user rdp) ══►  127.0.0.1:13389  ◄══ SSH (codexvpn) ══  ssh -R 13389:127.0.0.1:3389
                                    (VM loopback only)                       (kept up by `codex-vpn serve`)
```

- The home PC keeps `ssh -R 127.0.0.1:13389:127.0.0.1:3389` open to the VM as a second ssh process next to the Codex tunnel (`rdp-relay` in `status`). If it fails, the Codex tunnel is unaffected.
- A remote device connects as the `rdp` user with its own key and forwards its `localhost:13389` to the VM's `127.0.0.1:13389`, then points a Remote Desktop client at `localhost:13389`.

Setup (Remote Desktop must already be enabled on this PC):

```bash
uv run codex-vpn rdp-client laptop   # .secrets/rdp-clients/laptop/: key, known_hosts, scripts, README.txt
uv run codex-vpn up                  # adds the device key to the VM and applies the sshd restrictions
uv run codex-vpn install             # restarts serve so it starts the rdp relay
```

Copy `.secrets/rdp-clients/laptop/` to the device, then:

- **Windows**: double-click `connect-rdp.cmd` (tunnel in a minimized window, then `mstsc /v:localhost:13389`).
- **macOS / Linux**: `sh connect-rdp.sh`, then connect an RDP client to `localhost:13389`.
- **Phone**: follow `README.txt` (Termius with a local port forward 13389 → 127.0.0.1:13389, then Microsoft "Windows App" with PC `localhost:13389`).

Sign in with this PC's Windows account password (not the PIN). The PC has to be awake and running `codex-vpn serve`.

Security:

- RDP is never exposed publicly: the firewall only allows SSH, and the relay port is bound to the VM's loopback (`GatewayPorts no`).
- Each device has its own key. The `rdp` user can only open a forward to `127.0.0.1:13389`: no shell, no TTY, no other ports (`terraform/startup.sh`).
- Windows still asks for the account password, so a leaked device key alone does not give access to the PC.
- Revoke a device: `uv run codex-vpn rdp-client --remove laptop`, then `uv run codex-vpn up`. `rdp-client --list` shows which keys are on the VM.

## Cost

The e2-micro VM and 30 GB standard disk are in GCP's always-free tier (us-west1, us-central1, us-east1). You pay for the external IPv4 address (about $3.65/month) and egress beyond the free 1 GB/month.

## Files that stay local

`.secrets/` (SSH key, known_hosts, VM address, Remote Desktop device bundles), `terraform/terraform.tfvars.json`, Terraform state and `logs/` are git-ignored.
