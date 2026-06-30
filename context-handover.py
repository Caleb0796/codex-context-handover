#!/usr/bin/env python3
"""Codex context-handover hook.

Two event paths share this one script (wired in ~/.codex/config.toml):

  PreCompact        -> write_handover(): refresh THIS thread's rolling handover.
  UserPromptSubmit  -> consume_latest_handover(): inject it once per compaction.

Design rules that fix the "dozens of files" failure mode:
  * ONE rolling file per (workspace, thread) -- overwritten, never accumulated.
    A single long turn that compacts N times => 1 file, always current.
  * Handovers live OUTSIDE the repo (default ~/.codex/handovers/<slug>/) so they
    never show up in `git status`. Override with CODEX_HANDOVER_DIR.
  * State is keyed per thread, so concurrent sessions / subagents in the same
    workspace never clobber each other's "latest" / "injected" tracking.
  * Ephemeral review/guardian subagents (model matches SKIP_MODEL_PATTERN) never
    write a handover -- they must not overwrite the real session's handover.
  * The original user request is captured from the HEAD of the transcript, so
    "Current Goal" is real even when the tail is all tool output.
"""
import datetime as _datetime
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


STATE_SCHEMA_VERSION = 2
DEFAULT_STATE_DIR = Path.home() / ".codex" / "hooks" / "state"
STATE_FILE_NAME = "context-handover-state.json"
DEFAULT_HANDOVER_DIR = Path.home() / ".codex" / "handovers"
HANDOVER_PREFIX = "handover-"
MAX_TRANSCRIPT_LINE_SCAN = 200000   # safety cap on rollout JSONL lines scanned per handover
MAX_SNIPPET_CHARS = 1200
MAX_HANDOVER_CHARS = 50000

# Models used by ephemeral review/guardian subagents. They share the parent
# thread id, so without this guard their PreCompact would overwrite the real
# session's handover with review chatter. Never write a handover for these.
SKIP_MODEL_PATTERN = re.compile(r"review|guardian", re.I)


def main():
    payload = read_payload()
    event_name = detect_event_name(payload)
    try:
        if event_name == "precompact":
            handover = write_handover(payload, "PreCompact")
            if handover is None:
                emit({"continue": True, "suppressOutput": True})
            else:
                emit(
                    {
                        "continue": True,
                        "suppressOutput": True,
                        "systemMessage": f"Context handover saved: {handover}",
                    }
                )
            return 0
        if event_name == "userpromptsubmit":
            additional_context = consume_latest_handover(payload)
            output = {"continue": True, "suppressOutput": True}
            if additional_context:
                output["hookSpecificOutput"] = {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": additional_context,
                }
            emit(output)
            return 0
        emit({"continue": True, "suppressOutput": True})
        return 0
    except Exception as exc:
        emit(
            {
                "continue": True,
                "suppressOutput": False,
                "systemMessage": f"context-handover hook warning: {exc}",
            }
        )
        return 0


def read_payload():
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw_stdin": raw}
    return parsed if isinstance(parsed, dict) else {"payload": parsed}


def detect_event_name(payload):
    candidates = [
        payload.get("hook_event_name"),
        payload.get("hookEventName"),
        payload.get("event_name"),
        payload.get("eventName"),
        payload.get("event"),
        os.environ.get("CODEX_HOOK_EVENT"),
    ]
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return re.sub(r"[^a-z0-9]", "", value.lower())
    return ""


def emit(output):
    print(json.dumps(output, ensure_ascii=True))


# ---------------------------------------------------------------------------
# Write path (PreCompact)
# ---------------------------------------------------------------------------

