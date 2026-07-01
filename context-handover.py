#!/usr/bin/env python3
"""Codex context-handover hook.

Five event paths share this one script (wired in ~/.codex/config.toml):

  PreCompact        -> write_handover(): refresh THIS thread's rolling handover.
  PostCompact       -> write_handover(): rewrite right after compaction succeeds
                       (covers compaction paths where PreCompact may not fire).
  PreToolUse        -> inject_handover(): the mid-turn injector. After an
                       auto-compaction inside a long agentic turn, the model's
                       next tool call re-arms it with the handover. This is the
                       ONLY boundary that exists for single-turn automation runs.
  SessionStart      -> inject_handover() when source == "compact": Codex queues a
                       SessionStart(compact) hook after EVERY compaction; it fires
                       at the next turn start (same turn for pre-turn compactions).
  UserPromptSubmit  -> inject_handover(): interactive safety net; regenerates the
                       summary from the live transcript so it is never stale.

Design rules that fix the historical failure modes:
  * ONE rolling file per (workspace, thread) -- overwritten, never accumulated.
  * Handovers live OUTSIDE the repo (default ~/.codex/handovers/<slug>/).
    Override with CODEX_HANDOVER_DIR.
  * State is ONE FILE PER THREAD (state/threads/<id>.json), so concurrent Codex
    windows never read-modify-write each other's injection tracking.
  * Injection happens exactly once per compaction: the first of PreToolUse /
    SessionStart / UserPromptSubmit to observe a handover newer than
    injected_mtime wins; the rest see it as consumed.
  * Ephemeral review/guardian subagents (model matches SKIP_MODEL_PATTERN) and
    subagent turns (payload carries agent_id) neither write nor consume.
  * The transcript scan is head+tail biased: the head yields the original goal,
    the tail yields current state -- a 100MB rollout no longer truncates away
    exactly the recent activity the handover exists to preserve.
"""
import datetime as _datetime
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


STATE_SCHEMA_VERSION = 3
DEFAULT_STATE_DIR = Path.home() / ".codex" / "hooks" / "state"
LEGACY_STATE_FILE_NAME = "context-handover-state.json"
DEFAULT_HANDOVER_DIR = Path.home() / ".codex" / "handovers"
HANDOVER_PREFIX = "handover-"
# Keep both retentions EQUAL: an armed-looking handover must never outlive its
# injection-dedup marker, or a thread resumed weeks later gets a spurious inject.
STATE_RETENTION_DAYS = 30
HANDOVER_RETENTION_DAYS = 30
MAX_SNIPPET_CHARS = 1200
MAX_HANDOVER_CHARS = 50000


def _env_int(name, default):
    """A malformed override env var must degrade to the default, not crash the
    hook at import (which would fail every wired event, incl. every tool call)."""
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


# Transcript scan bounds. The head scan hunts for the original goal plus rare
# session-scoped facts (apply_patch changes, compaction history); the tail scan
# (a byte-seek window from EOF) collects everything else, so recent state
# survives no matter how large the rollout grows.
HEAD_SCAN_LINES = _env_int("CODEX_HANDOVER_HEAD_LINES", 30000)
TAIL_SCAN_BYTES = _env_int("CODEX_HANDOVER_TAIL_BYTES", 16 * 1024 * 1024)

# Models used by ephemeral review/guardian subagents. They share the parent
# thread id, so without this guard their PreCompact would overwrite the real
# session's handover (and their prompts would consume its injection).
SKIP_MODEL_PATTERN = re.compile(r"review|guardian", re.I)

# Fernet tokens from inter-agent messaging (send_message/followup_task) are
# ciphertext -- pure noise in a handover. Redacted centrally in trim().
# Boundary lookarounds + a 40-char minimum keep interior substrings of
# unrelated base64 payloads out of scope.
FERNET_RE = re.compile(r"(?<![A-Za-z0-9+/=])gAAAA[A-Za-z0-9_\-]{40,}={0,2}")

