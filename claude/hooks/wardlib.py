"""Shared plumbing for the wards code-quality hooks.

Every hook script does `sys.path.insert(0, os.path.dirname(__file__))` and
imports from here. Keep this dependency-free (Python 3.11+ stdlib only).

Provides:
  config(section)         -> dict from the nearest .wards/config.toml, env-overridable
  audit(verdict, detail)  -> append a TSV line to the shared audit log
  Session(session_id)     -> per-session JSON state (baselines, warned keys)
  rel(path)               -> path relative to cwd for human-readable messages
  find_upward(start, rel) -> nearest ancestor containing rel, or None
  block(text) / warn_context(event, lines) / read_payload()
"""

import hashlib
import json
import os
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python < 3.11: run without .wards/config.toml rather than crash
    tomllib = None

AUDIT_LOG = Path(os.environ.get("WARDS_AUDIT_LOG", Path.home() / ".wards" / "audit.log"))
STATE_DIR = Path(os.environ.get("WARDS_STATE_DIR", Path.home() / ".wards" / "state"))
FILE_TOOLS = {"Edit", "Write", "MultiEdit"}
STOP_BLOCK_LIMIT = int(os.environ.get("WARDS_STOP_BLOCK_LIMIT", "3"))
CONFIG_REL = Path(".wards") / "config.toml"

# --------------------------------------------------------------------- config

_config_cache = {}


def find_upward(start, rel):
    """Nearest ancestor of `start` (inclusive) that contains `rel`."""
    p = Path(start).resolve()
    if p.is_file():
        p = p.parent
    for parent in [p, *p.parents]:
        if (parent / rel).exists():
            return parent / rel
    return None


def project_root(start=None):
    """Directory holding .wards/config.toml, else $CLAUDE_PROJECT_DIR, else cwd."""
    cfg = find_upward(start or os.getcwd(), CONFIG_REL)
    if cfg:
        return cfg.parent.parent
    return Path(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))


def config(section, start=None):
    """Return the given [section] of the nearest .wards/config.toml as a dict
    (empty if absent). Env vars WARDS_<SECTION>_<KEY> override individual keys."""
    cfg_path = find_upward(start or os.getcwd(), CONFIG_REL)
    key = str(cfg_path)
    if key not in _config_cache:
        data = {}
        if cfg_path and tomllib is None:
            audit(
                "WARN: config",
                f"{cfg_path} ignored: Python {sys.version_info.major}.{sys.version_info.minor} "
                "lacks tomllib (need 3.11+)",
            )
        elif cfg_path:
            try:
                data = tomllib.loads(cfg_path.read_text())
            except (tomllib.TOMLDecodeError, OSError) as exc:
                audit("WARN: config", f"unreadable {cfg_path}: {exc}")
        _config_cache[key] = data
    sec = dict(_config_cache[key].get(section, {}))
    prefix = f"WARDS_{section.upper()}_"
    for env_key, val in os.environ.items():
        if env_key.startswith(prefix):
            sec[env_key[len(prefix) :].lower()] = _coerce(val)
    return sec


def _coerce(val):
    """Env override values: true/false, ints, JSON lists/objects, else string."""
    low = val.lower()
    if low in ("true", "false"):
        return low == "true"
    if val[:1] in "[{":
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            return val
    try:
        return int(val)
    except ValueError:
        return val


# ---------------------------------------------------------------------- audit


def audit(verdict, detail):
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).astimezone().isoformat(timespec="seconds")
    detail = str(detail).replace("\n", "\\n")  # one entry = one line
    with AUDIT_LOG.open("a") as fh:
        fh.write(f"{stamp}\t{verdict}\t{detail}\n")


# ---------------------------------------------------------------------- state


STATE_TTL_DAYS = int(os.environ.get("WARDS_STATE_TTL_DAYS", "14"))


def prune_state():
    """Drop session state files older than STATE_TTL_DAYS (cheap; runs on each Session open)."""
    cutoff = time.time() - STATE_TTL_DAYS * 86400
    try:
        for f in STATE_DIR.glob("*.json*"):
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except OSError:
        pass


class Session:
    """Per-session JSON blob keyed by hook name: baselines, warned keys, etc.

    s = Session(session_id, "complexity")
    s.data["baseline"][path] = {...}
    s.save()
    """

    def __init__(self, session_id, hook):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        prune_state()
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
        self.path = STATE_DIR / f"{safe}.{hook}.json"
        self.data = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError) as exc:  # corrupt state: start fresh, say so
                audit("WARN: state", f"{self.path.name} unreadable ({exc}); resetting")
        self.data.setdefault("baseline", {})
        self.data.setdefault("warned", [])

    def save(self):
        """Atomic: a hook killed mid-write must not leave torn JSON that crashes every later run."""
        tmp = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        os.replace(tmp, self.path)

    @property
    def baseline(self):
        return self.data["baseline"]

    def touched(self):
        return list(self.baseline.keys())

    def warn_once(self, key):
        """True the first time `key` is seen this session, False afterwards."""
        if key in self.data["warned"]:
            return False
        self.data["warned"].append(key)
        return True

    def may_block_stop(self, limit=STOP_BLOCK_LIMIT):
        """Rate-limit Stop blocks: True (and count it) up to `limit` consecutive
        times, then False so a hook can never trap the agent in a loop. Reset
        by clear_stop_blocks() when a Stop passes clean."""
        n = self.data.get("stop_blocks", 0)
        if n >= limit:
            audit(
                "WARN: stop-limit", f"{self.path.name}: {n} consecutive Stop blocks; letting the agent stop"
            )
            return False
        self.data["stop_blocks"] = n + 1
        self.save()
        return True

    def clear_stop_blocks(self):
        self.data["stop_blocks"] = 0
        self.save()


