"""Check the 02-tracing result end to end.

Run with the app already running (`make app`) in another terminal:

    make verify-tracing

1. Sends one chat turn to the running server and reads its trace back from
   Langfuse by session ID.
2. Checks the trace shape: one trace, a `dad-it-support-chat-turn` agent root,
   OpenAI generations, both tool observations, the root input/output, and the
   user ID, session ID, and tags.
3. Starts a second server without Langfuse keys (port 8799) and checks that it
   still answers and logs exactly one warning.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

from langfuse import Langfuse

from app.env import env, is_langfuse_configured

TRACE_NAME = "dad-it-support-chat-turn"
TRACE_TAGS = {"langfuse-workshop", "dad-it-support"}
TOOL_NAMES = {"get_support_context", "search_help_library"}
NO_KEYS_PORT = 8799
PYTHON_ROOT = Path(__file__).resolve().parent.parent

failures = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    global failures
    if not ok:
        failures += 1
    suffix = f" ({detail})" if detail else ""
    print(f"{'PASS' if ok else 'FAIL'}  {label}{suffix}")
    return ok


def http_json(url: str, body: dict | None = None, timeout: float = 120) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def chat_payload(session_id: str) -> dict:
    return {
        "sessionId": session_id,
        "userId": "verify-tracing",
        "messages": [
            {
                "id": "m1",
                "role": "user",
                "content": "How do I turn Bluetooth on on my iPhone?",
                "timestamp": "2026-01-01T00:00:00Z",
            }
        ],
    }


def parse_io(value):
    # The observations API returns input/output as raw strings.
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def fetch_observations(langfuse: Langfuse, session_id: str) -> list:
    # Traces take a few seconds to appear: wait until the root and both tools are in.
    deadline = time.time() + 90
    observations: list = []
    while time.time() < deadline:
        response = langfuse.api.observations.get_many(
            session_id=session_id, fields="core,basic,io,trace_context", limit=100
        )
        observations = response.data
        names = {observation.name for observation in observations}
        if TRACE_NAME in names and TOOL_NAMES <= names:
            time.sleep(3)  # give late generations a moment to land
            return langfuse.api.observations.get_many(
                session_id=session_id, fields="core,basic,io,trace_context", limit=100
            ).data
        time.sleep(3)
    return observations


def verify_traced_turn() -> None:
    base = f"http://127.0.0.1:{env.port}"
    try:
        health = http_json(f"{base}/api/health")
    except OSError as error:
        check(f"server is running on {base}", False, f"{error}; start it with `make app`")
        return
    check("GET /api/health reports tracingConfigured: true", health.get("tracingConfigured") is True, str(health))

    session_id = f"verify-tracing-{uuid.uuid4().hex[:8]}"
    response = http_json(f"{base}/api/chat", chat_payload(session_id))
    check("chat turn returns an answer", bool(response.get("answer")))
    print(f"      session ID: {session_id}")

    langfuse = Langfuse(
        public_key=env.langfuse_public_key,
        secret_key=env.langfuse_secret_key,
        base_url=env.langfuse_base_url,
        tracing_enabled=False,
    )
    observations = fetch_observations(langfuse, session_id)
    trace_ids = {observation.trace_id for observation in observations}
    if not check("one chat turn creates exactly one trace", len(trace_ids) == 1, f"traces={len(trace_ids)}"):
        return

    roots = [o for o in observations if o.name == TRACE_NAME]
    root = roots[0] if roots else None
    check(
        f"root observation is {TRACE_NAME} of type agent",
        root is not None and root.type == "AGENT" and bool(root.is_root_observation),
        "" if root is None else f"type={root.type}, isRoot={root.is_root_observation}",
    )

    generations = [o for o in observations if o.type == "GENERATION"]
    check("child OpenAI generations are recorded", len(generations) >= 1, f"generations={len(generations)}")

    tools = {o.name for o in observations if o.type == "TOOL"}
    check("tool observations get_support_context and search_help_library", TOOL_NAMES <= tools, f"tools={sorted(tools)}")

    if root is not None:
        root_input = parse_io(root.input) or {}
        root_output = parse_io(root.output) or {}
        check(
            "root input is the chat request",
            isinstance(root_input, dict) and root_input.get("sessionId") == session_id and "messages" in root_input,
        )
        check(
            "root output is the chat response",
            isinstance(root_output, dict) and bool(root_output.get("answer")) and "usedTools" in root_output,
        )
        check("trace carries the user ID", root.user_id == "verify-tracing", f"userId={root.user_id}")
        check("trace carries the session ID", root.session_id == session_id, f"sessionId={root.session_id}")
        check("trace carries the workshop tags", TRACE_TAGS <= set(root.tags or []), f"tags={root.tags}")


def verify_without_keys() -> None:
    # The app loads .env with override=True, so clear the keys after import, not before.
    launcher = (
        "import dataclasses, os\n"
        "import app.env as e\n"
        "for key in ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY'):\n"
        "    os.environ.pop(key, None)\n"
        "e.env = dataclasses.replace(e.env, port=%d, langfuse_public_key='', langfuse_secret_key='')\n"
        "from app.server import main\n"
        "main()\n" % NO_KEYS_PORT
    )
    process = subprocess.Popen(
        [sys.executable, "-c", launcher],
        cwd=PYTHON_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{NO_KEYS_PORT}"
    try:
        for _ in range(60):
            try:
                health = http_json(f"{base}/api/health", timeout=2)
                break
            except OSError:
                time.sleep(0.5)
        else:
            check(f"server without Langfuse keys starts on port {NO_KEYS_PORT}", False)
            return
        check("without keys, /api/health reports tracingConfigured: false", health.get("tracingConfigured") is False)
        response = http_json(f"{base}/api/chat", chat_payload(f"verify-no-keys-{uuid.uuid4().hex[:8]}"))
        check("without keys, the app still answers", bool(response.get("answer")))
    finally:
        process.terminate()
        output, _ = process.communicate(timeout=15)

    warnings = [line for line in output.splitlines() if "WARNING" in line]
    check("without keys, the app logs exactly one warning", len(warnings) == 1, f"warnings={len(warnings)}")


def main() -> int:
    if not is_langfuse_configured():
        print("Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY in the repo-root .env first.")
        return 1
    verify_traced_turn()
    verify_without_keys()
    print("\nAll checks passed." if failures == 0 else f"\n{failures} check(s) failed.")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    os.chdir(PYTHON_ROOT)
    sys.exit(main())
