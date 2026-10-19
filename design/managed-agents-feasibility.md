# Ringleader as a self-hosted environment for Claude Managed Agents

Oct 18 2026. UNVERIFIED = inference, not documented.

## Summary

- **It exists, in public beta, ungated.** A `self_hosted` environment: Anthropic runs the agent loop; your worker executes `bash`, `read`, `write`, `edit`, `glob`, `grep`. [overview](https://platform.claude.com/docs/en/managed-agents/overview), [self-hosted](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes)
- **Pull-only protocol.** The worker long-polls a queue and streams session events over outbound HTTPS; Anthropic never dials in.
- **Verdict: worth a PoC now (about 5 days).** A channel, not the pitch: eleven sandbox vendors are listed, and Cloudflare and Vercel already do egress allowlists plus credential injection. Ringleader's edge: a persistent machine in the customer's own account, from one file, with admin ceilings and one agent list.
- **Claude-only.** One more agent type beside Claude Code, Codex and Antigravity, not a platform bet.

## What Managed Agents offers today

- **Concepts:** Agent, Environment, Session, Events. Beta header `anthropic-beta: managed-agents-2026-04-01`; on by default. [overview](https://platform.claude.com/docs/en/managed-agents/overview)
- **Create an environment.** `POST /v1/environments` with `{"name":"self-hosted","config":{"type":"self_hosted"}}`. Environment key (`sk-ant-oat01-…`) is Console-only. [self-hosted](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes)
- **Work protocol.** The endpoints are [Environments Work](https://platform.claude.com/docs/en/api/beta/environments/work):
  - `GET /v1/environments/{id}/work/poll` takes `block_ms` (1–999) and `reclaim_older_than_ms` (default 5000). [poll](https://platform.claude.com/docs/en/api/beta/environments/work/poll)
  - `POST …/work/{work_id}/ack` moves an item from `queued` to `starting`.
  - `POST …/heartbeat` takes `desired_ttl_seconds` and `expected_last_heartbeat`, and returns 412 when the lease has been lost. [heartbeat](https://platform.claude.com/docs/en/api/beta/environments/work/heartbeat)
  - `POST …/stop` (`force`); `GET …/work/stats` (`depth`, `pending`, `workers_polling`).
  - `BetaSelfHostedWork`: `state` (`queued|starting|active|stopping|stopped`), `data: {type:"session", id}`, `metadata`, per-session `secret`.
- **Tool execution:** `SessionToolRunner` reads the event stream, runs each `agent.tool_use`, posts `user.tool_result`. [Python SDK source](https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/lib/tools/_beta_session_runner.py)
- **Workers:** `ant beta:worker poll [--on-work spawn.sh]`, `ant beta:worker run`; `EnvironmentWorker`, `work.poller()`, `sessions.events.tool_runner()` in Python/TS/Go. Linux with `/bin/bash`. [reference](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes-reference), [workers](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes-workers)
- **Auth:** environment key scoped to one queue; the per-session `secret` suffices inside a sandbox: "a pod that must never hold the environment key runs on the secret's sessions token alone." [worker source](https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/lib/environments/_worker.py), [security](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes-security)
- **Data flow:** history and tool results go to Anthropic; files and processes stay local; web and MCP tools run Anthropic-side. [choosing](https://platform.claude.com/docs/en/managed-agents/choosing-an-environment)
- **Lost when self-hosted:** `file`/`github_repository` resources and environment-variable vault credentials. [choosing](https://platform.claude.com/docs/en/managed-agents/choosing-an-environment)
- **Spend and limits.**
  - List-price tokens plus **$0.08/session-hour** while `running`. [pricing](https://platform.claude.com/docs/en/about-claude/pricing)
  - Session `budget` (`max_list_cost`) is a hard cap. [budgets](https://platform.claude.com/docs/en/managed-agents/budgets)
  - 300 create / 1,200 read requests per minute. [reference](https://platform.claude.com/docs/en/managed-agents/reference)
- **No ZDR or HIPAA BAA.** [overview](https://platform.claude.com/docs/en/managed-agents/overview)

## Feasibility

**Laptop: yes for development and test, weak for long runs.**
- **NAT** is not a problem, because polling is outbound only. [Daytona guide](https://www.daytona.io/docs/en/guides/claude/claude-managed-agents)
- **Offline:** sessions "stay queued rather than failing." [self-hosted](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes)
- **Sleep in the middle of a turn:**
  - The SDK assumes the lease lost after one TTL (90 s default, then server-reported). [worker source](https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/lib/environments/_worker.py)
  - "Long-dormant sessions" that get a new message are queued again. [poll](https://platform.claude.com/docs/en/api/beta/environments/work/poll)
  - Clean resume of an in-flight tool call: UNVERIFIED.
- **One environment per VM**, or a session can land on a machine without its files.

**Customer cloud: strong fit.**
- Outbound HTTPS to `api.anthropic.com` only; sidesteps the port-22 gap for the agent path (not developer attach).
- The customer owns egress, hardening and key storage: Ringleader's product. [security](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes-security)

**Hybrid: per environment, not per session.**
- Session update has no `environment_id` field, so a session is bound to the environment it was created in. [SDK `session_update_params.py`](https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/beta/session_update_params.py)
- Ringleader's poller dispatches each item (`--on-work`) to the VM holding that workspace. Mid-run moves need Ringleader's unbuilt state transfer.

## What Ringleader would build

| Piece | Have | Build |
|---|---|---|
| Machine | One-file VM on laptop and in three clouds, Linux with bash | Bake `ant` into the base image |
| Adapter | none | A poller **outside** the VM (laptop host process, or the cloud gateway appliance) holding the environment key. It runs `ant beta:worker run` inside the VM with only `ANTHROPIC_WORK_SECRET`, `_WORK_ID`, `_SESSION_ID` and `_ENVIRONMENT_ID` |
| Connectivity | Cloud firewall plus hostname gateway | Allow `api.anthropic.com`. Dispatch from the gateway over the private network instead of a public port 22 |
| Credentials | Gateway injection (cloud) | Inject git/npm credentials for tool calls. This fills the missing environment-variable vault credentials. Stretch goal: inject the session token at the gateway the way Vercel does |
| Identity | Zitadel/SCIM users | Map `session_id` → work item → VM → user. Write the mapping into work `metadata` |
| Spend | Metering built but off | Model calls never cross Ringleader's gateway, so read usage from the sessions API and set `budget` at creation [observability](https://platform.claude.com/docs/en/managed-agents/session-observability) |
| Visibility/audit | Agent sessions hidden, no audit trail | Add Managed Agents sessions as a source in the one list. Log dispatch, egress refusals and stops |

## Fit with the thesis

**Net: strengthens the cloud story.** Anthropic hands the customer "network egress controls," "service key storage" and "isolating untrusted workloads" ([security](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes-security)): Ringleader's rules outside the VM.

Buyer gains: Anthropic's agent on a machine in their account, egress as an inspectable firewall rule, no key on the machine, sessions beside Claude Code and Codex.

Weakens: tool results still reach Anthropic (only files and processes stay); model spend is metered at Anthropic; Claude-only ("Claude 4.5 and later") ([agent setup](https://platform.claude.com/docs/en/managed-agents/agent-setup)), so keep it peripheral.

## Competitors

Anthropic lists platform guides for AWS Lambda MicroVMs, Blaxel, Cloudflare, Daytona, E2B, Fly.io Sprites, GKE Agent Sandbox, Modal, Namespace, Superserve and Vercel. Docker, Coder and microsandbox are absent. [self-hosted](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes#platform-guides)

- **Cloudflare:** webhook, sandbox per session, egress policy, credential-injecting proxies; alpha. [Cloudflare](https://developers.cloudflare.com/sandbox/claude-managed-agents/)
- **Vercel:** webhook, microVM per session, `networkPolicy` allowlist; "the environment key never enters the VM." [Vercel](https://vercel.com/kb/guide/run-claude-managed-agent-tools-with-vercel-sandbox)
- **Daytona:** poll or webhook, sandbox per session kept 30 days, no documented egress controls. [Daytona](https://www.daytona.io/docs/en/guides/claude/claude-managed-agents)
- **Namespace:** ephemeral devboxes, Tailscale ACLs. [Namespace](https://namespace.so/blog/claude-managed-agents-on-devboxes)

None advertise a persistent, developer-owned machine in the customer's account with the same definition on the laptop.

## Risks and open questions

- **Beta:** "Behaviors may be refined between releases"; MCP tunnels are research preview. [overview](https://platform.claude.com/docs/en/managed-agents/overview)
- **Residency:** `inference_geo: "us"` costs 1.1x ([pricing](https://platform.claude.com/docs/en/about-claude/pricing)); no ZDR/BAA; not on Bedrock/Vertex. Claude Platform on AWS: SigV4 workers, no self-hosted memory, 6-hour reauth. [AWS](https://platform.claude.com/docs/en/build-with-claude/claude-platform-on-aws)
- **Branding:** "Powered by Claude" allowed; must not look like Claude Code. [reference](https://platform.claude.com/docs/en/managed-agents/reference#branding-guidelines)
- **Latency:** each tool call round-trips to Anthropic; figures UNVERIFIED.
- **Billing:** session-hours during self-hosted tool runs UNVERIFIED.
- **Listing criteria** unpublished (UNVERIFIED).

## Recommendation and smallest PoC

**Worth a PoC now:** cheap, customer-cloud, fills a documented gap (no environment-variable vault credentials when self-hosted).

**PoC, about 5 days.**
1. Create a `self_hosted` environment, one per VM, and keep the environment key in Ringleader's encrypted secret store. (0.5 day)
2. Laptop: run `ant beta:worker poll --on-work spawn.sh` on the host, and have `spawn.sh` exec `ant beta:worker run` inside the Lima/QEMU VM with only the work secret. Reuse leash's restricted network. (1 day)
3. Cloud: run the same poller on the gateway appliance and dispatch over the private network to a VM that has no public address. (1.5 days)
4. Credential injection for `git push`, plus a session `budget`. (1 day)
5. Pull Managed Agents sessions into the hidden one-list view, keyed by work `metadata`. Measure tool round-trip latency. (1 day)

**Demo:** a laptop-created session clones, edits and pushes from a VM in the customer's AWS; `curl webhook.site` is refused by the firewall; `git push` uses a token the VM never held; the session pauses at its budget; it appears beside a Claude Code session.

**GTM:** request a platform-guide listing from Anthropic partnerships; integration page "Run Claude Managed Agents in your own cloud, on a machine you define"; co-market the own-account, persistent-machine angle against eleven ephemeral partners.