# Runtime/plumbing paths that mislead a resuming model when listed as session
# work-in-progress. Deliberately narrow: locks, heartbeat logs, files inside
# log directories, browser/session scratch. A plain *.log elsewhere may be a
# legitimate deliverable (test fixture, parser input) and must stay visible.
PATH_NOISE_RE = re.compile(
    r"(\.lock$|heartbeat\.log$|(^|/)logs?/[^/]+\.log$|\.playwright-mcp(/|$)|\.tmp$)"
)

# Scheduled/automation runs open with framework boilerplate (lock protocol,
# heartbeat rules) as the first user message; only its first line is the goal.
# Both the prefix AND a structural marker must match, so a legitimate prompt
# that merely starts with "Reminder ..." or "Cron ..." is never summarized.
AUTOMATION_GOAL_RE = re.compile(r"^\s*(automation|scheduled task|cron|reminder)\b", re.I)
AUTOMATION_MARKERS = ("automation id:", "automation memory:", "complete.flag", ".lock", "heartbeat")


def main():
    try:
        payload = read_payload()
        event_name = detect_event_name(payload)
        if event_name in ("precompact", "postcompact"):
            trigger = "PreCompact" if event_name == "precompact" else "PostCompact"
            handover = write_handover(payload, trigger)
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
            return emit_injection(payload, "UserPromptSubmit")
        if event_name == "pretooluse":
            return emit_injection(payload, "PreToolUse")
        if event_name == "sessionstart":
            source = payload.get("source")
            if source == "compact":
                return emit_injection(payload, "SessionStart")
            emit({"continue": True, "suppressOutput": True})
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


def emit_injection(payload, event):
    additional_context = inject_handover(payload, event)
    output = {"continue": True, "suppressOutput": True}
    if additional_context:
        output["hookSpecificOutput"] = {
            "hookEventName": event,
            "additionalContext": additional_context,
        }
    emit(output)
    return 0


def read_payload():
    raw = sys.stdin.buffer.read().decode("utf-8", "replace")
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


def is_subagent_payload(payload):
    return bool(payload.get("agent_id") or payload.get("agentId"))


# ---------------------------------------------------------------------------
# Write path (PreCompact / PostCompact)
# ---------------------------------------------------------------------------

def write_handover(payload, trigger):
    model = field(payload, "model", default=os.environ.get("CODEX_MODEL", "unknown"))
    if SKIP_MODEL_PATTERN.search(model or "") or is_subagent_payload(payload):
        # Ephemeral review/guardian subagent or subagent turn -- not resumable
        # user work, and it must not overwrite the parent thread's handover.
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
    # Atomic overwrite so a concurrent injector never reads a half file.
    tmp = handover_path.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, handover_path)

    entry = load_thread_state(thread_id)
    entry["handover_path"] = str(handover_path)
    entry["workspace"] = str(workspace)
    entry["model"] = model
    entry["written_at"] = now_iso()
    if transcript_path:
        entry["transcript_path"] = str(transcript_path)
    save_thread_state(thread_id, entry)
    prune_stale_files()
    return handover_path


# ---------------------------------------------------------------------------
# Inject path (PreToolUse / SessionStart(compact) / UserPromptSubmit)
# ---------------------------------------------------------------------------

