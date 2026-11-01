#!/bin/bash
# Verify an outbound draft: deterministic precheck, then a judge with no search tools.
#
#   .engagement/emails/verify.sh <draft.md>
#
# The judge runs through `claude -p` rather than the Agent tool so its toolset is ENFORCED rather
# than merely instructed. Handed the whole workspace, a search capable agent searches: told to use
# the precheck as a fast path it still cost 66k tokens and 132 seconds, because instructions do not
# constrain a tool. Read and Write only, and it cannot.
set -euo pipefail
DRAFT="$1"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SLUG="$(basename "$DRAFT" .md)"
PRE="$HERE/prechecks/$SLUG.json"
OUT="$HERE/verdicts/$SLUG.json"

python3 "$HERE/precheck.py" "$DRAFT" || {
  echo; echo "precheck found problems. Fix the manifest before judging."; exit 1; }

BODY="$(python3 "$HERE/emails.py" body "$DRAFT")"
BEFORE="$(python3 -c "import sys;sys.path.insert(0,'$HERE');import emails;print(emails.body_digest('$DRAFT'))")"
# --permission-mode acceptEdits, because a print session cannot prompt: without it the judge reaches
# the end of its work and stops with "awaiting permission to write the verdict", having done all the
# thinking and saved none of it. The toolset is still Read and Write only, which is the actual
# constraint; acceptEdits governs approval, not capability.
claude -p --model haiku --output-format json \
  --permission-mode acceptEdits \
  --allowedTools "Read,Write" \
  --append-system-prompt "$(cat "$HERE/claim-judge.md")" <<EOF | python3 -c '
import sys, json, re
raw = sys.stdin.read()
m = re.search(r"\{.*\}", raw, re.S)
j = json.loads(m.group(0))
u = j.get("usage", {})
print(j.get("result", "")[:1200])
print()
print("judge  %s tokens, %.0fs, $%.4f" % (
    u.get("cache_creation_input_tokens", 0) + u.get("input_tokens", 0) + u.get("output_tokens", 0),
    j.get("duration_ms", 0) / 1000.0, j.get("total_cost_usd", 0)))
'
Precheck JSON: $PRE
Write the verdict to: $OUT
Today is $(TZ=America/New_York date +%Y-%m-%d) US Eastern.

BODY:
$BODY
EOF

# A verdict is only valid for the body it judged. Two verify runs on the same draft, or one run while
# the draft is being edited, and the SLOWER one writes last: the verdict on disk then describes text
# nobody is sending. That happened on 2026-09-04, where an earlier background run finished after a
# later one and restored a verdict for a greeting that had since been corrected. `emails.py draft`
# catches the mismatch at registration, but only if someone registers again; a verdict already
# accepted stays pointing at the wrong file. So refuse to leave a stale one on disk.
AFTER="$(python3 -c "import sys;sys.path.insert(0,'$HERE');import emails;print(emails.body_digest('$DRAFT'))")"
if [ "$BEFORE" != "$AFTER" ]; then
  rm -f "$OUT"
  echo
  echo "The draft body changed while the judge was running, so its verdict was discarded."
  echo "Re-run verify.sh on the final text."
  exit 1
fi
STAMPED="$(python3 -c "import json;print(json.load(open('$OUT')).get('bodySha256',''))" 2>/dev/null || true)"
if [ -n "$STAMPED" ] && [ "$STAMPED" != "$AFTER" ]; then
  rm -f "$OUT"
  echo
  echo "The verdict written does not match the current body, which means a concurrent run wrote it."
  echo "Discarded. Re-run verify.sh, and do not run two passes on one draft at once."
  exit 1
fi
