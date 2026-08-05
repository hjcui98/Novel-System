# Current task

- Task: correct Stage 2M evidence read grain, pack exact raw slices, and close claims on demand
- Codex: architect, root-cause and direction owner, final reviewer
- OpenCode default `build`: implementation, tests, real API execution, monitoring, diagnosis,
  repair, and implementation evidence reporting
- Approval: the user invokes `/implement`
- Merge: Codex after acceptance

## User requirements

- Keep the interaction simple: Codex designs, OpenCode executes, Codex reviews.
- Give OpenCode one substantial task rather than many tiny slices.
- Use the canonical project documents as read-only authority; report real results in
  `.agent/implementation.md` for Codex to accept and integrate.
- Use and monitor the real Qwen3.6 API at `http://127.0.0.1:8002/v1`.
- If Codex review does not pass, Codex supplies the next technical/architectural direction and
  OpenCode continues. Do not impose an arbitrary repair-count limit.
- Do not add a fixed small Need cap or mistake report plumbing for retrieval quality.
- Stage 3, P3, formal Gate, C40, C80, and A/B/C are not part of this task.