def write_handover(payload, trigger):
    model = field(payload, "model", default=os.environ.get("CODEX_MODEL", "unknown"))
    if SKIP_MODEL_PATTERN.search(model or ""):
        # Ephemeral review/guardian subagent -- not resumable user work, and it
        # must not overwrite the parent thread's handover. Skip silently.
        return None

    workspace = workspace_root(payload)
    thread_id = thread_identifier(payload)
    handover_path = handover_path_for(workspace, thread_id)
    handover_path.parent.mkdir(parents=True, exist_ok=True)

    transcript_path = transcript_file(payload)
    transcript = build_summary(transcript_path)
    git = git_summary(workspace)
    meta = metadata(payload, workspace, transcript_path, thread_id, model)
    meta["trigger"] = trigger

    content = render_handover(meta=meta, transcript=transcript, git=git)
    # Atomic overwrite so a concurrent UserPromptSubmit never reads a half file.
    tmp = handover_path.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, handover_path)

    state = load_state()
    threads = state.setdefault("threads", {})
    entry = threads.setdefault(thread_id, {})
    entry["handover_path"] = str(handover_path)
    entry["workspace"] = str(workspace)
    entry["model"] = model
    entry["written_at"] = now_iso()
    save_state(state)
    return handover_path


# ---------------------------------------------------------------------------
# Inject path (UserPromptSubmit)
# ---------------------------------------------------------------------------

def consume_latest_handover(payload):
    workspace = workspace_root(payload)
    thread_id = thread_identifier(payload)
    handover_path = handover_path_for(workspace, thread_id)
    if not handover_path.exists():
        return ""

    file_mtime = handover_path.stat().st_mtime
    state = load_state()
    threads = state.setdefault("threads", {})
    entry = threads.setdefault(thread_id, {})

    injected_mtime = entry.get("injected_mtime")
    # Inject once per compaction: only when the file is newer than what we last
    # injected for THIS thread. The file mtime is the source of truth, so an
    # overwrite (new compaction) re-arms injection while a re-submit does not.
    if isinstance(injected_mtime, (int, float)) and file_mtime <= injected_mtime:
        return ""

    text = handover_path.read_text(encoding="utf-8", errors="replace")
    if len(text) > MAX_HANDOVER_CHARS:
        text = text[:MAX_HANDOVER_CHARS] + "\n\n[Truncated by context-handover hook]\n"

    entry["injected_mtime"] = file_mtime
    entry["injected_at"] = now_iso()
    save_state(state)

    return (
        "Codex context handover injected by the UserPromptSubmit hook.\n"
        "This summarizes THIS session's state from just before the last compaction. "
        "Continue from it, then answer the user's prompt. Do not ask the user to paste it again.\n\n"
        f"Source handover file: {handover_path}\n\n"
        f"{text}"
    )


# ---------------------------------------------------------------------------
# Paths / identity
# ---------------------------------------------------------------------------

def handover_dir():
    raw = os.environ.get("CODEX_HANDOVER_DIR")
    if raw and raw.strip() and not unsafe_path_string(raw):
        return Path(raw)
    return DEFAULT_HANDOVER_DIR


def workspace_slug(workspace):
    full = str(workspace)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", full).strip("-")
    digest = hashlib.sha1(full.encode("utf-8")).hexdigest()[:8]
    return f"{slug[:60]}-{digest}" if slug else digest


def handover_path_for(workspace, thread_id):
    return handover_dir() / workspace_slug(workspace) / f"{HANDOVER_PREFIX}{thread_id}.md"


def thread_identifier(payload):
    raw = field(payload, "thread_id", "threadId", "session_id", "sessionId", default="")
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", raw)
    return cleaned or "no-thread"


def workspace_root(payload):
    raw = payload.get("cwd") or payload.get("workspace") or os.environ.get("CODEX_WORKSPACE_ROOT")
    if isinstance(raw, str) and raw.strip() and not unsafe_path_string(raw):
        path = Path(raw)
        if path.is_absolute() and path.exists() and path.is_dir():
            return path.resolve()
    return Path.cwd().resolve()


def transcript_file(payload):
    raw = (
        payload.get("transcript_path")
        or payload.get("transcriptPath")
        or payload.get("session_path")
        or payload.get("sessionPath")
        or os.environ.get("CODEX_TRANSCRIPT_PATH")
        or os.environ.get("CODEX_SESSION_FILE")
    )
    if not isinstance(raw, str) or not raw.strip() or unsafe_path_string(raw):
        return None
    path = Path(raw)
    if path.is_absolute() and path.exists() and path.is_file():
        return path.resolve()
    return None


