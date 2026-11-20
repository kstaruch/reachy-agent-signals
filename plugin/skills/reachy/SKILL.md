---
name: reachy
description: Control the Reachy Mini agent signals - turn reactions on or off, toggle quiet mode, set presence (awake only while your phone is near), show status.
argument-hint: "[on | off | quiet [on|off] | presence [off|auto|awake|asleep] | status]"
disable-model-invocation: true
allowed-tools: Bash(reachy-signals *)
---

Run this one command with the Bash tool and nothing else:

```
reachy-signals $ARGUMENTS
```

If the arguments above are empty, run `reachy-signals status` instead. Accept only `on`, `off`, `quiet`, `quiet on`, `quiet off`, `presence`, `presence off`, `presence auto`, `presence awake`, `presence asleep` or `status`; for anything else, don't run a command and list those options.

Then reply with the command's output, in a code block, and no commentary. If it failed, add one short sentence saying what to do (for example, start the bridge with `reachy-signals serve`).