def inject_handover(payload, event):
    model = field(payload, "model", default=os.environ.get("CODEX_MODEL", ""))
    if SKIP_MODEL_PATTERN.search(model or "") or is_subagent_payload(payload):
        # Never let a review/guardian or subagent turn consume the injection
        # meant for the real session (they share the parent thread id).
        return ""

    workspace = workspace_root(payload)
    thread_id = thread_identifier(payload)
    handover_path = handover_path_for(workspace, thread_id)
    entry = load_thread_state(thread_id)

    if not handover_path.exists():
        # SessionStart(compact) means a compaction definitely just happened.
        # If no handover file exists (compaction path that skipped the write
        # hooks), regenerate straight from the transcript -- once per event.
        if event == "SessionStart":
            text = regenerate_text(payload, entry, thread_id, workspace, model, event)
            if text:
                entry["injected_at"] = now_iso()
                entry["injected_event"] = event
                try:
                    save_thread_state(thread_id, entry)
                except OSError:
                    return ""
                return injection_preamble(event, "regenerated from transcript") + text
        return ""

    file_mtime = handover_path.stat().st_mtime
    injected_mtime = entry.get("injected_mtime")
    # Inject once per compaction: only when the file is newer than what was
    # last injected for THIS thread. A new compaction overwrites the file and
    # re-arms injection; anything else finds it consumed.
    if isinstance(injected_mtime, (int, float)) and file_mtime <= injected_mtime:
        # Exception: SessionStart(compact) is authoritative proof a NEW
        # compaction just happened. If the write hooks failed to refresh the
        # file for it, it still looks consumed. Unless an injection landed
        # recently (the normal mid-turn PreToolUse -> next-turn SessionStart
        # sequence), regenerate and inject rather than silently lose it.
        if event != "SessionStart" or _recent_iso(entry.get("injected_at"), minutes=30):
            return ""

    text = ""
    source_desc = str(handover_path)
    if event in ("UserPromptSubmit", "SessionStart"):
        # These can fire long after the compaction that wrote the file; the
        # thread may have kept working. Rebuild from the live transcript so
        # the injected state is current, falling back to the file.
        text = regenerate_text(payload, entry, thread_id, workspace, model, event)
        if text:
            source_desc = "regenerated from transcript"
    if not text:
        text = handover_path.read_text(encoding="utf-8", errors="replace")
    if len(text) > MAX_HANDOVER_CHARS:
        text = text[:MAX_HANDOVER_CHARS] + "\n\n[Truncated by context-handover hook]\n"

    entry["injected_mtime"] = file_mtime
    entry["injected_at"] = now_iso()
    entry["injected_event"] = event
    try:
        save_thread_state(thread_id, entry)
    except OSError:
        # Fail closed and quiet: injecting without recording consumption would
        # re-inject on every subsequent tool call (the historical runaway mode),
        # and warning would spam the hot PreToolUse path.
        return ""

    return injection_preamble(event, source_desc) + text


def _recent_iso(ts, minutes):
    if not isinstance(ts, str) or not ts:
        return False
    try:
        then = _datetime.datetime.fromisoformat(ts)
    except ValueError:
        return False
    now = _datetime.datetime.now(then.tzinfo) if then.tzinfo else _datetime.datetime.now()
    return (now - then).total_seconds() < minutes * 60


def injection_preamble(event, source_desc):
    if event == "UserPromptSubmit":
        tail = "Continue from it, then answer the user's prompt. Do not ask the user to paste it again."
    else:
        tail = "Continue the in-flight work from it. Do not ask the user to repeat anything."
    return (
        f"Codex context handover injected by the {event} hook.\n"
        "This session's context was just compacted; the summary below reconstructs "
        f"the session state from its rollout transcript. {tail}\n\n"
        f"Source: {source_desc}\n\n"
    )


def regenerate_text(payload, entry, thread_id, workspace, model, event):
    transcript_path = transcript_file(payload)
    if transcript_path is None:
        stored = entry.get("transcript_path")
        if isinstance(stored, str) and stored:
            candidate = Path(stored)
            if candidate.is_file():
                transcript_path = candidate
    if transcript_path is None:
        return ""
    try:
        transcript = build_summary(transcript_path)
        git = git_summary(workspace)
        meta = metadata(payload, workspace, transcript_path, thread_id, model)
        meta["trigger"] = event
        return render_handover(meta=meta, transcript=transcript, git=git)
    except Exception:
        return ""


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
    # session_id first: it is a REQUIRED payload field on every hook event, so
    # all five events resolve to the same identity. thread_id is nullable and,
    # if it appeared on only some events, would split one thread's handover and
    # dedup state across two keys (double injection).
    raw = field(payload, "session_id", "sessionId", "thread_id", "threadId", default="")
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

