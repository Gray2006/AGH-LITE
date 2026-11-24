#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
python -m agh_lite ui "$@"