def unsafe_path_string(raw):
    if raw.startswith("~") or raw.startswith("\\\\"):
        return True
    if any(mark in raw for mark in ("$", "%", "*", "?", "[", "]")):
        return True
    return ".." in Path(raw).parts


# ---------------------------------------------------------------------------
# Transcript reading / summarization
# ---------------------------------------------------------------------------

def build_summary(transcript_path):
    """Single full-file pass over the rollout JSONL.

    Scanning the whole file (not a 256 KB tail) is what makes the goal correct:
    the original `user_message` lives at the top of the file, while the latest
    instruction lives near the end, and a huge agentic turn can push either out of
    any fixed window. Rollout files are line-delimited JSON, so this stays cheap.
    """
    empty = {
        "messages": [], "commands": [], "token_usage": [],
        "latest_total_tokens": None, "latest_current_tokens": None,
        "latest_context_window": None, "first_user": "", "last_user": "",
        "changed_files": [], "failures": [], "read_files": [], "turn_aborted": False,
        "plan": None,
    }
    if not transcript_path:
        return empty

    messages = []
    commands = []
    token_usage = []
    latest_total_tokens = latest_current_tokens = latest_context_window = None
    first_user = ""
    last_user = ""
    outputs = {}            # call_id -> {exit_code, snippet}
    changed_files = {}      # path -> last change type (insertion order preserved)
    patch_failures = []     # paths whose apply_patch failed
    read_files = []         # files the agent read (recent, de-duped)
    turn_aborted = False
    latest_plan = None      # most recent update_plan step list

    try:
        line_count = 0
        with transcript_path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line_count += 1
                if line_count > MAX_TRANSCRIPT_LINE_SCAN:
                    break
                item = parse_transcript_line(line)
                if not item:
                    continue
                message = message_from_item(item)
                if message:
                    messages.append(message)
                    if message["role"] == "user":
                        if not first_user:
                            first_user = message["text"]
                        last_user = message["text"]
                command = command_from_item(item)
                if command:
                    commands.append(command)
                    if is_read_command(command["command"]):
                        for path in paths_from_text(command["command"]):
                            if path not in read_files:
                                read_files.append(path)
                out = output_from_item(item)
                if out and out.get("call_id"):
                    outputs[out["call_id"]] = out
                changed = changed_files_from_item(item)
                if changed:
                    for change in changed:
                        if change["success"]:
                            changed_files[change["path"]] = change["type"]
                        else:
                            patch_failures.append(change["path"])
                if item.get("type") == "turn_aborted":
                    turn_aborted = True
                plan = plan_from_item(item)
                if plan:
                    latest_plan = plan
                usage = token_usage_from_item(item)
                if usage:
                    token_usage.append(usage["summary"])
                    if usage["total_tokens"] is not None:
                        latest_total_tokens = usage["total_tokens"]
                    if usage["current_tokens"] is not None:
                        latest_current_tokens = usage["current_tokens"]
                    if usage["context_window"] is not None:
                        latest_context_window = usage["context_window"]
    except OSError:
        return empty

    # Fallback: if no clean user_message was found (e.g. heavily compacted thread),
    # recover the original request from a compaction event's replacement_history.
    if not first_user:
        first_user = first_user_from_compaction(transcript_path) or ""
        if not last_user:
            last_user = first_user

    # Attach each command's outcome (exit code + snippet) via call_id.
    for command in commands:
        result = outputs.get(command.get("call_id"))
        command["exit_code"] = result.get("exit_code") if result else None
        command["out_snippet"] = result.get("snippet") if result else ""

    # Failures: recent commands that exited non-zero, EXCLUDING probe commands
    # (test/ls/grep/find/...) whose non-zero exit is normal control flow.
    failures = [
        {"command": c["command"], "exit_code": c["exit_code"], "snippet": c.get("out_snippet", "")}
        for c in commands
        if isinstance(c.get("exit_code"), int) and c["exit_code"] != 0 and not is_probe_command(c["command"])
    ][-6:]
    for path in patch_failures[-4:]:
        failures.append({"command": f"apply_patch -> {path}", "exit_code": "patch-failed", "snippet": ""})

    changed = [{"path": path, "type": ctype} for path, ctype in changed_files.items()]

    return {
        "messages": messages[-10:],
        "commands": commands[-12:],
        "token_usage": token_usage[-5:],
        "latest_total_tokens": latest_total_tokens,
        "latest_current_tokens": latest_current_tokens,
        "latest_context_window": latest_context_window,
        "first_user": first_user,
        "last_user": last_user,
        "changed_files": changed[-30:],
        "failures": failures,
        "read_files": read_files[-12:],
        "turn_aborted": turn_aborted,
        "plan": latest_plan,
    }


