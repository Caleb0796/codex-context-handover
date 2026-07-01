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

def run(payload, extra_env=None):
    e = dict(env, **(extra_env or {}))
    p = subprocess.run([sys.executable, HOOK], input=json.dumps(payload), text=True,
                       capture_output=True, env=e)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)

def files():
    return sorted(glob.glob(str(hdir / "**" / "*.md"), recursive=True))

def injected(out):
    return out.get("hookSpecificOutput", {}).get("additionalContext", "")

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
assert "Continue the active user-requested Codex task." not in before.split("## Session metadata")[0], "FAIL: fell back to generic goal"
print("PASS 4: original user goal captured in handover")

# 5) UserPromptSubmit thread A injects once
out = run({**base, "hook_event_name":"UserPromptSubmit", "model":"gpt-5.5", "thread_id":TA})
assert injected(out), "FAIL: no injection on first submit"
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

# 9) gpt-5.6 custom_tool_call format: JS-wrapped commands, update_plan, and failures are parsed
tx6 = tmp / "rollout56.jsonl"
l6 = [
    json.dumps({"item": {"type": "user_message", "message": "Refactor the auth module and add tests."}}),
    json.dumps({"item": {"type": "custom_tool_call", "name": "exec", "call_id": "c1",
        "input": 'const r = await tools.update_plan({plan: [{step: "Read auth module", status: "completed"}, {step: "Write tests", status: "in_progress"}]});'}}),
    json.dumps({"item": {"type": "custom_tool_call", "name": "exec", "call_id": "c2",
        "input": 'const r = await tools.exec_command({cmd: "git status --short", workdir: "/x"}); text(r.output);'}}),
    json.dumps({"item": {"type": "custom_tool_call_output", "call_id": "c2",
        "output": [{"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"}, {"type": "input_text", "text": " M auth.py\n"}]}}),
    json.dumps({"item": {"type": "custom_tool_call", "name": "exec", "call_id": "c3",
        "input": 'const r = await tools.exec_command({cmd: "pytest tests/", workdir: "/x"});'}}),
    json.dumps({"item": {"type": "custom_tool_call_output", "call_id": "c3",
        "output": [{"type": "input_text", "text": "Output:\nE   assert False\nProcess exited with code 1\n"}]}}),
    json.dumps({"item": {"type": "agent_message", "message": "Tests are failing; investigating."}}),
    json.dumps({"item": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 100}, "model_context_window": 353400, "last_token_usage": {"total_tokens": 40000}}}}),
]
tx6.write_text("\n".join(l6), encoding="utf-8")
T6 = "019f1995-cccc"
run({"cwd": str(ws), "transcript_path": str(tx6), "hook_event_name": "PreCompact", "model": "gpt-5.6-sol", "thread_id": T6})
c6 = Path([f for f in files() if T6 in f][0]).read_text()
assert "git status --short" in c6, "FAIL: gpt-5.6 exec cmd (double-quoted) not captured"
assert "Write tests" in c6, "FAIL: gpt-5.6 JS-wrapped update_plan not extracted"
assert "pytest tests/" in c6, "FAIL: gpt-5.6 failing cmd not captured"
fail_section = c6.split("## Open failures")[1].split("\n##")[0] if "## Open failures" in c6 else ""
assert "pytest" in fail_section, f"FAIL: failing pytest not flagged as a failure; section was:\n{fail_section}"
print("PASS 9: gpt-5.6 custom_tool_call commands + JS update_plan + failure captured")

# 10) Tail-biased scan: on a rollout larger than the tail window, the goal (head)
#     AND the newest activity (tail) both survive; the middle is skippable.
txbig = tmp / "rollout-big.jsonl"
big = [json.dumps({"item":{"type":"user_message","message":"HEAD-GOAL: port the History screen."}})]
filler = json.dumps({"item":{"type":"function_call","name":"exec_command","arguments":{"cmd":"echo mid-file filler " + "x"*200}}})
big += [filler]*400
big.append(json.dumps({"item":{"type":"agent_message","message":"TAIL-MARKER: verifying export now."}}))
big.append(json.dumps({"item":{"type":"custom_tool_call","name":"exec","call_id":"t1",
    "input":'const r = await tools.exec_command({cmd: "sqlcl @tail-verify.sql"});'}}))
