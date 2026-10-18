#!/usr/bin/env bash
# Explicit real-city smoke test. This mutates the selected rig's bead store and
# leaves both output files in the rig for inspection; invoke only in an active city.
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
convoy_json=$(gc --city "$city_dir" --rig "$rig_name" convoy create "$run_id" "$task_id" --json)
convoy_id=$(printf '%s\n' "$convoy_json" | python3 -c 'import json,sys; lines=[line for line in sys.stdin if line.strip()]; print(json.loads(lines[-1])["id"])')

gc --city "$city_dir" --rig "$rig_name" formula cook claim-worker-smoke \
  --attach "$task_id" --var "convoy_id=$convoy_id"

# Wait for the attached source task to close, which requires the formula DAG to finish.
deadline=$((SECONDS + ${SMOKE_TIMEOUT_SECONDS:-1800}))
while (( SECONDS < deadline )); do
  task_json=$(gc --city "$city_dir" --rig "$rig_name" bd show "$task_id" --json)
  if printf '%s' "$task_json" | python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("status") == "closed" else 1)'; then
    break
  fi
  sleep 5
done
if (( SECONDS >= deadline )); then
  echo "timed out waiting for formula completion: task=$task_id convoy=$convoy_id" >&2
  exit 1
fi

for file in volume-converter.txt volume-doubler.txt; do
  [[ -f "$rig_dir/$file" ]] || { echo "missing visible output: $rig_dir/$file" >&2; exit 1; }
done
[[ $(<"$rig_dir/volume-converter.txt") == "1 m^3" ]]
[[ $(<"$rig_dir/volume-doubler.txt") == "2 m^3" ]]
printf 'PASS task=%s convoy=%s\noutputs retained in %s\n' "$task_id" "$convoy_id" "$rig_dir"
