#!/bin/sh
set -eu
# Schema migrations are owned by the web service. Running them here would race
# with app startup when worker and scheduler containers start together.
exec "$@"