big.append(json.dumps({"item":{"type":"token_count","info":{"total_token_usage":{"total_tokens":1},"model_context_window":353400,"last_token_usage":{"total_tokens":250000}}}}))
txbig.write_text("\n".join(big), encoding="utf-8")
TBIG = "019fbig-tail"
run({"cwd": str(ws), "transcript_path": str(txbig), "hook_event_name":"PreCompact", "model":"gpt-5.6-sol", "thread_id":TBIG},
    extra_env={"CODEX_HANDOVER_TAIL_BYTES": "8192"})
cbig = Path([f for f in files() if TBIG in f][0]).read_text()
assert "HEAD-GOAL: port the History screen." in cbig, "FAIL: goal lost on tail-windowed scan"
assert "TAIL-MARKER" in cbig, "FAIL: tail assistant narrative lost"
assert "sqlcl @tail-verify.sql" in cbig, "FAIL: tail command lost"
assert "250,000 tokens" in cbig, "FAIL: tail token_count lost"
print("PASS 10: head goal + tail activity both survive a windowed scan of a big rollout")

# 11+12) Fernet blobs redacted; repeated polling calls collapsed
txn = tmp / "rollout-noise.jsonl"
noise = [json.dumps({"item":{"type":"user_message","message":"Run the F03 verification."}})]
for i in range(6):
    noise.append(json.dumps({"item":{"type":"mcp_tool_call_end","call_id":f"w{i}",
        "invocation":{"server":"agents","tool":"wait_agent","arguments":{"timeout_ms":30000}}}}))
noise.append(json.dumps({"item":{"type":"mcp_tool_call_end","call_id":"s1",
    "invocation":{"server":"agents","tool":"send_message",
                  "arguments":{"target":"review_f03","message":"gAAAAABqROFfOpD8ypDH2J" + "A"*180 + "="}}}}))
noise.append(json.dumps({"item":{"type":"function_call","name":"exec_command","call_id":"r1",
    "arguments":{"cmd":"head -5 install.sql"}}}))
txn.write_text("\n".join(noise), encoding="utf-8")
TN = "019fnoise-dd"
run({"cwd": str(ws), "transcript_path": str(txn), "hook_event_name":"PreCompact", "model":"gpt-5.6-sol", "thread_id":TN})
cn = Path([f for f in files() if TN in f][0]).read_text()
assert "gAAAAAB" not in cn, "FAIL: Fernet ciphertext leaked into handover"
assert "[encrypted]" in cn, "FAIL: redaction placeholder missing"
print("PASS 11: encrypted send_message payload redacted")
assert cn.count("wait_agent") == 1, f"FAIL: wait_agent should appear once, got {cn.count('wait_agent')}"
assert "(×6)" in cn, "FAIL: poll repeat count missing"
assert "head -5 install.sql" in cn, "FAIL: real command evicted"
print("PASS 12: 6 identical wait_agent polls collapsed to one line (×6)")

# 13) Automation boilerplate trimmed out of the Goal section
txa = tmp / "rollout-auto.jsonl"
auto_prompt = ("Automation: Sales Mapping ADF→APEX Ralph loop Automation ID: sales-mapping-adf-apex-ralph-loop "
               "Last run: 2026-07-01T08:50:07Z FIRST, if migration/COMPLETE.flag exists: print RALPH-LOOP-COMPLETE "
               "and exit immediately. Otherwise Acquire migration/.lock — if it exists and its mtime is < 15 min old, "
               "exit immediately; re-touch every ~5 min as heartbeat. Read AGENTS.md + migration/LEDGER.md." )
txa.write_text("\n".join([
    json.dumps({"item":{"type":"user_message","message":auto_prompt}}),
    json.dumps({"item":{"type":"agent_message","message":"Lock acquired; starting F04."}}),
]), encoding="utf-8")
TAUTO = "019fauto-ee"
run({"cwd": str(ws), "transcript_path": str(txa), "hook_event_name":"PreCompact", "model":"gpt-5.6-sol", "thread_id":TAUTO})
ca = Path([f for f in files() if TAUTO in f][0]).read_text()
goal_section = ca.split("## Goal")[1].split("\n##")[0]
assert "Sales Mapping ADF→APEX Ralph loop" in goal_section, "FAIL: automation identity lost from goal"
assert "migration/.lock" not in goal_section, "FAIL: lock-protocol boilerplate still in goal"
assert "boilerplate preamble omitted" in goal_section, "FAIL: no automation trim note"
print("PASS 13: automation goal keeps identity line, drops lock-protocol boilerplate")

