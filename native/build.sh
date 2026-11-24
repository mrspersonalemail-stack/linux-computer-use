#!/bin/sh
set -eu

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
output=${1:-"$source_dir/drag"}
if [ "$#" -gt 1 ]; then
    printf 'Usage: %s [output-path]\n' "$0" >&2
    exit 2
fi
build_dir=$(mktemp -d "$(dirname -- "$output")/.drag-build.XXXXXXXX")
trap 'rm -rf -- "$build_dir"' EXIT
trap 'exit 1' HUP INT TERM
protocol="$source_dir/wlr-virtual-pointer-unstable-v1.xml"

wayland-scanner client-header "$protocol" "$build_dir/virtual-pointer-client.h"
wayland-scanner private-code "$protocol" "$build_dir/virtual-pointer.c"
client_flags=$(pkg-config --cflags --libs wayland-client)
# pkg-config emits separate compiler arguments.
# shellcheck disable=SC2086
"${CC:-cc}" -std=c11 -O2 -Wall -Wextra -Werror -I"$build_dir" \
    "$source_dir/drag.c" "$build_dir/virtual-pointer.c" $client_flags -o "$build_dir/drag"
mv -fT -- "$build_dir/drag" "$output"
