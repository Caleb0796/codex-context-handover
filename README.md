# codex-context-handover

A [Codex](https://openai.com/codex/) hook that survives **auto-compaction**. When Codex
compacts a long thread, useful working context can get summarized away. This hook writes a
**rolling handover file** just before each compaction and **re-injects it** on your next prompt,
so the model continues from a structured summary instead of a lossy one.

One script handles both ends:

| Event | What it does |
|-------|--------------|
| `PreCompact` | Writes/overwrites a per-thread handover summarizing the session state |
| `UserPromptSubmit` | Injects that handover once per compaction as `additionalContext` |

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
- **Recent commands** — with ✓ / ✗ exit markers.
- **Key files read**, **context % at compaction**, and a compact **git** summary.

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
UserPromptSubmit = [{ hooks = [{ type = "command", command = "/ABSOLUTE/PATH/TO/.codex/hooks/context-handover.py", async = false, statusMessage = "Injecting latest context handover" }] }]
```

Use the absolute path to the script (Codex hook commands aren't shell-expanded, so `~` won't work).
Codex may ask you to trust the hook the first time.

## Configuration (env vars)

| Variable | Default | Purpose |
|----------|---------|---------|
| `CODEX_HANDOVER_DIR` | `~/.codex/handovers` | Where rolling handovers are written |
| `CODEX_HANDOVER_STATE_DIR` | `~/.codex/hooks/state` | Where per-thread injection state is stored |

No external dependencies — standard-library Python 3 only.

## Test

```bash
python3 test-context-handover.py
```

Covers: 5 PreCompacts on one thread → 1 file; two threads → 2 files; review subagent skipped;
goal captured; inject-once + re-arm-on-new-compaction; nothing written into the workspace.

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
| `context-handover.py` | the hook (PreCompact writer + UserPromptSubmit injector) |
| `test-context-handover.py` | 8-case regression test |
| `install.sh` | installs the hook and prints the config block |
| `set-auto-compact-limits.py` | sets per-model auto-compact limits for ~75% compaction |
| `NOTES.md` | root-cause diagnosis notes |

## License

MIT — see [LICENSE](LICENSE).