# 14) PreToolUse is the mid-turn injector: injects a pending handover exactly once
TP = "019fptu-ff"
run({**base, "hook_event_name":"PreCompact", "model":"gpt-5.6-sol", "thread_id":TP})
out = run({**base, "hook_event_name":"PreToolUse", "model":"gpt-5.6-sol", "thread_id":TP, "tool_name":"exec"})
assert injected(out), "FAIL: PreToolUse did not inject pending handover"
assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse", "FAIL: wrong hookEventName for PreToolUse"
out = run({**base, "hook_event_name":"PreToolUse", "model":"gpt-5.6-sol", "thread_id":TP, "tool_name":"exec"})
assert "hookSpecificOutput" not in out, "FAIL: PreToolUse injected twice for one compaction"
print("PASS 14: PreToolUse injects mid-turn, exactly once per compaction")

# 15) SessionStart injects only for source=compact, and dedups against other injectors
TS = "019fss-gg"
run({**base, "hook_event_name":"PreCompact", "model":"gpt-5.6-sol", "thread_id":TS})
out = run({**base, "hook_event_name":"SessionStart", "source":"startup", "model":"gpt-5.6-sol", "thread_id":TS})
assert "hookSpecificOutput" not in out, "FAIL: SessionStart(startup) must not inject"
out = run({**base, "hook_event_name":"SessionStart", "source":"compact", "model":"gpt-5.6-sol", "thread_id":TS})
assert injected(out), "FAIL: SessionStart(compact) did not inject"
assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart", "FAIL: wrong hookEventName for SessionStart"
out = run({**base, "hook_event_name":"UserPromptSubmit", "model":"gpt-5.6-sol", "thread_id":TS})
assert "hookSpecificOutput" not in out, "FAIL: UserPromptSubmit re-injected after SessionStart consumed it"
print("PASS 15: SessionStart(compact) injects; startup doesn't; cross-event dedup holds")

# 16) SessionStart(compact) with NO handover file regenerates from the transcript
TR = "019fregen-hh"
out = run({**base, "hook_event_name":"SessionStart", "source":"compact", "model":"gpt-5.6-sol", "thread_id":TR})
ctx = injected(out)
assert ctx and "Migrate the OverviewPage from ADF to APEX." in ctx, "FAIL: no regeneration without handover file"
print("PASS 16: SessionStart(compact) regenerates from transcript when no file exists")

# 17) Output that merely QUOTES an exit code is not a failure
txq = tmp / "rollout-quote.jsonl"
txq.write_text("\n".join([
    json.dumps({"item":{"type":"user_message","message":"Check the deploy log."}}),
    json.dumps({"item":{"type":"custom_tool_call","name":"exec","call_id":"q1",
        "input":'const r = await tools.exec_command({cmd: "print-deploy-log --recent"});'}}),
    json.dumps({"item":{"type":"custom_tool_call_output","call_id":"q1",
        "output":[{"type":"input_text","text":"Script completed\nWall time 1 second\nOutput:\nstep A ok\nprevious step exited with code 137 yesterday\nstep B ok\nall green\n"}]}}),
]), encoding="utf-8")
TQ = "019fquote-ii"
run({"cwd": str(ws), "transcript_path": str(txq), "hook_event_name":"PreCompact", "model":"gpt-5.6-sol", "thread_id":TQ})
cq = Path([f for f in files() if TQ in f][0]).read_text()
fail_q = cq.split("## Open failures")[1].split("\n##")[0]
assert "exit 137" not in fail_q and "print-deploy-log" not in fail_q, f"FAIL: quoted exit code treated as failure:\n{fail_q}"
print("PASS 17: exit codes quoted inside command OUTPUT are not failures")

