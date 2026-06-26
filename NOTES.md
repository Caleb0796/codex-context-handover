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
