# ACK Core Worker Rules

1. Work only inside the assigned PID-defined `PROJECT_ROOT`; canonicalise paths and reject traversal or symlink escapes.
2. Work only within the assigned task scope and inspect the existing implementation before changing it.
3. Git is durable truth. Workers make focused scoped filesystem changes but never commit or merge; ACK Runner/controller alone creates the task-scoped worker-output commit after mechanical validation.
4. Configuration does not belong in application source. Supply runtime/environment configuration externally and fail loudly when required configuration is missing.
5. Never store secrets in source, commits, results, logs, examples, Redis, prompts, or evidence.
6. Repeated work uses reusable, parameterised components rather than per-instance copies.
7. Prefer cohesive components and clear responsibility boundaries. Do not unnecessarily enlarge monoliths, or split code mechanically to satisfy arbitrary size limits.
8. Refactor when responsibilities are unrelated, coupling is excessive, independent testing is impaired, or code is unsafe to reason about.
9. Projects are sovereign. Cross-project interaction uses explicit authorised contracts such as APIs, Redis, SQL, events, or files.
10. UTC is canonical across persistence, processes, APIs, and logs; local time is display-only.
11. Prefer established, tested libraries, methods, and standards over casual invention.
12. Preserve approved architecture unless explicitly authorised to change it.
13. Do not fix unrelated issues; report them to Axiom.
14. Test changed behaviour. Never fabricate commands, results, runtime state, or evidence.
15. Fail loudly on missing dependencies/configuration, invalid authority, or incompatible assumptions.
16. Running services have meaningful names, visible startup/shutdown/failure events, UTC structured logs where practical, bounded retention, and no silent failures or sensitive dumps; container logs normally use stdout/stderr.
17. Never spawn another agent.
18. Return concise structured results. Acceptance criteria are required actions and outcomes, not facts to repeat. Worker `completed` means the bounded task was actually executed, including required mutation and testing.
19. Only Axiom may ACCEPT or REJECT project work; workers use only `completed`, `blocked`, or `failed`.
20. If required work crosses `PROJECT_ROOT`, stop and report `blocked`.
21. Use the ACK-prepared project dependency environment. Do not create virtual environments or install project dependencies inside a worker worktree.
22. Liveness is not progress. ACK bounds worker execution by progress freshness, wall-clock duration, and available inference/resource budgets; stale progress is surfaced as `alive_but_stalled`, while a ceiling stops/escalates the worker and preserves its evidence without redispatch.

## Delivery decomposition doctrine

Axiom carries planning intelligence; workers receive narrow executable deliveries. A work item should be the
smallest independently verifiable delivery that materially advances the PID: one clear objective, one narrow
mutation surface, one focused verification target, and one obvious completion condition. Prefer roughly one or
two primary source files, one behaviour change, one focused test command, and a small bounded acceptance set.

Before dispatch, PL actively considers decomposition when work combines independent behaviours, schema/CRUD/
validation/integration, UI/persistence/API, multiple subsystem concepts, more than two primary implementation
files, a large acceptance list, or several natural acceptance points. These are planning heuristics, not brittle
limits. ACK validation may surface advisories, but does not reject a task solely for size; informed PL judgment
decides. Each accepted delivery becomes the governed base commit for the next delivery. Do not compensate for a
broad task by expanding worker prompts.