# 18) Compound JS block (update_plan + exec_command in ONE input) keeps the command
txc = tmp / "rollout-compound.jsonl"
txc.write_text("\n".join([
    json.dumps({"item":{"type":"user_message","message":"Fix the LOV bug."}}),
    json.dumps({"item":{"type":"custom_tool_call","name":"exec","call_id":"k1",
        "input":'await tools.update_plan({plan: [{step: "Patch LOV", status: "in_progress"}]}); const r = await tools.exec_command({cmd: "sqlcl @fix_lov.sql"}); text(r.output);'}}),
]), encoding="utf-8")
TC = "019fcomp-jj"
run({"cwd": str(ws), "transcript_path": str(txc), "hook_event_name":"PreCompact", "model":"gpt-5.6-sol", "thread_id":TC})
cc = Path([f for f in files() if TC in f][0]).read_text()
assert "sqlcl @fix_lov.sql" in cc, "FAIL: compound JS lost the exec command"
assert "Patch LOV" in cc, "FAIL: compound JS lost the plan"
print("PASS 18: compound update_plan+exec JS keeps both the plan and the command")

# 19) Subagent turns (agent_id) neither write nor consume
TG = "019fsub-kk"
run({**base, "hook_event_name":"PreCompact", "model":"gpt-5.6-sol", "thread_id":TG, "agent_id":"agent_1"})
assert not [f for f in files() if TG in f], "FAIL: subagent PreCompact wrote a handover"
run({**base, "hook_event_name":"PreCompact", "model":"gpt-5.6-sol", "thread_id":TG})
out = run({**base, "hook_event_name":"UserPromptSubmit", "model":"gpt-5.6-sol", "thread_id":TG, "agent_id":"agent_1"})
assert "hookSpecificOutput" not in out, "FAIL: subagent consumed the injection"
out = run({**base, "hook_event_name":"UserPromptSubmit", "model":"gpt-5.6-sol", "thread_id":TG})
assert injected(out), "FAIL: real session lost its injection to the subagent"
print("PASS 19: subagent turns neither write nor consume handovers")

# 20) Bracket probes ([ -f x ]) exiting non-zero are not failures
txp = tmp / "rollout-probe.jsonl"
txp.write_text("\n".join([
    json.dumps({"item":{"type":"user_message","message":"Bootstrap check."}}),
    json.dumps({"item":{"type":"function_call","name":"exec_command","call_id":"p1",
        "arguments":{"cmd":"[ -f /tmp/never-there ] && echo yes"}}}),
    json.dumps({"item":{"type":"function_call_output","call_id":"p1","output":"exited with code 1"}}),
]), encoding="utf-8")
TPR = "019fprobe-ll"
run({"cwd": str(ws), "transcript_path": str(txp), "hook_event_name":"PreCompact", "model":"gpt-5.6-sol", "thread_id":TPR})
cp = Path([f for f in files() if TPR in f][0]).read_text()
fail_p = cp.split("## Open failures")[1].split("\n##")[0]
assert "never-there" not in fail_p, f"FAIL: [ -f ... ] probe flagged as failure:\n{fail_p}"
print("PASS 20: bracket-test probes with exit 1 are not failures")

# 21) Lock/heartbeat/scratch writes are separated from substantive file changes
txl = tmp / "rollout-lock.jsonl"
txl.write_text("\n".join([
    json.dumps({"item":{"type":"user_message","message":"Advance F05."}}),
    json.dumps({"item":{"type":"patch_apply_end","success":True,"changes":{
        "migration/.lock":{"type":"update"},
        "migration/logs/heartbeat.log":{"type":"update"},
        "migration/features/F05.md":{"type":"update"}}}}),
]), encoding="utf-8")
TL = "019flock-mm"
run({"cwd": str(ws), "transcript_path": str(txl), "hook_event_name":"PreCompact", "model":"gpt-5.6-sol", "thread_id":TL})
cl = Path([f for f in files() if TL in f][0]).read_text()
changed_l = cl.split("## Files changed this session")[1].split("\n##")[0]
assert "[update] migration/features/F05.md" in changed_l, "FAIL: real change missing"
assert "[update] migration/.lock" not in changed_l, "FAIL: lock listed as a substantive change"
assert "omitted" in changed_l and ".lock" in changed_l, "FAIL: omitted note must NAME the filtered paths"
print("PASS 21: lock/heartbeat writes filtered from Files changed (named in the note)")

