#!/usr/bin/env bash
# Explicit real-city smoke test. This mutates the selected rig's bead store and
# leaves both output files in the rig for inspection; invoke only in an active city.
# This does not establish claim identity, duplicate-claim behavior, or drain
# acknowledgement: the completed-workflow CLI view does not expose those
# lifecycle events. Those acceptance checks remain blocked pending an active-city run.
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 CITY_DIR RIG_DIR" >&2
  exit 2
fi
city_dir=$(cd "$1" && pwd)
rig_dir=$(cd "$2" && pwd)
rig_name=$(basename "$rig_dir")
run_id="volume-smoke-$(date +%s)"

# Keep the source description numeric-only: it is the payload under test.
task_id=$(gc --city "$city_dir" --rig "$rig_name" bd create \
  --title "Volume formula smoke input $run_id" --description "1000" --silent)

# --on routes the formula root with this source bead as its invocation convoy.
sling_json=$(gc --city "$city_dir" --rig "$rig_name" sling \
  gascity-irc-pack/volume-converter "$task_id" --on claim-worker-smoke --nudge --json)
root_id=$(printf '%s\n' "$sling_json" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["workflow_id"])')

# Wait for the formula root itself, not merely the source task, to close.
deadline=$((SECONDS + ${SMOKE_TIMEOUT_SECONDS:-1800}))
while (( SECONDS < deadline )); do
  root_json=$(gc --city "$city_dir" --rig "$rig_name" bd show "$root_id" --json)
  if printf '%s' "$root_json" | python3 -c 'import json,sys; data=json.load(sys.stdin); data=data[0] if isinstance(data,list) else data; raise SystemExit(0 if data.get("status") == "closed" else 1)'; then
    break
  fi
  sleep 5
done
if (( SECONDS >= deadline )); then
  echo "timed out waiting for formula root: root=$root_id source=$task_id" >&2
  exit 1
fi

for file in volume-converter.txt volume-doubler.txt; do
  [[ -f "$rig_dir/$file" ]] || { echo "missing visible output: $rig_dir/$file" >&2; exit 1; }
done
[[ $(<"$rig_dir/volume-converter.txt") == "1 m^3" ]]
[[ $(<"$rig_dir/volume-doubler.txt") == "2 m^3" ]]
printf 'PASS task=%s root=%s\noutputs retained in %s\n' "$task_id" "$root_id" "$rig_dir"
