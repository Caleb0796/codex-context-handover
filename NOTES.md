# Diagnosis notes

How this hook and the companion auto-compaction settings came to be — the three symptoms that
were debugged and what actually caused each. Useful if you hit the same behavior.

## Symptom 1 — dozens of handover files piling up in the repo

A naive PreCompact hook produced 27+ `handover-<timestamp>.md` files in the working tree in under an hour.

**Root causes**
1. **Per-minute timestamped filenames, never replaced.** A single long agentic turn auto-compacts
   *many* times (a 7.5M-token turn compacted ~13×) — each compaction wrote a new file.
2. **No session isolation.** Two concurrent Codex sessions plus the `codex-auto-review` guardian
   subagent all wrote into the same folder and shared one global state file, interleaving and
   clobbering each other's "latest handover" tracking.
3. **Written into the working tree**, so they showed up in `git status`.

**Fix (in `context-handover.py`)**
- One **rolling** file per `(workspace, thread)`, overwritten each compaction → 13 compactions = 1 file.
- Stored **outside** the repo at `~/.codex/handovers/<workspace-slug>/handover-<thread>.md`.
- **Per-thread** state so concurrent sessions don't collide.
- **Skip** review/guardian subagents (model matching `review|guardian`).

Also note: the `trusted_hash` entries Codex writes under `[hooks.state]` in `config.toml` hash the
**hook definition** (event + command), **not the script body** — so editing the `.py` does *not*
require re-trusting the hook.

## Symptom 2 — auto-compacting at ~17% of the window

With the hook fixed, Codex was still compacting when only ~17% of the context window was used
(e.g. ~65k of a 380k window), wiping context before any real work accumulated.

**Root cause.** The auto-compaction trigger is driven by the per-model `auto_compact_token_limit`
field inside the catalog referenced by `model_catalog_json`. That field was **null** for every
model, so Codex fell back to a low default. The top-level `model_auto_compact_token_limit` config
key does **not** drive this path.

Verified by isolating a Codex run: catalog `auto_compact_token_limit = 6000` → compacted as soon as
context crossed 6k; `= 200000` → no compaction; `= 40000` → compacted at exactly 40,400.

**Fix.** Set `auto_compact_token_limit` per model in the catalog (see `set-auto-compact-limits.py`).
Confirm effective values with `codex debug models`.

## Symptom 3 — compacting at 66%, not the intended 75%

After setting the limit to 75% of the window (285,000 of 380,000 for gpt-5.5), it compacted at
**66%** (251,420), not 75%.

**Root cause.** The trigger is `live_context + output_reserve >= limit`, not `live_context >= limit`.
Codex reserves room for the model's response, and at **Extra-High** reasoning effort that reserve is
large. Measured: limit 285,000 fired at live context 251,420 → reserve = **33,580** (~34k).
(An earlier proxy test used `low` effort, where the reserve is tiny, which is why it appeared to fire
right at the limit.)

**Fix.** Set the limit to `0.75 * usable_window + reserve` so the *real* trigger lands at ~75%:

| model | usable window | limit | real trigger |
|-------|---------------|-------|--------------|
| gpt-5.5 | 380,000 | 319,000 | ~285,000 (75%) |
| gpt-5.4 | 950,000 | 746,500 | ~712,500 (75%) |
| 272k-window models | 258,400 | 227,800 | ~193,800 (75%) |

The reserve scales with reasoning effort, so at lower effort it will compact a bit *later* than 75%
(closer to the raw limit). `set-auto-compact-limits.py --reserve N` lets you tune it.

## Verifying

- `codex debug models` — shows the effective per-model `auto_compact_token_limit`.
- Open a **fresh thread** (catalog changes only apply to new threads) and watch the context indicator —
  it should climb to ~75% before "Automatically compacting context."

## Symptom 4: handovers written but never read (the injection gap)

Even with symptoms 1–3 fixed, two days of production evidence (177 threads) showed the handover
content going unused: of the handovers on disk, only **one** had ever actually been injected.

**Why.** Injection was wired only to `UserPromptSubmit`. But ~95% of thread volume was
automation runs (Ralph-loop style): a single long agentic turn that auto-compacts **mid-turn**
and then ends without any further user prompt. The handover was written 59 seconds before the
compaction — and orphaned forever. Interactive threads fared little better: by the time the user
typed the next prompt, the pre-compaction snapshot was stale (the thread had kept working for
hours after compacting).

**What the hook API actually offers** (verified against the Codex 0.142.5 binary's embedded hook
schemas and the `rust-v0.142.5` source):

- `PreCompact`/`PostCompact` outputs have **no** `additionalContext` — writers can only observe.
- Every compaction (local and remote) queues a **`SessionStart` hook with `source="compact"`**,
  consumed at the next `run_turn` start. `SessionStart` **does** support `additionalContext`
  (injected as a developer-role message). For a *pre-turn* compaction it fires in the same turn,
  before sampling.
- For a *mid-turn* compaction, the queued `SessionStart(compact)` won't fire until the next turn —
  which never comes for a single-turn automation. The only true mid-turn injection point is
  **`PreToolUse`/`PostToolUse` `additionalContext`**, recorded into history immediately.

**Fix.** Three injectors sharing one per-thread dedup (handover mtime vs `injected_mtime`), so each
compaction is injected exactly once, at the earliest boundary that exists:

1. `PreToolUse` — the model's next tool call after a mid-turn compaction (~seconds later);
2. `SessionStart(compact)` — turn boundaries, regenerated from the live transcript;
3. `UserPromptSubmit` — interactive prompts, also regenerated so it is never stale.

**Trusting the hooks headlessly.** Codex gates hooks behind `[hooks.state]` `trusted_hash`
entries. The hash is `sha256:` over the canonical-JSON of a normalized identity
`{event_name, matcher?, hooks:[{type, command, timeout (default 600), async, statusMessage?}]}`
(see `command_hook_hash` + `version_for_toml` in the Codex source) — so you can precompute it and
write it next to the hook definition instead of clicking through the trust dialog. Verify with the
app-server: `codex app-server` → `hooks/list` shows `trustStatus` per hook.