# 22) Legacy combined state file is honored (migration path)
TLEG = "019fleg-nn"
run({**base, "hook_event_name":"PreCompact", "model":"gpt-5.6-sol", "thread_id":TLEG})
leg_file = [f for f in files() if TLEG in f][0]
mtime = Path(leg_file).stat().st_mtime
# simulate: old hook already injected this exact version, recorded in the legacy file
(Path(sdir) / "context-handover-state.json").write_text(json.dumps(
    {"schema_version": 2, "threads": {TLEG: {"injected_mtime": mtime}}}), encoding="utf-8")
for p in (Path(sdir) / "threads").glob(f"{TLEG}.json"):
    p.unlink()  # no per-thread state yet -> must fall back to legacy
out = run({**base, "hook_event_name":"UserPromptSubmit", "model":"gpt-5.6-sol", "thread_id":TLEG})
assert "hookSpecificOutput" not in out, "FAIL: legacy injected_mtime ignored -> double injection"
print("PASS 22: legacy state file honored, no duplicate injection after migration")

# 23) PostCompact also writes (same path as PreCompact) and re-arms injection
TPC = "019fpc-oo"
run({**base, "hook_event_name":"PostCompact", "model":"gpt-5.6-sol", "thread_id":TPC})
assert [f for f in files() if TPC in f], "FAIL: PostCompact did not write a handover"
out = run({**base, "hook_event_name":"PreToolUse", "model":"gpt-5.6-sol", "thread_id":TPC, "tool_name":"exec"})
assert injected(out), "FAIL: PostCompact write did not arm PreToolUse injection"
print("PASS 23: PostCompact writes the handover and arms mid-turn injection")

# 24) Stale state/handover files get pruned by the write path
old_state = Path(sdir) / "threads" / "ancient-thread.json"
old_state.parent.mkdir(parents=True, exist_ok=True)
old_state.write_text("{}", encoding="utf-8")
old_md = hdir / "old-ws-slug" / "handover-ancient-thread.md"
old_md.parent.mkdir(parents=True, exist_ok=True)
old_md.write_text("old", encoding="utf-8")
ancient = time.time() - 60 * 86400
os.utime(old_state, (ancient, ancient)); os.utime(old_md, (ancient, ancient))
run({**base, "hook_event_name":"PreCompact", "model":"gpt-5.6-sol", "thread_id":TA})
assert not old_state.exists(), "FAIL: stale thread state not pruned"
assert not old_md.exists(), "FAIL: stale handover file not pruned"
print("PASS 24: stale state + handover files pruned on write")

# 25) A real prompt merely STARTING with "Reminder"/"Cron" is rendered verbatim
txr = tmp / "rollout-reminder.jsonl"
legit = ("Reminder emails are being sent twice to every user. Find the duplicate-send bug in notify.py, "
         "fix the scheduler double-registration, and add a regression test that fails on the old code. "
         "The bug appeared after the celery upgrade; check the beat schedule too. Do not touch the email templates. "
         "FIRST reproduce it locally with two workers, then fix, then run the full test suite.")
txr.write_text(json.dumps({"item":{"type":"user_message","message":legit}}), encoding="utf-8")
TREM = "019frem-pp"
run({"cwd": str(ws), "transcript_path": str(txr), "hook_event_name":"PreCompact", "model":"gpt-5.6-sol", "thread_id":TREM})
cr = Path([f for f in files() if TREM in f][0]).read_text()
goal_r = cr.split("## Goal")[1].split("\n##")[0]
assert "boilerplate preamble omitted" not in goal_r, "FAIL: legit Reminder prompt treated as automation"
assert "full test suite" in goal_r, "FAIL: legit prompt truncated/clause-cut"
print("PASS 25: prompt merely starting with 'Reminder' is rendered verbatim")

