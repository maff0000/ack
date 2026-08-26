# ACK Operator HOWTO

## First project start

1. Work with the CGPT Architect to produce the final approved `PID.md`.
2. Create the GitHub repository.
3. Choose the canonical project path under `/srv/codex`.
4. Clone the repository and install the approved PID:

```bash
git clone https://github.com/OWNER/REPOSITORY.git /srv/codex/PROJECT
install -m 0644 /path/to/approved/PID.md /srv/codex/PROJECT/PID.md
```

5. From a normal host shell—not an existing Codex/Axel sandbox—run:

```bash
/srv/codex/ACK/.ack/tools/ack-pl bootstrap /srv/codex/PROJECT
```

The bootstrap validates the PID-defined root, verifies or initializes Git, installs the portable ACK kit without overwriting existing project truth, creates concise project essentials/state, prepares ignored runtime configuration, and runs the mandatory PL preflight. When every capability is ready it launches ordinary managed-policy Codex for Axiom reasoning and attaches the project-local, pre-authorised ACK PL MCP bridge. The host-side launcher and bridge retain guarded Git and worker-control authority; Codex's built-in command sandbox remains enforced.

If preflight reports missing external runtime variables, inject them through the established host runtime mechanism without putting credentials in Git, then rerun the same bootstrap command. ACK output redacts configured credentials.

6. Answer only genuine Axiom questions caused by missing or contradictory project facts.
7. Let Axiom lead Scout, bounded implementation, independent verification, integration, and acceptance within the approved PID.
8. Inspect GitHub at meaningful milestones.

## Return to an existing project

From a normal host shell, run:

```bash
/srv/codex/ACK/.ack/tools/ack-pl resume /srv/codex/PROJECT
```

Resume validates requested, approved PID, project-state, and Git roots; runs the trusted host preflight; and starts a fresh Axiom session with explicit durable-recovery instructions. It does not require chat history and never regenerates project truth. If Redis is unavailable but host/Git/worker capabilities are sound, resume continues from Git and project state with live control reported as degraded.

The shell and Codex working directory are context, not authority. `ACK_PROJECT_ROOT`, PID, project state, and Git root must agree. Axiom is the trusted Project Lead; its narrow bridge exposes validated ACK/Git operations but no general shell or acceptance command. ACK workers remain confined by ACK's role-derived bubblewrap profiles.
