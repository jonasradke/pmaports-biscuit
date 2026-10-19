#!/bin/sh
# Build the complete corresponding source of the kernel a release ships,
# linux-amazon-biscuit-r<kernel pkgrel>-source.tar.gz, laid out like the
# upstream project's r243 bundle:
#
#   linux-amazon-biscuit-<commit>.tar.gz   the upstream kernel archive, taken
#                                          from the upstream r243 bundle (GitHub
#                                          no longer serves it byte-identical)
#   linux-amazon-biscuit/                  this repository's kernel package
#                                          directory: APKBUILD, patches, the
#                                          MT6625L driver, install hooks
#   kernel-config-r<N>                     the configuration embedded in the
#                                          built kernel (CONFIG_IKCONFIG)
#   prepare-tree.sh, README.txt, SHA256SUMS
#
# Usage: tools/ci/kernel-source.sh <upstream r243 bundle> <kernel apk> <out dir>
set -eu

UPSTREAM=${1:?usage: kernel-source.sh <upstream bundle> <kernel apk> <out dir>}
APK=${2:?usage: kernel-source.sh <upstream bundle> <kernel apk> <out dir>}
OUT=${3:?usage: kernel-source.sh <upstream bundle> <kernel apk> <out dir>}
REPO=$(cd "$(dirname "$0")/../.." && pwd)
PKG="$REPO/device/testing/linux-amazon-biscuit"

die() { echo "kernel-source.sh: $*" >&2; exit 1; }

eval "$(grep -E '^(pkgver|pkgrel|_commit)=' "$PKG/APKBUILD")"
case "$APK" in
*"linux-amazon-biscuit-$pkgver-r$pkgrel.apk") ;;
*) die "$APK is not linux-amazon-biscuit-$pkgver-r$pkgrel.apk" ;;
esac
NAME=linux-amazon-biscuit-r$pkgrel-source
ARCHIVE=linux-amazon-biscuit-$_commit.tar.gz

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
B="$T/$NAME"
mkdir -p "$B/linux-amazon-biscuit" "$OUT"

# The upstream archive and prepare-tree.sh from the upstream bundle.
tar xzf "$UPSTREAM" -C "$T" --wildcards "*/$ARCHIVE" "*/prepare-tree.sh"
mv "$T"/linux-amazon-biscuit-r243-source/"$ARCHIVE" "$B/"
sed "s/linux-amazon-biscuit r243 kernel/linux-amazon-biscuit r$pkgrel kernel/" \
	"$T"/linux-amazon-biscuit-r243-source/prepare-tree.sh > "$B/prepare-tree.sh"
[ "$(sha512sum "$B/$ARCHIVE" | cut -d' ' -f1)" = \
  "$(sed -n "s/^\([0-9a-f]\{128\}\)  $ARCHIVE\$/\1/p" "$PKG/APKBUILD")" ] ||
	die "$ARCHIVE does not match the APKBUILD's sha512"

# The package directory: every local file the APKBUILD's source= names.
cp "$PKG/APKBUILD" "$B/linux-amazon-biscuit/"
for f in $(sed -n '/^source="/,/^"/p' "$PKG/APKBUILD" | tr -d ' \t"' |
	   sed "s/\$pkgname/linux-amazon-biscuit/" | grep -v -e '::' -e '^source=' -e '^$'); do
	cp "$PKG/$f" "$B/linux-amazon-biscuit/"
done
NPATCH=$(ls "$B/linux-amazon-biscuit"/*.patch | wc -l)

# The configuration, read back out of the built kernel.
tar xzf "$APK" -C "$T" boot/vmlinuz 2>/dev/null
python3 - "$T/boot/vmlinuz" "$B/kernel-config-r$pkgrel" <<'EOF'
import gzip, sys, zlib
d = open(sys.argv[1], "rb").read()
if d[:2] == b"\x1f\x8b":
    d = gzip.decompress(d)
i = d.find(b"IKCFG_ST")
if i < 0:
    sys.exit("no embedded configuration (CONFIG_IKCONFIG) in the kernel")
open(sys.argv[2], "wb").write(zlib.decompressobj(31).decompress(d[i + 8:]))
EOF

cat > "$B/README.txt" <<EOF
linux-amazon-biscuit r$pkgrel - complete kernel source
====================================================

This is the complete corresponding source for the kernel package
linux-amazon-biscuit $pkgver-r$pkgrel, the kernel for the Amazon Echo Dot
(2nd generation, "biscuit") postmarketOS port, as built by the jonasradke
fork. The binary r$pkgrel kernel in the fork's package feed and in the
release's install zip were built from exactly this source.

Licence: GPL-2.0-only (the kernel, the patches and the MT6625L driver).
Project: https://github.com/jonasradke/pmaports-biscuit
         (device/testing/linux-amazon-biscuit at the release's tag)


Contents
--------

$ARCHIVE
    The upstream kernel: GitHub's archive of bengris32/linux-mtk at commit
    $_commit, a Linux 7.0-rc6 tree with MT8163 support, as carried by the
    upstream project's r243 source bundle (GitHub does not guarantee archive
    tarballs stay byte-identical). It is the file the APKBUILD downloads,
    with the sha512 the APKBUILD names.

linux-amazon-biscuit/
    The kernel package directory: the APKBUILD (the build recipe), the
    $NPATCH patches applied in its source= order, the MT6625L Wi-Fi driver
    (mt6625l-wlan-*.tar.gz: MediaTek's driver from Amazon's GPL source
    release for the Echo Dot, ported to this kernel) and the package's
    install hooks.

kernel-config-r$pkgrel
    The configuration r$pkgrel was built with, read out of the published
    kernel (CONFIG_IKCONFIG; on a running device /proc/config.gz).

prepare-tree.sh
    Reproduces the source tree: extracts the upstream archive and the
    driver, applies the patches in source= order with GNU patch -p1 as
    abuild does, and makes the APKBUILD's prepare() changes.

SHA256SUMS
    Checksums of every file here.


Reproducing and building
------------------------

    sha256sum -c SHA256SUMS
    sh prepare-tree.sh [output-directory]
    mkdir out && cp kernel-config-r$pkgrel out/.config
    make O=out ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- olddefconfig
    make O=out ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- \\
        KCFLAGS=-Wno-error=incompatible-pointer-types
    make O=out ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- \\
        KCFLAGS=-Wno-error=incompatible-pointer-types \\
        mediatek/mt8163-amazon-biscuit.dtb

The release binary was built by pmbootstrap on the APKBUILD (arch aarch64)
in GitHub Actions. The upstream project's r243 tree is also in git, at
https://github.com/liamtw22/linux-mtk branch biscuit-r243; this source is
that tree plus the patches the fork added after 0124.
EOF

( cd "$B" && find . -type f ! -name SHA256SUMS | sed 's#^\./##' | LC_ALL=C sort |
	xargs sha256sum > SHA256SUMS )
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime=@0 \
	-C "$T" -czf "$OUT/$NAME.tar.gz" "$NAME"
echo "$OUT/$NAME.tar.gz"