# 26) A .log file that is the DELIVERABLE stays visible in Files changed
txd = tmp / "rollout-logfixture.jsonl"
txd.write_text("\n".join([
    json.dumps({"item":{"type":"user_message","message":"Regenerate tests/fixtures/expected.log from the new parser."}}),
    json.dumps({"item":{"type":"patch_apply_end","success":True,"changes":{
        "tests/fixtures/expected.log":{"type":"update"}}}}),
]), encoding="utf-8")
TD = "019flogf-qq"
run({"cwd": str(ws), "transcript_path": str(txd), "hook_event_name":"PreCompact", "model":"gpt-5.6-sol", "thread_id":TD})
cd = Path([f for f in files() if TD in f][0]).read_text()
changed_d = cd.split("## Files changed this session")[1].split("\n##")[0]
assert "[update] tests/fixtures/expected.log" in changed_d, "FAIL: deliverable .log hidden as noise"
print("PASS 26: non-runtime .log deliverable stays in Files changed")

# 27) fail -> apply_patch -> retry: the collapsed command keeps the FAILURE
txf = tmp / "rollout-retry.jsonl"
txf.write_text("\n".join([
    json.dumps({"item":{"type":"user_message","message":"Make the build pass."}}),
    json.dumps({"item":{"type":"custom_tool_call","name":"exec","call_id":"m1",
        "input":'const r = await tools.exec_command({cmd: "mvn package -q"});'}}),
    json.dumps({"item":{"type":"custom_tool_call_output","call_id":"m1",
        "output":[{"type":"input_text","text":"Output:\nBUILD FAILURE compile error in TerritoryAMImpl.java\nProcess exited with code 1\n"}]}}),
    json.dumps({"item":{"type":"custom_tool_call","name":"exec","call_id":"m2",
        "input":'await tools.apply_patch({patch: "fix"});'}}),
    json.dumps({"item":{"type":"custom_tool_call","name":"exec","call_id":"m3",
        "input":'const r = await tools.exec_command({cmd: "mvn package -q"});'}}),
]), encoding="utf-8")
TF = "019fretry-rr"
run({"cwd": str(ws), "transcript_path": str(txf), "hook_event_name":"PreCompact", "model":"gpt-5.6-sol", "thread_id":TF})
cf = Path([f for f in files() if TF in f][0]).read_text()
fail_f = cf.split("## Open failures")[1].split("\n##")[0]
assert "mvn package" in fail_f and "BUILD FAILURE" in fail_f, f"FAIL: retry collapse hid the build failure:\n{fail_f}"
print("PASS 27: collapsed retry keeps the failing run's exit code and snippet")

# 28) Malformed override env vars degrade to defaults instead of crashing
p = subprocess.run([sys.executable, HOOK], input=json.dumps({**base, "hook_event_name":"PreToolUse",
                   "model":"gpt-5.6-sol", "thread_id":TA, "tool_name":"exec"}),
                   text=True, capture_output=True,
                   env=dict(env, CODEX_HANDOVER_TAIL_BYTES="16MB", CODEX_HANDOVER_HEAD_LINES=""))
assert p.returncode == 0 and json.loads(p.stdout).get("continue") is True, \
    f"FAIL: bad env var crashed the hook: rc={p.returncode} err={p.stderr[:200]}"
print("PASS 28: malformed env overrides fall back to defaults (exit 0, valid JSON)")

# 29) SessionStart(compact) with a consumed-but-stale file re-injects (write hooks
#     failed for the newest compaction), but skips when the injection was recent
TSS = "019fssre-ss"
run({**base, "hook_event_name":"PreCompact", "model":"gpt-5.6-sol", "thread_id":TSS})
out = run({**base, "hook_event_name":"UserPromptSubmit", "model":"gpt-5.6-sol", "thread_id":TSS})
assert injected(out)
out = run({**base, "hook_event_name":"SessionStart", "source":"compact", "model":"gpt-5.6-sol", "thread_id":TSS})
assert "hookSpecificOutput" not in out, "FAIL: SessionStart re-injected despite a recent injection"
st_path = Path(sdir) / "threads" / f"{TSS}.json"
st = json.loads(st_path.read_text())
st["injected_at"] = "2026-06-01T00:00:00-07:00"   # pretend the last injection was a month ago
st_path.write_text(json.dumps(st), encoding="utf-8")
out = run({**base, "hook_event_name":"SessionStart", "source":"compact", "model":"gpt-5.6-sol", "thread_id":TSS})
assert injected(out), "FAIL: SessionStart(compact) lost the injection when write hooks were skipped"
print("PASS 29: SessionStart(compact) recovers a compaction whose write hooks failed")

print("\nALL TESTS PASSED")
print("Handover files produced:", files())
