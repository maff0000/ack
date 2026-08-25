# Project Initiation Document — ACK v0.1

## Project

- **Name:** ACK — Axiom Control Kit
- **Purpose:** Provide a small, portable project-control kit for Axiom-led, LiteLLM-backed local coding agents.
- **Target outcome:** A copyable kit that safely dispatches bounded workers, exposes live state in Redis, and uses Git as durable evidence.
- **PROJECT_ROOT:** `/srv/codex/ACK`

## Scope

- Project-local doctrine, composable skills, task/result contracts, state, CLI worker control, Redis heartbeat/progress/events/leases, boundary enforcement, tests, and self-hosted proof.

## Non-goals

- Web UI, Kubernetes, distributed scheduling, recursive agents, generic workflow engines, heavyweight governance, RBAC, custom databases, or a telemetry platform.

## Architecture

The Architect owns direction and architecture. Axiom decomposes, delegates, integrates, tests, and accepts. Bounded workers operate inside explicit project roots. Redis carries live operational state; Git and project-local result/evidence files carry durable truth.

## Critical invariants

- All mutation is confined to the canonical, resolved `PROJECT_ROOT`; traversal and symlink escapes fail closed.
- Workers never merge canonical work, spawn workers, broaden scope, or accept/reject work.
- Runtime configuration and secrets stay outside application source.
- UTC is canonical across persistence, processes, APIs, and logs.
- ACK remains portable, project-local, and lightweight.

## Technical constraints

- Use small, understandable tooling and proven libraries only where justified.
- Model names are logical aliases selected by Axiom and routed externally through LiteLLM.
- Up to four worker slots by default; independent uncertainty may run concurrently, dependent mutation is serialised.

## Deployment assumptions

- A Git repository exists in the project root.
- Redis and a local-agent/LiteLLM command are externally configured when live control is used.
- Each adopting project customises this PID and `.ack/skills/project/PROJECT.md`.

## Security constraints

- No secrets, full prompts, reasoning traces, or code in Redis.
- Validate task-controlled paths and avoid shell command construction from untrusted strings.
- Missing authority or ambiguous mutation scope fails closed.

## Acceptance definition

ACK v0.1 is accepted when its required project kit and controls work, invariant tests pass, a self-hosted Scout → Builder → independent verification flow succeeds, Axiom records acceptance, and the repository is coherent and recoverable without chat history.
