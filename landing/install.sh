#!/bin/bash
# Claude Control - one-line installer (thin wrapper).
#
# This file is what claude.sekoia.ca/install.sh serves. It delegates to the
# canonical installer in the repo so it can never drift from scripts/install.sh.
#
# Source of truth: https://github.com/fabultra/claude-control/blob/main/scripts/install.sh
set -e
exec /bin/bash <(curl -fsSL https://raw.githubusercontent.com/fabultra/claude-control/main/scripts/install.sh)
