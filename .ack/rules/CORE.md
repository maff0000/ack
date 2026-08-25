# ACK Core Worker Rules

1. Work only inside the assigned PID-defined `PROJECT_ROOT`; canonicalise paths and reject traversal or symlink escapes.
2. Work only within the assigned task scope and inspect the existing implementation before changing it.
3. Git is durable truth; create focused, meaningful work and never merge into the canonical branch.
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
18. Return concise structured results. Worker completion means only that the bounded task finished.
19. Only Axiom may ACCEPT or REJECT project work; workers use only `completed`, `blocked`, or `failed`.
20. If required work crosses `PROJECT_ROOT`, stop and report `blocked`.
