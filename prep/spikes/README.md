# Spikes

Throwaway experiments that prove (or disprove) the plan's containment claims before kickoff. Not product code.

## Microsandbox on an Apple Silicon Mac

```bash
python3 -m venv .venv
.venv/bin/pip install "microsandbox==0.7.11"      # the SDK bundles msb + libkrunfw; no installer needed
.venv/bin/python prep/spikes/microsandbox_spike.py
```

Options: `--only secrets,metadata`, `--dry-run` (build configs, boot nothing), `--browser` (needs `.venv/bin/pip install "playwright==1.64.0"`; pulls a ~2 GB image). Results go to `out/spike_results.json`.

### First run: 30 Oct 2026, MacBook (Apple Silicon), macOS 27.0, microsandbox 0.7.11

10 of 10 passed. Full report: [out/spike_results.json](out/spike_results.json).

| Check | What happened |
|---|---|
| boot | Guest runs Linux 6.12.113; host is Darwin 27. Warm boot 154 ms; first boot about 7 s including the image pull |
| offline | A VM with `Network.none()` cannot even resolve example.com |
| metadata | With public egress allowed, `169.254.169.254` and `10.0.0.1` still get `HTTP 403 network_policy_denied` |
| allowlist | Allow `example.com` only: example.com 200, example.org connection refused |
| memory | 2 GiB allocation in a 512 MiB VM raises `MemoryError`; the VM keeps answering |
| timeout | `while True: pass` killed at the 5 s exec timeout |
| forkbomb | Classic bash fork bomb as `nobody` with `NPROC=64`: reaped at 10 s; VM answered in 36 ms afterwards |
| diskfill | `dd` capped at 64 MiB by `FSIZE`; host free space unchanged |
| secrets | Guest sees `$MSB_DEMO_TOKEN`; the allowed host received the real value; sending the placeholder to example.com killed the VM 74 ms later |
| lifecycle | A file written in one task's VM is absent in the next task's VM |

### Lessons for the build

1. **Short `MSB_HOME`.** Unix socket paths on macOS must be under 104 bytes, so `MSB_HOME` must be about 50 characters or less. The default `~/.microsandbox` is fine.
2. **Per-secret `violation_action` is broken in the Python SDK 0.7.11.** It is serialised as a string and the native layer rejects it. Set `secret_violation_action=ViolationAction.BLOCK_AND_TERMINATE` on `Sandbox.create` instead (sandbox-wide). Re-check when upgrading.
3. **Run untrusted code as a non-root user.** Linux does not enforce `RLIMIT_NPROC` for root, so the fork-bomb limit only works with `user="nobody"` (or a dedicated user) on `exec`.
4. **Stream system logs while the VM runs.** A terminated ephemeral VM is removed at once, so its logs go with it. The runner should follow `log_stream(sources=[LogReadSource.SYSTEM])` and forward lines like `secret violation: placeholder detected ... secret_env_var=DEMO_TOKEN` to the console as containment events.
5. **Denials are easy to classify.** Plain-HTTP denials come back to the guest as JSON 403s with `code` `network_policy_denied` or `secret_policy_denied`. HTTPS to a non-allowed domain (strict off) is a refused connection.
6. **Secrets over plain HTTP need `require_tls_identity=False`.** In production use HTTPS with `intercept_tls=True` and keep the default.
7. **`Rule.allow_dns()`** is needed under deny-by-default egress whenever any domain is allowed.

### Still to verify

- `--browser`: Chromium inside a microVM driven by `playwright run-server`, the riskiest piece for Pattern B. Run it once before Tuesday.
- The same suite on the Vultr VX1 instance on day 1 (`msb doctor` first).
- HTTPS secret substitution with `intercept_tls=True` against a real API host.

## gVisor fallback (Linux only)

[gvisor_spike.sh](gvisor_spike.sh) installs `runsc` for Docker and runs the same basic checks. Use it on the Vultr VM only if Microsandbox or nested KVM fails there. Not run yet.