def first_user_from_compaction(transcript_path):
    """Recover the original user request from a `compacted` event's replacement_history."""
    try:
        with transcript_path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"compacted"' not in line and '"replacement_history"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = obj.get("payload", obj)
                history = payload.get("replacement_history") if isinstance(payload, dict) else None
                for entry in history or []:
                    if not isinstance(entry, dict) or entry.get("role") != "user":
                        continue
                    text = text_from_content(entry.get("content"))
                    if text and not is_marker_text(text):
                        return trim(text)
    except OSError:
        return ""
    return ""


def is_injected_handover_text(text):
    return "context handover injected" in text.lower() or "# Codex Context Handover" in text


def parse_transcript_line(line):
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    payload = obj.get("payload")
    if isinstance(payload, dict):
        if isinstance(payload.get("item"), dict):
            return payload["item"]
        return payload
    if isinstance(obj.get("item"), dict):
        return obj["item"]
    return obj


def message_from_item(item):
    # Codex stores genuine UI turns as `user_message` / `agent_message` items
    # (clean prompt text in a `message` field). Plain `message` items also carry
    # role=user, but those include system-injected markers (<turn_aborted>,
    # AGENTS.md instructions, <user_instructions>, ...) that are NOT the real goal.
    itype = item.get("type")
    if itype == "user_message":
        text = item.get("message") or text_from_content(item.get("content"))
        if not text or is_marker_text(text):
            return None
        return {"role": "user", "text": trim(text)}
    if itype == "agent_message":
        text = item.get("message") or text_from_content(item.get("content"))
        if not text:
            return None
        return {"role": "assistant", "text": trim(text)}
    if itype == "message":
        role = item.get("role")
        if role not in {"user", "assistant"}:
            return None
        text = text_from_content(item.get("content"))
        if not text:
            return None
        if role == "user" and is_marker_text(text):
            return None
        return {"role": role, "text": trim(text)}
    return None


def is_marker_text(text):
    """True for system/environment-injected user-role content that is not a real prompt."""
    stripped = text.lstrip()
    if stripped.startswith("<"):  # <turn_aborted>, <user_instructions>, <environment_context>, <permissions ...>
        return True
    low = stripped.lower()
    if low.startswith("# agents.md") or "<instructions>" in low or "# system instructions" in low:
        return True
    return is_injected_handover_text(text)