class _Collector:
    """Accumulates transcript items; shared by the head and tail scan phases."""

    def __init__(self):
        self.messages = []
        self.commands = []
        self.token_usage = []
        self.latest_total_tokens = None
        self.latest_current_tokens = None
        self.latest_context_window = None
        self.first_user = ""
        self.last_user = ""
        self.compaction_goal = ""   # first user text recovered from replacement_history
        self.outputs = {}           # call_id -> {exit_code, snippet}
        self.changed_files = {}     # path -> last change type (insertion order preserved)
        self.patch_failures = []
        self.read_files = []
        self.turn_aborted = False
        self.latest_plan = None

    def process(self, item, head_phase=False):
        message = message_from_item(item)
        if message and message["role"] == "user" and not self.first_user:
            self.first_user = message["text"]
        if not self.compaction_goal:
            goal = goal_from_compacted_item(item)
            if goal:
                self.compaction_goal = goal
        changed = changed_files_from_item(item)
        if changed:
            for change in changed:
                if change["success"]:
                    self.changed_files[change["path"]] = change["type"]
                elif not head_phase:
                    # Old patch failures from the head window are stale, not
                    # open blockers; only the tail contributes failures.
                    self.patch_failures.append(change["path"])
        if head_phase:
            # The head window contributes only session-scoped facts: the goal,
            # the compacted-history fallback, and the apply_patch inventory.
            # Recency-biased sections come exclusively from the tail window.
            return
        if message:
            self.messages.append(message)
            if message["role"] == "user":
                self.last_user = message["text"]
        command = command_from_item(item)
        if command:
            command["call_ids"] = [command.get("call_id")]
            previous = self.commands[-1] if self.commands else None
            if (
                previous
                and previous["tool"] == command["tool"]
                and previous["command"] == command["command"]
            ):
                # Collapse polling stretches (wait_agent, status checks) so a
                # dozen identical calls don't evict every real command. All
                # call_ids are kept so a failing run inside the collapsed
                # stretch still surfaces its exit code.
                previous["repeat"] = previous.get("repeat", 1) + 1
                previous["call_ids"].append(command.get("call_id"))
            else:
                self.commands.append(command)
            if is_read_command(command["command"]):
                for path in paths_from_text(command["command"]):
                    if path not in self.read_files:
                        self.read_files.append(path)
        out = output_from_item(item)
        if out and out.get("call_id"):
            self.outputs[out["call_id"]] = out
        if item.get("type") == "turn_aborted":
            self.turn_aborted = True
        plan = plan_from_item(item)
        if plan:
            self.latest_plan = plan
        usage = token_usage_from_item(item)
        if usage:
            self.token_usage.append(usage["summary"])
            if usage["total_tokens"] is not None:
                self.latest_total_tokens = usage["total_tokens"]
            if usage["current_tokens"] is not None:
                self.latest_current_tokens = usage["current_tokens"]
            if usage["context_window"] is not None:
                self.latest_context_window = usage["context_window"]


