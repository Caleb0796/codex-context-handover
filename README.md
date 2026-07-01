# codex-context-handover

A [Codex](https://openai.com/codex/) hook that survives **auto-compaction**. When Codex
compacts a long thread, useful working context can get summarized away. This hook writes a
**rolling handover file** around each compaction and **re-injects it at the earliest boundary
Codex offers** — including *mid-turn*, on the model's next tool call — so the model continues
from a structured summary instead of a lossy one.

One script handles all five ends:

| Event | What it does |
|-------|--------------|
| `PreCompact` | Writes/overwrites a per-thread handover summarizing the session state |
| `PostCompact` | Rewrites it right after compaction succeeds (covers paths where PreCompact may not fire) |
| `PreToolUse` | **Mid-turn injector**: after an auto-compaction inside a long agentic turn, the model's next tool call injects the handover. This is the only boundary that exists for single-turn automation runs, where no follow-up prompt ever comes. Cheap no-op (~50 ms) otherwise. |
| `SessionStart` | Codex queues a `SessionStart(source="compact")` hook after **every** compaction; this injects at the next turn start (same turn for pre-turn compactions), regenerated from the live transcript |
| `UserPromptSubmit` | Interactive safety net — injects on your next prompt, regenerated from the live transcript so it is never stale |

Exactly **one injection per compaction**: whichever event fires first consumes it (tracked per
thread via the handover file's mtime). Why not inject from `PreCompact` itself? Codex's
`PreCompact`/`PostCompact` hook outputs support no `additionalContext` — they can only observe
(verified against the binary's embedded hook schemas). The three injectors above are the
supported paths.

## Why not just use Codex's built-in compaction?

Codex already summarizes on compaction, but this hook adds a durable, structured, **resumption-focused**
artifact you (and the model) can rely on, with a few deliberate design choices that avoid common failure modes:

- **One rolling file per (workspace, thread)** — overwritten, never accumulated. A single long agentic
  turn that compacts 15× produces **one** file, always current (not 15 stale files).
- **Stored outside your repo** (`~/.codex/handovers/<workspace-slug>/handover-<thread>.md`) so it never
  shows up in `git status`. Override with `CODEX_HANDOVER_DIR`.
- **Per-thread state** — concurrent sessions / subagents in the same workspace never clobber each
  other's "latest" / "injected" tracking.
- **Skips ephemeral review/guardian subagents** (model matching `review|guardian`) so they don't
  overwrite the real session's handover.
- **Inject-once-per-compaction** via the handover file's mtime.

## What's in a handover

The content is built from **authoritative transcript signals**, not regex guesses:

- **Goal** — the original `user_message` (recovered from a `compacted` event's `replacement_history`
  if the thread was already compacted), filtering out `<turn_aborted>` / `<user_instructions>` / AGENTS.md markers.
- **Latest instruction** — the most recent user prompt (when different from the goal).
- **Where things stand** — recent `agent_message` turns (deduped).
- **Current plan** — the latest `update_plan` step list with statuses (▶ in-progress, ☐ pending, ✓ done).
- **Files changed this session** — from `patch_apply_end` (add/update/delete), not a path scrape.
- **Open failures / blockers** — non-zero command exits paired via `call_id`, **excluding** probe
  commands (`test`/`ls`/`grep`/`rg`/`find`…) whose non-zero exit is normal control flow; plus turn-aborts and failed patches.
- **Recent commands** — with ✓ / ✗ exit markers. Consecutive identical calls (agent polling like
  `wait_agent`) collapse into one `(×N)` line — keeping the failing run's exit code if any repeat failed —
  and encrypted inter-agent payloads (Fernet `gAAAA…` tokens) are redacted.
- **Automation-aware goal** — scheduled/Ralph-loop runs open with framework boilerplate (lock
  protocol, heartbeats); the goal keeps the identifying headline and drops the plumbing. Guarded
  structurally (requires `Automation ID:`/lock markers), so a prompt merely starting with
  "Reminder …" is rendered verbatim.
- **Noise-filtered file lists** — locks, heartbeat logs, `logs/*.log`, and browser scratch dirs are
  moved out of *Files changed* and the git status into a named "omitted" note.
- **Key files read**, **context % at compaction**, and a compact **git** summary.

### Model formats

Works across model transcript formats. Older models emit `function_call` items; **gpt-5.6**
(`sol`/`terra`/`luna`) wraps every tool in a JS exec harness as a `custom_tool_call` —
`tools.exec_command({cmd: "…"})`, `tools.update_plan({plan:[…]})`, etc. The hook parses both,
including backtick template-literal commands, multi-command JS scripts, list-shaped
`custom_tool_call_output`, and `mcp_tool_call_end` MCP calls.

## Install

Quick path:

```bash
./install.sh   # copies the hook to ~/.codex/hooks/, chmods it, prints the config block to add
```

Or manually:

```bash
mkdir -p ~/.codex/hooks
cp context-handover.py ~/.codex/hooks/
chmod +x ~/.codex/hooks/context-handover.py
```

Then add to `~/.codex/config.toml`:

```toml
[hooks]
PreCompact = [{ hooks = [{ type = "command", command = "/ABSOLUTE/PATH/TO/.codex/hooks/context-handover.py", async = false, statusMessage = "Writing context handover" }] }]
PostCompact = [{ hooks = [{ type = "command", command = "/ABSOLUTE/PATH/TO/.codex/hooks/context-handover.py", async = false, statusMessage = "Refreshing context handover" }] }]
SessionStart = [{ hooks = [{ type = "command", command = "/ABSOLUTE/PATH/TO/.codex/hooks/context-handover.py", async = false, statusMessage = "Checking for context handover" }] }]
PreToolUse = [{ hooks = [{ type = "command", command = "/ABSOLUTE/PATH/TO/.codex/hooks/context-handover.py", async = false, timeout = 30 }] }]
UserPromptSubmit = [{ hooks = [{ type = "command", command = "/ABSOLUTE/PATH/TO/.codex/hooks/context-handover.py", async = false, statusMessage = "Injecting latest context handover" }] }]
```

`PreToolUse` runs on every tool call, so it carries no `statusMessage` and a short timeout; its
no-op path is a couple of file stats (~50 ms including interpreter startup).

Use the absolute path to the script (Codex hook commands aren't shell-expanded, so `~` won't work).
Codex will ask you to trust the hooks the first time (Settings -> Hooks -> trust).

## Configuration (env vars)

| Variable | Default | Purpose |
|----------|---------|---------|
| `CODEX_HANDOVER_DIR` | `~/.codex/handovers` | Where rolling handovers are written |
| `CODEX_HANDOVER_STATE_DIR` | `~/.codex/hooks/state` | Where per-thread injection state is stored |
| `CODEX_HANDOVER_TAIL_BYTES` | 16 MiB | Tail window scanned for recent activity on huge rollouts |
| `CODEX_HANDOVER_HEAD_LINES` | 30000 | Head lines scanned for the original goal / patch inventory |

No external dependencies — standard-library Python 3 only.

## Test

```bash
python3 test-context-handover.py
```

29 cases: rolling-file semantics, per-thread isolation, review-subagent + subagent (`agent_id`)
guards, goal capture (incl. automation-boilerplate trimming and its verbatim guard), inject-once +
re-arm across all three injectors (`PreToolUse` / `SessionStart(compact)` / `UserPromptSubmit`),
head+tail windowed scan of oversized rollouts, Fernet redaction, poll collapsing (failure-preserving),
quoted-exit-code false positives, `[ -f x ]` probes, lock/heartbeat noise filtering, legacy-state
migration, `PostCompact` arming, malformed env overrides, and stale-file pruning.

## Companion: compacting at ~75% instead of ~17%

Separate from this hook, if Codex auto-compacts far too early (e.g. at ~17% of the window), the cause is
usually the per-model `auto_compact_token_limit` being unset in your model catalog, so Codex falls back
to a low default. Run:

```bash
./set-auto-compact-limits.py            # sets each model to 0.75*window + reserve; --dry-run to preview
codex debug models                      # verify the effective limits
```

This sets each model's limit to **75% of the usable window plus the output reserve** (~34k at Extra-High
effort) so the *real* trigger lands at 75% — because Codex fires when `live_context + output_reserve >= limit`,
not at the raw limit. See [NOTES.md](NOTES.md) for the full diagnosis of all three symptoms
(file pileup → ~17% compaction → 66% vs 75%).

## Repo contents

| file | |
|------|---|
| `context-handover.py` | the hook (PreCompact/PostCompact writers + PreToolUse/SessionStart/UserPromptSubmit injectors) |
| `test-context-handover.py` | 29-case regression test |
| `install.sh` | installs the hook and prints the config block |
| `set-auto-compact-limits.py` | sets per-model auto-compact limits for ~75% compaction |
| `NOTES.md` | root-cause diagnosis notes |

## License

MIT — see [LICENSE](LICENSE).