def text_from_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return first_text_field(content)
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict):
            text = first_text_field(part)
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def first_text_field(obj):
    for key in ("text", "input_text", "output_text", "content"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


TOOLS_CALL_RE = re.compile(r"tools\.(\w+)\s*\(")


def decode_js_str(s):
    """Unescape the common escapes in a JS double-quoted string literal."""
    return (s.replace("\\\\", "\\").replace('\\"', '"').replace("\\n", "\n")
             .replace("\\t", "\t").replace("\\r", "").replace("\\'", "'").replace("\\/", "/"))


def js_field(js, key):
    """Pull a double-quoted scalar field (key: "...") out of a JS object-literal string."""
    m = re.search(r"\b" + re.escape(key) + r'\s*:\s*"((?:[^"\\]|\\.)*)"', js)
    return decode_js_str(m.group(1)) if m else None


def js_template_field(js, key):
    """Pull a backtick template-literal field (key: `...`) — gpt-5.6 uses these for interpolated cmds."""
    m = re.search(r"\b" + re.escape(key) + r"\s*:\s*`([^`]*)`", js)
    return m.group(1).strip() if m else None


def command_from_js_input(js):
    """gpt-5.6 wraps every tool in a JS exec harness, e.g.
       const r = await tools.exec_command({cmd: "git status", workdir: "..."}); text(r.output);
       Return (inner_tool_label, command_text). apply_patch/update_plan return ""
       because they are surfaced in their own sections."""
    if not isinstance(js, str):
        return None, ""
    m = TOOLS_CALL_RE.search(js)
    inner = m.group(1) if m else None
    if inner in ("update_plan", "apply_patch"):
        return inner, ""
    for key in ("cmd", "command"):
        val = js_field(js, key) or js_template_field(js, key)
        if val:
            return (inner or "exec"), val
    for key in ("query", "sql", "pattern", "path", "title"):
        val = js_field(js, key)
        if val:
            return inner, f"{key}: {val}"
    # Arbitrary JS (loops, Promise.all, multiple inner calls) — a compact one-line snippet
    # is far more useful than the bare tool name.
    snippet = re.sub(r"\s+", " ", js).strip()
    return (inner or "exec"), trim(snippet, 160)


def mcp_command_from_item(item):
    """An MCP tool call (mcp_tool_call_end): server:tool + a short argument snippet."""
    inv = item.get("invocation")
    if not isinstance(inv, dict):
        return None
    label = ":".join(x for x in (inv.get("server"), inv.get("tool")) if x) or "mcp"
    args = inv.get("arguments")
    snippet = ""
    if isinstance(args, dict):
        for key in ("cmd", "command", "query", "sql", "code", "title", "pattern"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                snippet = val
                break
        else:
            snippet = json.dumps(args, sort_keys=True)
    # `snippet` only — the render already prefixes the tool label, so don't repeat it.
    return {"tool": label, "command": trim(snippet or label, 400),
            "call_id": item.get("call_id")}


def command_from_item(item):
    itype = item.get("type")
    if itype == "mcp_tool_call_end":
        return mcp_command_from_item(item)
    if itype not in {"function_call", "tool_call", "custom_tool_call"}:
        return None
    name = item.get("name") or item.get("tool_name") or item.get("recipient_name")
    arguments = item.get("arguments") or item.get("input") or item.get("parameters")
    # gpt-5.6 custom_tool_call (name="exec", JS input) — or any input that wraps tools.X(...)
    if itype == "custom_tool_call" or (isinstance(arguments, str) and "tools." in arguments):
        inner, command = command_from_js_input(arguments)
        if not command:
            return None  # apply_patch/update_plan are shown elsewhere
        return {"tool": str(inner or name or "exec"), "command": trim(command, 400),
                "call_id": item.get("call_id")}
    command = command_from_arguments(arguments)
    if not command and isinstance(name, str):
        command = name
    if not command:
        return None
    return {"tool": str(name or "tool"), "command": trim(command, 400), "call_id": item.get("call_id")}


def output_from_item(item):
    """Extract a tool/command result: exit code + a short output snippet, keyed by call_id."""
    if item.get("type") not in {"function_call_output", "custom_tool_call_output", "tool_call_output"}:
        return None
    out = item.get("output")
    if isinstance(out, str):
        text = out
    elif isinstance(out, list):          # gpt-5.6 custom_tool_call_output: list of {type, text}
        text = text_from_content(out)
    elif isinstance(out, dict):
        text = first_text_field(out)
    else:
        text = ""
    return {"call_id": item.get("call_id"), "exit_code": parse_exit_code(text), "snippet": output_snippet(text)}


def parse_exit_code(text):
    t = text or ""
    match = re.search(
        r"(?:exited with code|Exit code:|exit code|exited with status|Process exited(?: with(?: code)?)?)\s*(-?\d+)",
        t, re.I)
    if match:
        return int(match.group(1))
    if re.search(r"\bScript failed\b", t):   # gpt-5.6 unified-exec wrapper failure marker
        return 1
    return None


def output_snippet(text):
    if not text:
        return ""
    body = text
    marker = "\nOutput:\n"
    if marker in body:  # strip the unified-exec preamble (Chunk ID / Wall time / Process exited / Output:)
        body = body.split(marker, 1)[1]
    return trim(body, 220)


def changed_files_from_item(item):
    """Authoritative list of files an apply_patch turn wrote (add/update/delete)."""
    if item.get("type") != "patch_apply_end":
        return None
    changes = item.get("changes")
    if not isinstance(changes, dict):
        return None
    success = bool(item.get("success", True))
    files = []
    for path, info in changes.items():
        ctype = info.get("type") if isinstance(info, dict) else "change"
        files.append({"path": str(path), "type": str(ctype or "change"), "success": success})
    return files or None


READ_CMD_RE = re.compile(r"^\s*(?:[A-Za-z0-9_./-]*/)?(cat|sed|less|head|tail|bat|nl|wc|view)\b")


def is_read_command(cmd):
    return bool(READ_CMD_RE.match(cmd or ""))


# Commands that routinely exit non-zero as normal control flow (existence checks,
# searches with no match). A non-zero exit here is not a blocker, so it is filtered
# out of the failures list to avoid the false positives the old heuristic produced.
PROBE_CMD_RE = re.compile(
    r"^\s*(?:if\s+)?(?:[A-Za-z0-9_./-]*/)?"
    r"(test|\[|\[\[|ls|grep|egrep|fgrep|rg|ag|find|stat|which|type|diff|cmp|pgrep|pidof|"
    r"git\s+diff|git\s+grep)\b"
)


def is_probe_command(cmd):
    return bool(PROBE_CMD_RE.match(cmd or ""))


def plan_from_item(item):
    """Extract the latest update_plan step list (status + step text)."""
    if item.get("type") not in {"function_call", "custom_tool_call", "tool_call"}:
        return None
    name = item.get("name") or item.get("tool_name") or item.get("recipient_name")
    args = item.get("arguments") or item.get("input") or item.get("parameters")
    # gpt-5.6: name="exec", the plan is inside a JS input string: tools.update_plan({plan: [...]})
    if name != "update_plan" and isinstance(args, str) and "update_plan" in args:
        return plan_from_js_input(args)
    if name != "update_plan":
        return None
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return None
    if not isinstance(args, dict) or not isinstance(args.get("plan"), list):
        return None
    steps = []
    for step in args["plan"]:
        if isinstance(step, dict):
            steps.append({"status": str(step.get("status", "")), "step": trim(str(step.get("step", "")), 200)})
    return steps or None


def plan_from_js_input(js):
    """Best-effort: pull step/status pairs out of a JS update_plan({plan:[{step,status}]}) call."""
    steps = []
    for block in re.findall(r"\{[^{}]*\}", js):
        step = re.search(r"step\s*:\s*\"((?:[^\"\\]|\\.)*)\"", block)
        if not step:
            continue
        status = re.search(r"status\s*:\s*\"(\w+)\"", block)
        steps.append({"status": status.group(1) if status else "",
                      "step": trim(decode_js_str(step.group(1)), 200)})
    return steps or None


def command_from_arguments(arguments):
    if isinstance(arguments, dict):
        for key in ("cmd", "command", "query", "pattern"):
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return json.dumps(arguments, sort_keys=True)
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return arguments
        return command_from_arguments(parsed)
    return ""


def token_usage_from_item(item):
    total = nested_get(item, ["tokenUsage", "total", "totalTokens"])
    window = item.get("modelContextWindow")
    current = nested_get(item, ["tokenUsage", "last", "totalTokens"])
    if total is None:
        total = nested_get(item, ["info", "total_token_usage", "total_tokens"])
        window = nested_get(item, ["info", "model_context_window"])
        current = nested_get(item, ["info", "last_token_usage", "total_tokens"])
    total_int = int_or_none(total)
    current_int = int_or_none(current)
    window_int = int_or_none(window)
    if total_int is None and current_int is None:
        return None
    if total_int is None:
        total_int = current_int
    return {
        "summary": (
            f"totalTokens={total_int}, "
            f"currentTokens={current_int if current_int is not None else 'unknown'}, "
            f"modelContextWindow={window_int if window_int is not None else 'unknown'}"
        ),
        "total_tokens": total_int,
        "current_tokens": current_int,
        "context_window": window_int,
    }


def int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def nested_get(obj, keys):
    current = obj
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


# ---------------------------------------------------------------------------
# Git / metadata / heuristics
# ---------------------------------------------------------------------------

def git_summary(workspace):
    if run_git(workspace, "rev-parse", "--is-inside-work-tree").strip() != "true":
        return {"inside": False, "summary": "Not a git worktree."}
    branch = run_git(workspace, "branch", "--show-current").strip()
    head = run_git(workspace, "rev-parse", "--short", "HEAD").strip()
    status = [
        line
        for line in run_git(workspace, "status", "--short").splitlines()
        if HANDOVER_PREFIX not in line  # don't echo handover files back at ourselves
    ]
    return {
        "inside": True,
        "branch": branch or "(detached)",
        "head": head or "unknown",
        "status": status[:80],
    }


def run_git(workspace, *args):
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace), *args],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def metadata(payload, workspace, transcript_path, thread_id, model):
    return {
        "thread_id": thread_id,
        "turn_id": field(payload, "turn_id", "turnId"),
        "model": model,
        "cwd": str(workspace),
        "transcript_path": str(transcript_path) if transcript_path else "unknown",
        "created_at": now_iso(),
    }


