# ADR-001 — Isolated worker repositories

- **Decision:** Write workers use ACK-allocated, `--no-hardlinks` project-local Git clones under `.ack/worktrees/<task>` rather than linked Git worktrees. Axiom alone fetches and integrates accepted worker commits. Runner verifies allocation provenance and rejects canonical object inode sharing.
- **Context:** A linked worktree must write the canonical repository's shared object database and administrative paths to create commits. Granting that access would let a bounded worker corrupt canonical Git truth.
- **Rationale:** An isolated clone keeps worker source, refs, index, and objects inside one sandboxed directory while preserving normal Git commits and attributable task branches.
- **Rejected alternatives:** Writable canonical `.git` access was rejected as unsafe. A private object overlay on a linked worktree was rejected because it can leave canonical refs pointing to objects canonical Git cannot resolve.
- **Consequences:** Axiom creates the clone/branch, then fetches or cherry-picks accepted commits. This costs modest local disk space but keeps canonical Git administration outside worker authority.
- **Architect approval/reference:** ACK v0.1 build brief: project-boundary enforcement, worker isolation, canonical-branch protection, and Axiom-owned integration. This implementation choice stays inside the approved architecture.
- **UTC date:** 2026-08-25
