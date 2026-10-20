#!/bin/sh
set -eu
: "${GC_CITY_PATH:?factory check requires the city path}"
exec python3 "$GC_CITY_PATH/commands/midgar/factory-check"
