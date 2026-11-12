#!/usr/bin/env bash
# Simulate a short Claude Code session by piping hook payloads through the real
# hook command, then show what AgentLens recorded.
#
# Safe by default: uses a throwaway AGENTLENS_HOME and never touches
# ~/.claude or ~/.agentlens. Set AGENTLENS_HOME yourself to keep the data.
#
#   ./examples/demo_session.sh
#   PYTHON=/path/to/python ./examples/demo_session.sh
set -euo pipefail

cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
export AGENTLENS_HOME="${AGENTLENS_HOME:-$(mktemp -d)/agentlens}"
HOOK=("$PY" -m agentlens hook claude-code)   # the exact command `agentlens install` registers

SID="demo-$(date +%s)-7e3b"
CWD="$PWD"
common="\"session_id\":\"$SID\",\"transcript_path\":\"/tmp/$SID.jsonl\",\"cwd\":\"$CWD\",\"permission_mode\":\"default\""

send() {  # send <HookEvent> '<extra json fields>'
  printf '{%s,"hook_event_name":"%s"%s}' "$common" "$1" "${2:+,$2}" | "${HOOK[@]}"
}

echo "AGENTLENS_HOME=$AGENTLENS_HOME"
echo "Session $SID"
echo

send SessionStart '"source":"startup","model":"claude-sonnet-5-5"'
send UserPromptSubmit '"prompt":"Make the tests pass. The staging password is demo-prompt-secret."'

send PreToolUse '"tool_name":"Bash","tool_use_id":"toolu_demo_1","tool_input":{"command":"API_TOKEN=demo-secret-value pytest -q","description":"Run tests"}'
sleep 0.4
send PostToolUseFailure '"tool_name":"Bash","tool_use_id":"toolu_demo_1","tool_input":{"command":"API_TOKEN=demo-secret-value pytest -q"},"error":"Exit code 1\nFAILED tests/test_cart.py::test_total"'

send PreToolUse '"tool_name":"Edit","tool_use_id":"toolu_demo_2","tool_input":{"file_path":"'"$CWD"'/src/cart.py","old_string":"return 0","new_string":"return sum(items)"}'
sleep 0.2
send PostToolUse '"tool_name":"Edit","tool_use_id":"toolu_demo_2","tool_input":{"file_path":"'"$CWD"'/src/cart.py"},"tool_response":{"filePath":"'"$CWD"'/src/cart.py","structuredPatch":[]}'

send PreToolUse '"tool_name":"Write","tool_use_id":"toolu_demo_3","tool_input":{"file_path":"'"$CWD"'/.env.test","content":"DB_PASSWORD=demo-file-content\n"}'
send PostToolUse '"tool_name":"Write","tool_use_id":"toolu_demo_3","tool_input":{"file_path":"'"$CWD"'/.env.test"},"tool_response":{"success":true},"duration_ms":3'

send PreToolUse '"tool_name":"Bash","tool_use_id":"toolu_demo_4","tool_input":{"command":"pytest -q"}'
send PostToolUse '"tool_name":"Bash","tool_use_id":"toolu_demo_4","tool_input":{"command":"pytest -q"},"tool_response":{"stdout":"4 passed","stderr":"","interrupted":false},"duration_ms":1312'

send Stop '"stop_hook_active":false,"last_assistant_message":"All tests pass now."'
send SessionEnd '"reason":"prompt_input_exit"'

"$PY" -m agentlens sessions
echo
"$PY" -m agentlens inspect "$SID"
echo
echo "Privacy check: searching everything AgentLens wrote for the demo secrets..."
if grep -raE 'demo-secret-value|demo-prompt-secret|demo-file-content' "$AGENTLENS_HOME"; then
  echo "LEAK: a demo secret was found on disk" >&2
  exit 1
fi
echo "OK: no prompt text, file content or secret values on disk."
