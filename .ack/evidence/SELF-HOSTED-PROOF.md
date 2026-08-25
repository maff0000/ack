# ACK v0.1 self-hosted proof

UTC date: 2026-08-25

- Runtime: authenticated Redis PING and ACK namespaced write/read succeeded. LiteLLM Responses and the configured Codex provider returned successful harmless inference for `trinity-fast`, `trinity-core`, and `trinity-deep`; credentials remained external and ignored.
- Scout: `AX-SELF-001`, instance `S08`, completed read-only. Redis recorded start, phase progress, and completion at 09:34:58–09:35:01Z. Axiom consumed and accepted `.ack/results/AX-SELF-001.yaml`.
- Builder: `AX-SELF-002-R1`, instance `B13`, ran in an ACK-allocated `git clone --no-hardlinks` repository on `ack/AX-SELF-002-R1/worker`. Redis recorded start, progress, and completion at 10:01:58–10:02:03Z. ACK scope-validated `README.md` and created worker commit `2bb3bacce7fb4906b736fc5cd41f8bdd94677942`.
- Integration: Axiom inspected the worker diff, fetched the isolated branch, and deliberately integrated it as `de5e0cba89537c888a5fd747f1b51ee4434b6935`.
- Independent review: `AX-SELF-003-R1`, instance `R07`, independently checked Git scope and the durable Builder result. Redis recorded start, progress, and completion at 10:08:51–10:08:55Z. Result: no findings.
- Acceptance: Axiom verified both documentation statements exactly once, reviewed commit/result traceability, and ran `python3 -m unittest discover -v`: 39 tests passed, 0 failed. `AX-SELF-001`, `AX-SELF-002-R1`, and `AX-SELF-003-R1` are ACCEPTED by Axiom.
- Control plane: `ack-status` showed live HEALTHY/PROGRESSING and ALIVE_BUT_STALLED classifications during longer attempts, active leases while running, and completed result/commit paths for successful workers. Redis Stream contains `task_started`, `phase_changed`, `task_failed`, and `task_completed` evidence.
- Boundary: PID `PROJECT_ROOT` is `/srv/codex/ACK`. Automated tests accept in-root paths and reject `..` plus symlink escapes. Bubblewrap makes the host read-only and grants write authority only to the task's isolated project-local clone. Canonical Git administration and shared objects are not worker-writable.
- Rejections were preserved where useful. Failed or fabricated local-model claims were never accepted or integrated; ACK's result, scope, lease, and commit checks failed closed.