def field(payload, *keys, default="unknown"):
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return default


def paths_from_text(text):
    found = []
    for match in re.findall(r"/(?:Users|private|tmp|var|opt)/[^'\"\n\r]+", text):
        for sep in (" && ", " || ", " | ", " ; ", " > ", " >> ", " &&", "; "):
            index = match.find(sep)
            if index != -1:
                match = match[:index]
        match = match.rstrip(".,;:) \t")
        if match:
            found.append(match)
    return found


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_handover(meta, transcript, git):
    goal = transcript.get("first_user") or latest_role(transcript["messages"], "user")
    latest_user = transcript.get("last_user") or ""
    progress = recent_role(transcript["messages"], "assistant", 3)
    changed = transcript.get("changed_files") or []
    failures = transcript.get("failures") or []
    read_files = transcript.get("read_files") or []

    lines = [
        "# Codex Context Handover",
        f"_Rolling summary, overwritten by the {meta['trigger']} hook each compaction. "
        "Continue from it; do not ask the user to repeat it._",
        "",
        "## Goal (original request)",
        goal or "Continue the active user-requested Codex task.",
    ]

    # Only show the latest instruction when it differs from the original goal.
    if latest_user and latest_user.strip() and latest_user.strip() != (goal or "").strip():
        lines += ["", "## Latest instruction", latest_user]

    lines += ["", "## Where things stand (recent assistant turns)"]
    lines += bullet_lines(progress) if progress else ["- No assistant narrative found in transcript."]

    plan = transcript.get("plan")
    if plan:
        lines += ["", "## Current plan (latest update_plan)"]
        lines += plan_lines(plan)

    lines += ["", "## Files changed this session"]
    if changed:
        lines += [f"- [{change['type']}] {short_path(change['path'], meta['cwd'])}" for change in changed]
    else:
        lines += ["- None recorded (no apply_patch writes in this thread)."]

    lines += ["", "## Open failures / blockers"]
    if transcript.get("turn_aborted"):
        lines += ["- ⚠ The latest turn was interrupted/aborted by the user — confirm intent before continuing."]
    if failures:
        lines += [failure_line(failure) for failure in failures]
    elif not transcript.get("turn_aborted"):
        lines += ["- None detected (no non-zero exits or failed patches in recent activity)."]

    lines += ["", "## Recent commands (oldest → newest)"]
    lines += command_lines(transcript["commands"])

    if read_files:
        lines += ["", "## Key files read"]
        lines += [f"- {short_path(path, meta['cwd'])}" for path in read_files]

    lines += ["", "## Context & git"]
    lines += [f"- {context_summary(transcript)}"]
    if git["inside"]:
        status = git["status"]
        lines += [f"- Branch {git['branch']} @ {git['head']} — "
                  + (f"{len(status)} uncommitted path(s)" if status else "clean tree")]
        lines += [f"  - {entry}" for entry in status[:15]]
    else:
        lines += [f"- {git['summary']}"]

    lines += [
        "",
        "## Session metadata",
        f"- thread {meta['thread_id']} · turn {meta['turn_id']} · model {meta['model']} · {meta['created_at']}",
        f"- cwd {meta['cwd']}",
        f"- transcript {meta['transcript_path']}",
        "",
        "## Resume guidance",
        "1. Re-read any file under \"Files changed this session\" before editing it further.",
        "2. Resolve anything under \"Open failures / blockers\" first.",
        "3. Then continue toward the Goal, honoring the Latest instruction.",
        "",
    ]
    return "\n".join(lines)


