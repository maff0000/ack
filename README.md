# ACK — Axiom Control Kit

ACK is a small, portable project-control kit for Axiom-led local coding agents. The PID defines the box, the Architect changes it, Axiom leads execution inside it, and bounded workers perform independently verifiable work. Redis says what is happening now; Git proves what happened.

## Architecture

```text
Architect (direction / architecture)
  -> Axiom (decomposition / dispatch / integration / acceptance)
    -> local LiteLLM workers (bounded implementation / verification)
       Redis: heartbeat, progress, status, leases, events
       Git: durable changes, evidence, and history
```

Workers receive selective context only:

```text
CORE + role + PROJECT + selected engineering skills + relevant ADRs + task
```

Only Axiom accepts or rejects work. A worker may report `completed`, `blocked`, or `failed`.

## Bootstrap or adopt

1. Copy `PID.md`, `AXIOM.md`, and `.ack/` into a new Git project root.
2. Set one absolute `PROJECT_ROOT` in `PID.md` and `.ack/state/project.yaml`.
3. Customise `.ack/skills/project/PROJECT.md` with only essential invariants.
4. Configure Redis and the local-agent command outside source; use `.ack/config.example.yaml` as the non-secret template.
5. Start Axiom from the project root. Axiom reads the PID, doctrine, concise state, skill index, Git, Redis, active tasks/results, and only relevant ADRs.

Path checks resolve canonical paths and must reject `..` and symlink escapes. A worker that requires out-of-root mutation reports `blocked`.

## Configuration

Runtime configuration includes the Redis endpoint, local agent/LiteLLM invocation, heartbeat, lease and stale thresholds, maximum parallel workers, and logical model aliases. Never put endpoints with credentials or secrets in committed configuration. Logical aliases such as `trinity-fast`, `trinity-core`, and `trinity-deep` are routed to physical models externally.

Install the two small Python dependencies with `python3 -m pip install -r requirements.txt`, copy `.ack/config.example.yaml` to the ignored `.ack/config.yaml`, and inject `ACK_REDIS_URL`. ACK v0.1 constructs a fixed `bubblewrap` profile that mounts the host read-only. A read worker gets no writable project bind; a write worker gets only its project-local isolated repository under `.ack/worktrees/`. Canonical files, refs, and objects remain read-only. Axiom fetches and integrates accepted worker commits. ACK refuses unsupported sandbox executables and passes only an explicit environment allowlist.

Validate or dispatch a task and inspect live state with:

```text
.ack/tools/ack-agent validate .ack/tasks/active/AX-001.yaml
.ack/tools/ack-agent prepare .ack/tasks/active/AX-001.yaml
.ack/tools/ack-agent run .ack/tasks/active/AX-001.yaml --agent A01
.ack/tools/ack-status ack
```

`prepare` creates write-worker repositories with `git clone --no-hardlinks`, task provenance, and a task-scoped branch. The runner rejects missing/mismatched provenance and shared canonical object inodes. The configured agent command is an argv list executed with `shell=False`; logical model aliases and provider authentication remain external. Workers can publish meaningful transitions with `ack-agent progress <phase> <concise-action>`.

## Redis state model

Each project uses collision-safe namespaced keys:

- `ack:<project>:agent:<agent-instance>` — status, phase, heartbeat, progress, task, result and commit metadata.
- `ack:<project>:task:<task-id>` — lifecycle and time-bounded lease ownership.
- `ack:<project>:events` — concise start, phase, blocked, failed and completed events.
- `ack:<project>:pl` — Axiom heartbeat, objective and basic scheduler state.

Redis must not contain secrets, code, full prompts, or reasoning traces. Heartbeat proves liveness; `progress_at_utc` changes only for meaningful milestones. Status tooling classifies heartbeat health and surfaces expired leases and stalled progress.

## Task lifecycle

1. Axiom creates a bounded task with root, base commit, role, logical model, skills, authority, risk, and acceptance criteria.
2. The worker validates the task/root, acquires a lease, publishes status and events, loads selective skills, and performs only authorised work.
3. Write workers use isolated project-local worktrees where appropriate, test, commit, and write a structured result.
4. An independent Tester or Reviewer checks normal/material work in proportion to risk.
5. Axiom inspects results, diff, commit, tests, and evidence; integrates and runs canonical checks; then records acceptance or rejection.

Independent uncertainty may be parallelised (up to four workers by default). Dependent mutation and canonical integration are serialised. Workers never spawn workers or merge canonical work.

## Recovery

After restart, recover from `PID.md`, `AXIOM.md`, `.ack/state/project.yaml`, Git, Redis, `.ack/tasks/`, `.ack/results/`, and relevant `.ack/decisions/`. Redis reconstructs live/expired ownership; Git and result files establish durable output awaiting acceptance. Chat history is not required.

## Self-hosted proof

ACK v0.1 proves itself inside `/srv/codex/ACK`: Axiom dispatches a read-only Scout, a bounded Builder improvement in an isolated write context, and an independent Tester/Reviewer. Each worker emits heartbeat, progress, events, and a result. Axiom observes the control plane, checks commits and tests, and alone records acceptance. Proof artifacts belong in project-local tasks, results, and evidence.

## Project layout

The portable kit contains its PID and Axiom doctrine plus `.ack/` rules, skills, templates, tasks/results/evidence/decisions, current state, runtime tools, and external configuration example. Keep additions necessary to dispatch, constrain, observe, recover, integrate, or accept work; ACK is deliberately not a general governance framework.
Worker output follows .ack/templates/result.schema.json. Structured JSON is YAML-compatible and ACK validates it again.
Bubblewrap makes read tasks read-only and exposes only the isolated clone to write tasks.
