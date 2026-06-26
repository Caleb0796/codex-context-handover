import json, os, subprocess, sys, tempfile, glob, time
from pathlib import Path

HOOK = str(Path(__file__).resolve().parent / "context-handover.py")
tmp = Path(tempfile.mkdtemp(prefix="ho-test-"))
ws = tmp / "workspace"; ws.mkdir()
hdir = tmp / "handovers"
sdir = tmp / "state"
# a fake transcript jsonl: first user message (goal) at head, tool output + token usage at tail
tx = tmp / "rollout.jsonl"
lines = []
lines.append(json.dumps({"item":{"type":"message","role":"user","content":[{"type":"text","text":"Migrate the OverviewPage from ADF to APEX."}]}}))
for i in range(50):
    lines.append(json.dumps({"item":{"type":"function_call","name":"exec_command","arguments":{"cmd":f"sed -n '1,20p' file{i}.txt"}}}))
lines.append(json.dumps({"item":{"type":"message","role":"assistant","content":[{"type":"text","text":"Done reading files; proceeding."}]}}))
lines.append(json.dumps({"item":{"type":"event","info":{"total_token_usage":{"total_tokens":7800000},"model_context_window":380000,"last_token_usage":{"total_tokens":50000}}}}))
tx.write_text("\n".join(lines), encoding="utf-8")

env = dict(os.environ, CODEX_HANDOVER_DIR=str(hdir), CODEX_HANDOVER_STATE_DIR=str(sdir))

def run(payload):
    p = subprocess.run([sys.executable, HOOK], input=json.dumps(payload), text=True,
                       capture_output=True, env=env)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)

def files():
    return sorted(glob.glob(str(hdir / "**" / "*.md"), recursive=True))

TA = "019f026b-aaaa"
TB = "019f0274-bbbb"
base = {"cwd": str(ws), "transcript_path": str(tx)}

# 1) PreCompact thread A, 5 times (simulates one long turn compacting repeatedly)
for _ in range(5):
    run({**base, "hook_event_name":"PreCompact", "model":"gpt-5.5", "thread_id":TA})
    time.sleep(0.01)
assert len(files()) == 1, f"FAIL: expected 1 file after 5 PreCompacts on one thread, got {len(files())}: {files()}"
print("PASS 1: 5 PreCompacts (thread A) -> 1 rolling file")

# 2) Second concurrent thread B
run({**base, "hook_event_name":"PreCompact", "model":"gpt-5.4", "thread_id":TB})
assert len(files()) == 2, f"FAIL: expected 2 files for 2 threads, got {len(files())}"
print("PASS 2: thread B -> separate file (2 total)")

# 3) Review subagent (same thread id as A) must NOT write/overwrite
a_path = [f for f in files() if TA in f][0]
before = Path(a_path).read_text()
run({**base, "hook_event_name":"PreCompact", "model":"codex-auto-review", "thread_id":TA})
assert len(files()) == 2, f"FAIL: review subagent created a file, now {len(files())}"
assert Path(a_path).read_text() == before, "FAIL: review subagent overwrote thread A's handover"
print("PASS 3: review subagent skipped (no new file, A unchanged)")

# 4) Content quality: goal captured from head, not generic fallback
assert "Migrate the OverviewPage from ADF to APEX." in before, "FAIL: original goal not captured"
assert "Continue the active user-requested Codex task." not in before.split("## Session Metadata")[0], "FAIL: fell back to generic goal"
print("PASS 4: original user goal captured in handover")

# 5) UserPromptSubmit thread A injects once
out = run({**base, "hook_event_name":"UserPromptSubmit", "model":"gpt-5.5", "thread_id":TA})
assert "hookSpecificOutput" in out and out["hookSpecificOutput"]["additionalContext"], "FAIL: no injection on first submit"
print("PASS 5: UserPromptSubmit injects handover once")

# 6) Second submit (no new compaction) -> deduped, no injection
out = run({**base, "hook_event_name":"UserPromptSubmit", "model":"gpt-5.5", "thread_id":TA})
assert "hookSpecificOutput" not in out, "FAIL: injected twice without a new compaction"
print("PASS 6: re-submit without new compaction -> no duplicate injection")

# 7) New compaction re-arms injection
time.sleep(0.02)
run({**base, "hook_event_name":"PreCompact", "model":"gpt-5.5", "thread_id":TA})
out = run({**base, "hook_event_name":"UserPromptSubmit", "model":"gpt-5.5", "thread_id":TA})
assert "hookSpecificOutput" in out, "FAIL: new compaction did not re-arm injection"
print("PASS 7: new compaction re-arms injection")

# 8) No repo pollution: handovers live outside the workspace
assert not glob.glob(str(ws / "handover-*.md")), "FAIL: handover files leaked into workspace"
print("PASS 8: no handover files written into the workspace/repo")

print("\nALL TESTS PASSED")
print("Handover files produced:", files())