def context_summary(transcript):
    current = transcript.get("latest_current_tokens")
    window = transcript.get("latest_context_window")
    if isinstance(current, int) and isinstance(window, int) and window > 0:
        return f"~{current:,} tokens in context (~{round(100 * current / window)}% of {window:,}) at compaction"
    if isinstance(current, int):
        return f"~{current:,} tokens in context at compaction"
    return "Context size at compaction: unknown (no token_count events in transcript)."


def short_path(path, cwd):
    cwd = str(cwd or "")
    if cwd and path.startswith(cwd + "/"):
        return path[len(cwd) + 1:]
    return path


def failure_line(failure):
    code = failure.get("exit_code")
    snippet = failure.get("snippet") or ""
    head = f"- ✗ exit {code}: {failure['command']}"
    return f"{head} → {snippet}" if snippet else head


def latest_role(messages, role):
    for message in reversed(messages):
        if message["role"] == role:
            return message["text"]
    return ""


def recent_role(messages, role, count):
    texts = []
    for message in messages:
        if message["role"] != role:
            continue
        if texts and texts[-1] == message["text"]:  # drop consecutive duplicates
            continue
        texts.append(message["text"])
    return texts[-count:]


PLAN_STATUS_ICON = {"completed": "✓", "in_progress": "▶", "pending": "☐"}


