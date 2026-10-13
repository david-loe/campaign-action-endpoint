#!/bin/sh
set -eu
python -m campaign_action migrate
exec "$@"
