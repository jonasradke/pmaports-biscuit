#!/bin/sh
# Rebuilds biscuit-pryon-helper.bin.gz, the prebuilt binary the package carries. Run it by hand and commit the
# result: the engine it talks to is 32-bit ARM with Android's libc, which the package's own aarch64 musl
# toolchain cannot target, and no Amazon file is needed, since it links against biscuit-pryon-stubs.c.
#
#   pip install ziglang==0.17.1
#   sh biscuit-pryon-helper.build.sh
set -e
cd "$(dirname "$0")"
ZIG="${ZIG:-python -m ziglang}"
TMP=.pryon-build.$$
mkdir "$TMP"
trap 'rm -rf "$TMP"' EXIT
TARGET="-target arm-linux-musleabi -mcpu=cortex_a7"

$ZIG cc $TARGET -shared -nostdlib -DSTUB_PRYON -Wl,-soname,libpryon.so \
    -Wno-incompatible-library-redeclaration -o "$TMP/libpryon.so" biscuit-pryon-stubs.c
$ZIG cc $TARGET -shared -nostdlib -Wl,-soname,libc.so \
    -Wno-incompatible-library-redeclaration -o "$TMP/libc.so" biscuit-pryon-stubs.c

# The linker named here is where the importer puts the one from the owner's Fire OS.
$ZIG cc $TARGET -O2 -fPIE -pie -nostdlib -fno-stack-protector -s \
    -Wno-incompatible-library-redeclaration \
    -Wl,--dynamic-linker=/var/lib/biscuit-amazon/amazonwake/bin/linker \
    -Wl,--hash-style=sysv -Wl,--allow-shlib-undefined \
    -o biscuit-pryon-helper.bin biscuit-pryon-helper.c "$TMP/libpryon.so" "$TMP/libc.so"
# Gzipped, because abuild would otherwise run the aarch64 strip over a 32-bit ARM file. The package's
# post-install script unpacks it.
gzip -9nf biscuit-pryon-helper.bin
