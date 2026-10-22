#!/usr/bin/env bash
# Run the full offline test suite: pytest modules, script-style engine checks, and the admin JS test.
# Usage: tests/run_all.sh   (needs uv and node)
set -euo pipefail
cd "$(dirname "$0")/.."
MAC_CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
if [ -z "${MUSE2API_CHROMIUM:-}" ] && [ -x "$MAC_CHROME" ]; then export MUSE2API_CHROMIUM="$MAC_CHROME"; fi

PY=(uv run -q --no-project --python 3.12 --with-requirements requirements.txt --with pytest python)
SCRIPT_TESTS=(test_media_selection test_quota_account_binding test_vm_wait test_wake_confirmation)

ignore=()
for t in "${SCRIPT_TESTS[@]}"; do ignore+=(--ignore "tests/$t.py"); done
"${PY[@]}" -m pytest -q -p no:cacheprovider "${ignore[@]}" tests

for t in "${SCRIPT_TESTS[@]}"; do
  echo "--- $t"
  "${PY[@]}" "tests/$t.py" engine.py --assert
done

echo "--- test_admin_api_error_messages"
node tests/test_admin_api_error_messages.mjs
echo "ALL TESTS PASSED"