def build_summary(transcript_path):
    """Head+tail scan of the rollout JSONL.

    The original user goal lives at the HEAD of the file; everything else the
    handover needs (latest instruction, plan, commands, failures, token counts)
    lives at the TAIL. Scanning head-first with a line cap used to throw away
    the tail on huge rollouts -- the one part that must never go stale. Now the
    head scan hunts only for the goal, and a byte-seek tail window collects the
    rest, so cost stays bounded no matter how large the file grows.
    """
    collector = _Collector()
    empty = _summary_dict(collector)
    empty["windowed"] = False
    if not transcript_path:
        return empty

    windowed = False
    try:
        size = transcript_path.stat().st_size
        windowed = size > TAIL_SCAN_BYTES
        # Binary mode: exact byte offsets for the tail seek, decode per line.
        with transcript_path.open("rb") as fh:
            if not windowed:
                for raw in fh:
                    item = parse_transcript_line(raw.decode("utf-8", "replace"))
                    if item:
                        collector.process(item)
            else:
                # Phase 1 (head): session-scoped facts only (goal, compaction
                # fallback, apply_patch inventory), bounded by lines AND bytes.
                head_bytes = 0
                for line_count, raw in enumerate(fh, 1):
                    head_bytes += len(raw)
                    item = parse_transcript_line(raw.decode("utf-8", "replace"))
                    if item:
                        collector.process(item, head_phase=True)
                    if line_count >= HEAD_SCAN_LINES or head_bytes >= TAIL_SCAN_BYTES // 2:
                        break
                # Phase 2 (tail): everything, from a byte window at EOF.
                fh.seek(max(0, size - TAIL_SCAN_BYTES))
                fh.readline()  # discard the partial line at the seek point
                for raw in fh:
                    item = parse_transcript_line(raw.decode("utf-8", "replace"))
                    if item:
                        collector.process(item)
    except OSError:
        return empty

    # Goal fallback order: head user_message, tail user_message, then the
    # original request recovered from a `compacted` event's replacement_history.
    if not collector.first_user:
        collector.first_user = collector.compaction_goal
    if not collector.last_user:
        collector.last_user = collector.first_user

    # Attach each command's outcome via call_id. For a collapsed stretch,
    # prefer the most recent FAILING result so a fail->patch->retry cycle
    # can't hide the failure behind the retry.
    for command in collector.commands:
        results = [collector.outputs[i] for i in command.get("call_ids", []) if i in collector.outputs]
        chosen = next(
            (r for r in reversed(results) if isinstance(r.get("exit_code"), int) and r["exit_code"] != 0),
            results[-1] if results else None,
        )
        command["exit_code"] = chosen.get("exit_code") if chosen else None
        command["out_snippet"] = chosen.get("snippet") if chosen else ""

    out = _summary_dict(collector)
    out["windowed"] = windowed
    return out


def _summary_dict(collector):
    # Failures: recent commands that exited non-zero, EXCLUDING probe commands
    # (test/ls/grep/find/...) whose non-zero exit is normal control flow.
    failures = [
        {"command": c["command"], "exit_code": c["exit_code"], "snippet": c.get("out_snippet", "")}
        for c in collector.commands
        if isinstance(c.get("exit_code"), int)
        and c["exit_code"] != 0
        and not is_probe_command(c["command"])
    ][-6:]
    for path in collector.patch_failures[-4:]:
        failures.append({"command": f"apply_patch -> {path}", "exit_code": "patch-failed", "snippet": ""})

    changed = [{"path": path, "type": ctype} for path, ctype in collector.changed_files.items()]

    return {
        "messages": collector.messages[-10:],
        "commands": collector.commands[-12:],
        "token_usage": collector.token_usage[-5:],
        "latest_total_tokens": collector.latest_total_tokens,
        "latest_current_tokens": collector.latest_current_tokens,
        "latest_context_window": collector.latest_context_window,
        "first_user": collector.first_user,
        "last_user": collector.last_user,
        "changed_files": changed[-30:],
        "failures": failures,
        "read_files": collector.read_files[-12:],
        "turn_aborted": collector.turn_aborted,
        "plan": collector.latest_plan,
    }


def goal_from_compacted_item(item):
    """Recover the original user request from a `compacted` event's replacement_history."""
    history = item.get("replacement_history")
    if not isinstance(history, list):
        return ""
    for entry in history:
        if not isinstance(entry, dict) or entry.get("role") != "user":
            continue
        text = text_from_content(entry.get("content"))
        if text and not is_marker_text(text):
            return trim(text)
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

_JS_ESCAPES = {"n": "\n", "t": "\t", "r": "", "\\": "\\", '"': '"', "'": "'", "/": "/"}


