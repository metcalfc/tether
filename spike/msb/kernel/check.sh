#!/bin/bash
# Boot a throwaway msb sandbox on a given libkrunfw and run guest-check.sh.
#
#   spike/msb/kernel/check.sh [path/to/libkrunfw.5.dylib]
#
# With no argument, msb uses its stock libkrunfw (the negative control).
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
msb=${MSB:-$HOME/.microsandbox/bin/msb}
nono_dir=${NONO_GUEST_DIR:-$HOME/.microsandbox/tether-guest/bin}
image=${CHECK_IMAGE:-node:22-bookworm}

if [ $# -ge 1 ]; then
    export MSB_LIBKRUNFW_PATH
    MSB_LIBKRUNFW_PATH=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
    echo "MSB_LIBKRUNFW_PATH=$MSB_LIBKRUNFW_PATH"
fi

perl -e 'alarm shift; exec @ARGV' 180 "$msb" run \
    --net-default deny \
    --mount-dir "$nono_dir:/opt/nono:ro" \
    --mount-dir "$here:/opt/kcheck:ro" \
    "$image" -- bash /opt/kcheck/guest-check.sh