def plan_lines(plan):
    out = []
    for step in plan:
        icon = PLAN_STATUS_ICON.get(step.get("status", ""), "·")
        out.append(f"- {icon} {step.get('step', '')}")
    return out


def bullet_lines(values):
    return [f"- {value}" for value in values]


def command_lines(commands):
    if not commands:
        return ["- None found in transcript."]
    out = []
    for entry in commands:
        code = entry.get("exit_code")
        mark = "✓" if code == 0 else ("✗" if isinstance(code, int) else "·")
        suffix = f" (exit {code})" if isinstance(code, int) and code != 0 else ""
        out.append(f"- {mark} {entry['tool']}: {entry['command']}{suffix}")
    return out


def trim(text, limit=MAX_SNIPPET_CHARS):
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 15].rstrip() + " [truncated]"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def now_iso():
    return _datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def load_state():
    path = state_path()
    default = {"schema_version": STATE_SCHEMA_VERSION, "threads": {}}
    if not path.exists():
        return default
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    if not isinstance(state, dict):
        return default
    state["schema_version"] = STATE_SCHEMA_VERSION
    if not isinstance(state.get("threads"), dict):
        state["threads"] = {}
    return state


def save_state(state):
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def state_path():
    raw = os.environ.get("CODEX_HANDOVER_STATE_DIR")
    base = Path(raw) if raw else DEFAULT_STATE_DIR
    return base / STATE_FILE_NAME


if __name__ == "__main__":
    raise SystemExit(main())