def decode_js_str(s):
    """Unescape a JS double-quoted string literal in a single pass.

    Sequential .replace() calls corrupt escaped backslashes ('\\\\n' would
    decode to a newline instead of backslash-n), mangling regexes and printf
    formats in recorded commands.
    """
    return re.sub(r"\\(.)", lambda m: _JS_ESCAPES.get(m.group(1), m.group(1)), s)


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
       Return (inner_tool_label, command_text). A call whose ONLY inner tools are
       apply_patch/update_plan returns "" (they surface in their own sections),
       but a compound block that ALSO runs a command still yields that command."""
    if not isinstance(js, str):
        return None, ""
    names = TOOLS_CALL_RE.findall(js)
    if names and set(names) <= {"update_plan", "apply_patch"}:
        return names[0], ""
    inner = next((n for n in names if n not in ("update_plan", "apply_patch")), names[0] if names else None)
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
    # gpt-5.6 custom_tool_call (name="exec", JS input) — or any input that wraps
    # tools.X(...). Requires an actual harness CALL, not just the substring
    # "tools." (which appears in ordinary commands mentioning tools.py etc.).
    if itype == "custom_tool_call" or (isinstance(arguments, str) and TOOLS_CALL_RE.search(arguments)):
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


EXIT_CODE_RE = re.compile(
    r"^.{0,40}?(?:exited with code|Exit code:|exit code|exited with status|Process exited(?: with(?: code)?)?)\s*(-?\d+)",
    re.I | re.M,
)


def parse_exit_code(text):
    """Parse the wrapper's exit status without being fooled by command OUTPUT
    that merely mentions exit codes (a viewed CI log, `tail deploy.log`, ...).

    Only the wrapper preamble (before the `Output:` marker) and the final lines
    of the body are considered, and matches must sit at the start of a line."""
    t = text or ""
    marker = "\nOutput:\n"
    if marker in t:
        preamble, body = t.split(marker, 1)
        tail = "\n".join(body.rstrip().splitlines()[-2:])
        candidates = preamble + "\n" + tail
    else:
        candidates = t
    matches = EXIT_CODE_RE.findall(candidates)
    if matches:
        return int(matches[-1])
    head = "\n".join(candidates.splitlines()[:3])
    if re.search(r"^\s*Script failed\b", head, re.M):   # gpt-5.6 unified-exec wrapper failure marker
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
# out of the failures list to avoid false positives. `[`/`[[` need their own
# lookahead branch: `\b` can never match after a bracket.
PROBE_CMD_RE = re.compile(
    r"^\s*(?:if\s+)?(?:[A-Za-z0-9_./-]*/)?"
    r"(?:\[\[?(?=\s)|"
    r"(?:test|ls|grep|egrep|fgrep|rg|ag|find|stat|which|type|diff|cmp|pgrep|pidof)\b|"
    r"git\s+(?:diff|grep)\b)"
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
    status = []
    noise = 0
    for line in run_git(workspace, "status", "--short").splitlines():
        if HANDOVER_PREFIX in line:  # don't echo handover files back at ourselves
            continue
        path_part = line.strip().split(" ", 1)[-1].strip()
        if " -> " in path_part:  # rename: only noise if BOTH sides are noise
            noisy = all(PATH_NOISE_RE.search(s.strip()) for s in path_part.split(" -> "))
        else:
            noisy = bool(PATH_NOISE_RE.search(path_part))
        if noisy:
            noise += 1
            continue
        status.append(line)
    return {
        "inside": True,
        "branch": branch or "(detached)",
        "head": head or "unknown",
        "status": status[:80],
        "noise_omitted": noise,
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

def summarize_goal(goal):
    """Automation/scheduled runs open with framework boilerplate (lock protocol,
    heartbeat rules). Keep only the identifying first line so the Goal section
    states the actual mission, not the plumbing.

    Guarded structurally: the prompt must both START like an automation AND
    contain a framework marker (Automation ID:, COMPLETE.flag, .lock, ...).
    A real user prompt that merely begins with "Reminder ..." or "Cron ..."
    is rendered verbatim."""
    if not goal or not AUTOMATION_GOAL_RE.match(goal):
        return goal
    low = goal.lower()
    if not any(marker in low for marker in AUTOMATION_MARKERS):
        return goal
    first_line = goal.splitlines()[0].strip()
    headline = trim(first_line, 300)
    # A collapsed single-line prompt: cut at the first protocol-ish clause.
    if len(first_line) > 290:
        cut = re.split(r"(?i)\b(?:FIRST|STEP 1|Acquire|Read AGENTS)\b", first_line)[0].strip(" .-:")
        if len(cut) >= 40:
            headline = trim(cut, 300)
    return (
        f"{headline}\n"
        "_(Automation run — boilerplate preamble omitted; the automation's ledger/"
        "memory files hold the durable protocol and state.)_"
    )


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
        summarize_goal(goal) or "Continue the active user-requested Codex task.",
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
    real_changes = [c for c in changed if not PATH_NOISE_RE.search(c["path"])]
    noise_paths = [short_path(c["path"], meta["cwd"]) for c in changed if PATH_NOISE_RE.search(c["path"])]
    if real_changes:
        lines += [f"- [{change['type']}] {short_path(change['path'], meta['cwd'])}" for change in real_changes]
        if noise_paths:
            shown = ", ".join(noise_paths[:4]) + ("…" if len(noise_paths) > 4 else "")
            lines += [f"- _(+{len(noise_paths)} runtime/lock/log path(s) omitted: {shown})_"]
    elif noise_paths:
        # Name them rather than deny them — one of these may still matter.
        lines += ["- Only runtime/lock/log paths were written: " + ", ".join(noise_paths[:6])]
    else:
        lines += ["- None recorded (no apply_patch writes in this thread)."]
    if transcript.get("windowed"):
        lines += ["- _(rollout exceeded the scan window; changes from the middle of the session may be missing)_"]

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
        omitted = git.get("noise_omitted", 0)
        summary = f"{len(status)} uncommitted path(s)" if status else "clean tree"
        if omitted:
            summary += f" (+{omitted} lock/log path(s) omitted)"
        lines += [f"- Branch {git['branch']} @ {git['head']} — {summary}"]
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
        repeat = f" (×{entry['repeat']})" if entry.get("repeat", 1) > 1 else ""
        out.append(f"- {mark} {entry['tool']}: {entry['command']}{repeat}{suffix}")
    return out


def trim(text, limit=MAX_SNIPPET_CHARS):
    text = FERNET_RE.sub("[encrypted]", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 15].rstrip() + " [truncated]"


# ---------------------------------------------------------------------------
# State (one file per thread — concurrent Codex windows never share a file)
# ---------------------------------------------------------------------------

def now_iso():
    return _datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def state_dir():
    raw = os.environ.get("CODEX_HANDOVER_STATE_DIR")
    return Path(raw) if raw else DEFAULT_STATE_DIR


def thread_state_path(thread_id):
    return state_dir() / "threads" / f"{thread_id}.json"


def load_thread_state(thread_id):
    path = thread_state_path(thread_id)
    if path.exists():
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(entry, dict):
                return entry
        except Exception:
            pass
        return {}
    return _legacy_thread_entry(thread_id)


def _legacy_thread_entry(thread_id):
    """One-time migration read from the old all-threads state file."""
    legacy = state_dir() / LEGACY_STATE_FILE_NAME
    if not legacy.exists():
        return {}
    try:
        state = json.loads(legacy.read_text(encoding="utf-8"))
        entry = state.get("threads", {}).get(thread_id)
        return entry if isinstance(entry, dict) else {}
    except Exception:
        return {}


def save_thread_state(thread_id, entry):
    path = thread_state_path(thread_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry["schema_version"] = STATE_SCHEMA_VERSION
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def prune_stale_files():
    """Drop thread-state and handover files nothing will ever read again.
    Called from the write path only, so the hot inject paths stay cheap."""
    now = _datetime.datetime.now().timestamp()
    try:
        for path in (state_dir() / "threads").glob("*.json"):
            if now - path.stat().st_mtime > STATE_RETENTION_DAYS * 86400:
                path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        for path in handover_dir().glob(f"*/{HANDOVER_PREFIX}*.md"):
            if now - path.stat().st_mtime > HANDOVER_RETENTION_DAYS * 86400:
                path.unlink(missing_ok=True)
    except OSError:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
