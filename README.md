# tether

Keep a coding agent's credentials, egress and spend outside the machine it
runs on. A proof of concept, in the spirit of
[imp](https://github.com/metcalfc/imp): the agent gets a per-boot
**capability**, never a credential.

Three layers, configured from one policy file:

| layer | what | enforces |
|---|---|---|
| **cage** | a [microsandbox](https://github.com/superradcompany/microsandbox) VM, `--net-default deny` plus one host port | the gateway is the only way out, even for root in the guest |
| **identity** | [nono](https://nono.sh/) inside the guest (Landlock), per-agent Unix users | each tool reads only its own cap file |
| **gateway** | `./tether` on the host, one listener per sandbox | credential injection, egress allowlist, spend cap, audit |

```
make test        # unit tests, loopback only, no sandbox, no keys
make compile     # spike/msb/poc-policy.json -> msb flags, nono profiles, tether config
make report      # spend and denials per (session, agent, tool)
make audit       # verify the audit's hash chain
```

## The gateway

`tether` is one stdlib Python file (3.10+). It does four jobs:

| | how | what the agent sees |
|---|---|---|
| **credential injection** | `/anthropic/…`, `/openai/…`, `/github/…` routes swap the cap for the real key, after a per-route path allowlist | `ANTHROPIC_AUTH_TOKEN=tth_…`, worthless anywhere else |
| **egress allowlist** | `CONNECT` only to listed hosts and ports, resolved on the host; loopback, private, link-local and metadata addresses refused; SNI must match | `403` from anything else |
| **spend cap** | a reservation at admission, settled from the response's own usage at stream end; per-session and per-cap caps, `max_inflight` | a `permission_error` naming the cap |
| **audit** | one hash-chained JSON line per decision, attributed to (session, agent, tool); never headers, never bodies | — |

```
agent VM (no route out)                 tether (host)
-----------------------------------------------------------------------------
claude  -- ANTHROPIC_BASE_URL -->  /anthropic + real key ---> api.anthropic.com
git     -- cap file ------------>  /github    + real token -> github.com
npm     -- HTTPS_PROXY --------->  CONNECT allowlist ------> registry.npmjs.org
curl https://webhook.site ------>  403, audited
curl --noproxy '*' anything ---->  no DNS, no route
```

A cap only works on its own sandbox's listener, so a cap copied out of one
sandbox gets 403 from another. Caps are minted at boot
(`tether cap mint|revoke|list|boot`) and revocation is live.

A host that has a credentialed route can't also be tunnelled to. Otherwise
`CONNECT api.anthropic.com` would bypass injection, metering and the cap.

## The policy

`spike/msb/poc-policy.json` describes the machine, the gateway's routes and
credentials, each agent and each of its tools, and a ceiling.
`tether compile` turns it into the three layers' configs:

- `msb.args`
- `nono/<agent>.json`
- `tether.json`, with a grant per cap, each grant intersected with the ceiling

Credentials take `keychain:NAME`, `env:NAME`, `file:PATH` or `cmd:<shell>`. A
route whose credential doesn't resolve is switched off, not left open.

## Running the cage

On an Apple Silicon Mac you need:

- msb 0.7.8 in `~/.microsandbox`
- nono's guest build in `~/.microsandbox/tether-guest/bin`
- the Landlock libkrunfw from `spike/msb/kernel/build.sh`
- the keychain items `tether-anthropic` and `tether-gh-scratch`

Then:

```sh
make demo-setup                 # fresh state, compile, gateway up
spike/msb/demo.sh 1             # ... through 10, one step each
make demo-teardown              # cage down, gateway down
make record                     # or all of it unattended, via VHS
```

`spike/msb/cage.sh` drives the pieces one at a time: `compile`, `gw-up`,
`up`, `users`, `nono`, `attack`, `adv`, `replay` and `down`. It reads the
compiled policy from `overnight/run/compiled` unless `CAGE_OUT` says
otherwise.

## Results

`spike/RESULTS.md` has every success criterion and measurement.
`overnight/REPORT.md` is the one-page summary, and the open questions are
listed under "Look at these first". The short version:

- every cage attack is blocked, with root in the guest;
- no real credential is on guest disk or in guest memory;
- every credentialed call is attributed;
- overshoot with four parallel streams stays within one reservation;
- the gateway adds +0.17 ms at p50.

The design is `design/tether-poc.md`.

## What this is not

- **Not novel on its own.** Several tools already ship host-side credential
  injection plus an egress allowlist:
  - [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/) (`sbx`)
  - [Claude Code's sandbox](https://code.claude.com/docs/en/sandboxing) (credential masking)
  - [nono](https://nono.sh/), whose "phantom token" proxy injects real
    secrets at the network boundary
  - [Infisical Agent Vault](https://infisical.com/blog/agent-vault-the-open-source-credential-proxy-and-vault-for-agents)
  - [microsandbox](https://github.com/superradcompany/microsandbox)

  The point of tether is narrower:
  1. Per-**session** and per-tool spend caps, enforced where the agent can't
     reach them.
  2. One policy file for the cage, the in-guest sandbox and the gateway.
     "One policy, laptop or cloud" is Ringleader's actual claim.
- **Not secret from the agent's own tools.** The agent that drives git can
  read git's cap. msb's agentd also keeps copies of every cap in memory.
  A cap is worth its grant, within its cap, until it is revoked.
- **No TLS between guest and gateway.** That hop never leaves the host.
- **No interception of TLS on tunnels.** Allowlisted hosts get an opaque
  tunnel, so credentials can only be injected on explicit routes.
