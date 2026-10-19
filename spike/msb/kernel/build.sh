#!/bin/bash
# Build microsandbox's libkrunfw with Landlock enabled.
#
#   spike/msb/kernel/build.sh <libkrunfw checkout> <output dir>
#
# The kernel is built inside an arm64 Fedora container on the container's own
# filesystem (the kernel tree has case-only name clashes that a default APFS
# volume cannot hold). Only kernel.c comes back to the host, where the
# libkrunfw Makefile links it into libkrunfw.5.dylib.
set -euo pipefail

src=${1:?usage: build.sh <libkrunfw checkout> <output dir>}
out=${2:?usage: build.sh <libkrunfw checkout> <output dir>}
here=$(cd "$(dirname "$0")" && pwd)
src=$(cd "$src" && pwd)
mkdir -p "$out"
out=$(cd "$out" && pwd)

cfg="$src/config-libkrunfw_aarch64"
[ -f "$cfg.orig" ] || cp "$cfg" "$cfg.orig"
cp "$cfg.orig" "$cfg"
# Drop every line that sets or unsets a key from the fragment, then append it.
keys=$(grep -E '^CONFIG_' "$here/landlock.config" | cut -d= -f1 | paste -sd'|' -)
grep -vE "^(# )?(${keys})([= ]|$)" "$cfg.orig" > "$cfg" || true
grep -E '^CONFIG_' "$here/landlock.config" >> "$cfg"

docker run --rm --platform linux/arm64 \
    -v "$src:/src:ro" -v "$out:/out" \
    fedora:latest bash -c '
        set -euo pipefail
        dnf install -y -q "dnf-command(builddep)" python3-pyelftools curl rsync >/dev/null
        dnf builddep -y -q kernel >/dev/null
        rsync -a --exclude kernel.c --exclude "linux-*" /src/ /build/
        cd /build
        make -j"$(nproc)" kernel.c
        grep -E "^CONFIG_(SECURITY|SECURITY_LANDLOCK|LSM)=" linux-*/.config > /out/kernel-config.txt
        cp linux-*/.config /out/kernel.config
        cp kernel.c /out/kernel.c
    '

cp "$out/kernel.c" "$src/kernel.c"
(cd "$src" && make)
cp "$src/libkrunfw.5.dylib" "$out/libkrunfw.5.dylib"
ln -sf libkrunfw.5.dylib "$out/libkrunfw.dylib"
cat "$out/kernel-config.txt"
