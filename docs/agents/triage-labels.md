# Labels (for agents)

The agent-loop uses four labels as its **only** state. Don't add parallel state.

| Label               | Meaning                                          | Who sets it        |
|---------------------|--------------------------------------------------|--------------------|
| `agent:go`          | Queued for the loop. The trigger.                | A human (maintainer) |
| `agent:working`     | Loop is running on it. Don't touch.              | The loop           |
| `agent:done`        | Done — PR merged, or auto-merge armed on green CI.| The loop           |
| `agent:needs-human` | Flagged. A comment explains why. Needs a human.  | The loop           |

## Lifecycle

```
(none) --human--> agent:go --loop picks up--> agent:working
                                                  |
                 PASS + merge gate ok ----------> agent:done
                 hard-stop / no CI gate --------> agent:needs-human (+ comment)
                 rounds exhausted --------------> agent:needs-human (+ draft PR)
```

`agent:needs-human` is terminal until a human acts: read the comment, then either
close it, open a follow-up, or re-queue by adding `agent:go` again.

> If your repo already uses a triage vocabulary (`needs-triage`, `ready-for-agent`,
> …), keep it — the `agent:*` labels sit alongside it. `agent:go` is just "this is
> the next thing the loop should pick up."