# --------------------------------------------------------------------- output


def rel(path):
    try:
        return os.path.relpath(path)
    except ValueError:
        return str(path)


def read_payload():
    try:
        return json.load(sys.stdin)
    except json.JSONDecodeError:
        return {}


def block(text):
    """Exit 2 with `text` on stderr — Claude Code feeds it back to the agent."""
    sys.stderr.write(text.rstrip("\n") + "\n")
    sys.exit(2)


def warn_context(event, header, lines):
    """Non-blocking: inject context into the agent's turn (PreToolUse / PostToolUse;
    Stop has no context channel, so callers audit instead)."""
    if event not in ("PreToolUse", "PostToolUse") or not lines:
        return
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": header + "\n" + "\n".join(f"- {ln}" for ln in lines),
                }
            },
            ensure_ascii=False,
        )
    )


def in_project(path):
    """True if `path` is inside the project ($CLAUDE_PROJECT_DIR); scratch files in
    /tmp etc. are not the project's code and are not judged. No project dir set
    (tests, ad-hoc use) → everything counts."""
    root = os.environ.get("CLAUDE_PROJECT_DIR")
    if not root or os.environ.get("WARDS_JUDGE_OUTSIDE_PROJECT") == "1":
        return True
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return True


def config_fingerprint_changed(session_id, start=None):
    """Remember the sha256 of the nearest .wards/config.toml the first time a
    session sees it; return a short description if it differs now, else "".
    Makes mid-session tampering visible even when the write itself slipped by."""
    cfg_path = find_upward(start or os.getcwd(), CONFIG_REL)
    if not cfg_path or not session_id:
        return ""
    try:
        digest = hashlib.sha256(cfg_path.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""
    s = Session(session_id, "config")
    seen = s.data.setdefault("fingerprints", {})
    key = str(cfg_path)
    if key not in seen:
        seen[key] = digest
        s.save()
        return ""
    if seen[key] != digest:
        old, seen[key] = seen[key], digest  # report once, then accept the new state
        s.save()
        return f"{cfg_path}: {old} -> {digest}"
    return ""


def bands(value, default, name="threshold"):
    """Validate a 3-int ascending band list from config; fall back to default loudly."""
    if (
        isinstance(value, list)
        and len(value) == 3
        and all(isinstance(v, int) and not isinstance(v, bool) for v in value)
        and value[0] < value[1] < value[2]
    ):
        return list(value)
    audit("WARN: config", f"{name}={value!r} is not three ascending ints; using {default}")
    return list(default)


def as_list(value):
    """Config values that should be lists: accept a real list, or a comma-separated
    string (env overrides can't express TOML lists)."""
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return []


def anchor(payload, hook):
    """The path that config lookups should resolve from: the edited file for
    Pre/PostToolUse, the first file touched this session for Stop, else cwd.
    Lets a subproject's .wards/config.toml win over the workspace root's."""
    path = payload.get("tool_input", {}).get("file_path")
    if path:
        return path
    if payload.get("hook_event_name") == "Stop" and payload.get("session_id"):
        touched = Session(payload["session_id"], hook).touched()
        if touched:
            return touched[0]
    return payload.get("cwd") or os.getcwd()


def run_hook(hook, configure, handlers, tools=FILE_TOOLS):
    """Standard entry point: read payload, resolve config from the right anchor,
    dispatch — with the crash handler around ALL of it (a bad config value must
    not produce a silent, un-audited traceback). handlers = (on_pre, on_post, on_stop)."""
    on_pre, on_post, on_stop = handlers
    payload = read_payload()
    try:
        configure(anchor(payload, hook))
    except SystemExit:
        raise
    except Exception as exc:  # broad on purpose: last-resort visibility, not control flow
        _crash(hook, payload, exc)
    dispatch(payload, on_pre, on_post, on_stop, tools)


def _crash(hook, payload, exc):
    event, tool = payload.get("hook_event_name"), payload.get("tool_name", "")
    audit(f"CRASH: {hook}", f"{event}/{tool}: {exc!r}")
    sys.stderr.write(
        f"WARD CRASH ({hook}) on {event}: {exc!r} — this ward is not enforcing; tell the human.\n"
    )
    sys.stderr.write(traceback.format_exc())
    sys.exit(1)


def dispatch(payload, on_pre, on_post, on_stop, tools=FILE_TOOLS):
    """Route a hook payload to the right handler. Handlers take (payload).
    A crash inside a handler must never look like a clean pass: it is
    audit-logged and reported to the user (exit 1, non-blocking) with the
    hook name, so a broken ward is visible rather than silently inactive."""
    event = payload.get("hook_event_name")
    tool = payload.get("tool_name", "")
    try:
        if event == "PreToolUse" and tool in tools:
            on_pre(payload)
        elif event == "PostToolUse" and tool in tools:
            on_post(payload)
        elif event == "Stop":
            # Deliberately NOT short-circuiting on stop_hook_active: after a block the
            # agent fixes and stops again, and that second Stop must be re-checked.
            # Session.may_block_stop() caps consecutive blocks so there is no loop.
            on_stop(payload)
    except SystemExit:
        raise
    except Exception as exc:  # broad on purpose: last-resort visibility, not control flow
        _crash(os.path.basename(sys.argv[0]) if sys.argv else "ward", payload, exc)
