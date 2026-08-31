---
type: agent-context
owner: repo-agent
scope: repo/agora
reviewed: 2026-08-31
---

# Issue tracker (for agents)

- **Tracker:** GitHub Issues, via the `gh` CLI.
- **Repo:** `determlab/agora` — `/agent-loop:init` fills this in; otherwise run
  `gh repo view --json nameWithOwner`.
- **Where work comes from:** issues filed with the `feature.yml` / `bug.yml` forms
  (goal + testable acceptance criteria). The loop only runs on issues a maintainer
  has labelled `agent:go`.
- **Linking:** PRs close their issue with `Closes #<n>` in the body.
- **Branches:** the loop works on `agent/<issue#>-<slug>` off the base branch.

See `triage-labels.md` for the label lifecycle.
