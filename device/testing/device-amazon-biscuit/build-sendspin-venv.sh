#!/bin/sh
# Rebuild biscuit-sendspin-venv.tar.gz.
#
# WHY THIS EXISTS: the venv shipped as a prebuilt 30 MB tarball with no recipe
# anywhere in this tree. Nobody could reproduce it, audit what was in it, or
# rebuild it after a dependency changed. This script is that recipe, written
# from the shipped venv's own metadata on 2026-09-20.
#
# RUN IT IN AN aarch64 CHROOT, not on the host:
#   pmbootstrap -p <aports> chroot -b aarch64 -- sh /path/to/build-sendspin-venv.sh
# The result is /tmp/biscuit-sendspin-venv.tar.gz, to be copied over the copy
# in this directory and re-checksummed with `pmbootstrap checksum`.
#
# EXPECT IT TO TAKE ABOUT AN HOUR. Building av from source compiles roughly a
# hundred Cython modules under qemu emulation; that single step is most of the
# wall time.
#
# The 2026-10-18 tarball (sendspin 8.0.0) was built natively on an Echo with
# these pins instead, with no chroot to hand: av 18.1.0 and sendspin._volume
# were copied from the installed r300 venv (same versions, same source, same
# Python and FFmpeg), everything else installed from PyPI as below.
set -eu

VENV=${VENV:-/tmp/sendspin-build/sendspin}
OUT=${OUT:-/tmp/biscuit-sendspin-venv.tar.gz}
HERE=$(cd "$(dirname "$0")" && pwd)

# The upstream CLI is pinned in sendspin-venv-requirements.txt (sendspin 8.0.0
# from PyPI; until 2026-10-18 it was a git commit). This is UPSTREAM, not our
# fork: our changes are applied afterwards by the .diff files in the
# APKBUILD's source=, not baked in here. Its one C extension, sendspin._volume,
# needs build-base and python3-dev below; without a compiler setup.py quietly
# falls back to numpy, so check the .so is there.

# tar is GNU tar: busybox tar has no --sort/--owner/--mtime for the
# reproducible archive at the end.
apk add --quiet python3 python3-dev py3-pip build-base pkgconf linux-headers \
	ffmpeg-dev tar gzip

rm -rf "$(dirname "$VENV")"
mkdir -p "$(dirname "$VENV")"
# --system-site-packages, matching the shipped venv: several imports resolve
# to the system python rather than the venv, py3-zeroconf among them.
python3 -m venv --system-site-packages "$VENV"
"$VENV"/bin/pip install --quiet --upgrade pip setuptools wheel

# av FIRST and FROM SOURCE. The PyPI wheel bundles its own FFmpeg built
# --enable-gpl with libx264 and libx265, which would make this package a
# redistributor of GPL binaries with no corresponding source - for three audio
# codecs (flac in, flac out, opus out). Built from source it links the distro
# libraries instead, which Alpine already ships here for mpv. Installing it
# first means a later resolver pass cannot quietly swap the wheel back in.
"$VENV"/bin/pip install --no-binary av "av==18.1.0"

"$VENV"/bin/pip install --prefer-binary -r "$HERE/sendspin-venv-requirements.txt"
# See the requirements file: aiohttp comes from the system, with its multidict.
"$VENV"/bin/pip uninstall -y multidict 2>/dev/null || true
ls "$VENV"/lib/python3.14/site-packages/sendspin/_volume.*.so > /dev/null

# The venv is built here but runs from /usr/lib/sendspin: point the script
# shebangs, activate scripts and pyvenv.cfg there, or bin/sendspin names an
# interpreter that does not exist on the device.
grep -rIl "$VENV" "$VENV" | while read -r f; do
	sed -i "s#$VENV#/usr/lib/sendspin#g" "$f"
done

# Refuse to ship what this whole exercise removed.
if [ -d "$VENV/lib/python3.14/site-packages/av.libs" ]; then
	echo "build-sendspin-venv: av.libs present - the wheel was used, not source" >&2
	exit 1
fi
if find "$VENV" -iname '*x264*' -o -iname '*x265*' | grep -q .; then
	echo "build-sendspin-venv: GPL x264/x265 present, refusing to package" >&2
	exit 1
fi

"$VENV"/bin/python -c 'import av; [av.CodecContext.create(c,m) for c,m in (("flac","r"),("flac","w"),("libopus","w"))]'

# Reproducible archive: sorted names, root-owned with no names stored, one
# fixed mtime and no gzip timestamp, so the same tree always packs to the
# same bytes and nothing about the build machine or its account leaks in.
# abuild resets every packaged file's mtime to SOURCE_DATE_EPOCH anyway, so
# the fixed mtime here changes nothing on the device.
MTIME=${SOURCE_DATE_EPOCH:-1790640000}
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime="@$MTIME" \
	--format=gnu -C "$(dirname "$VENV")" -cf - "$(basename "$VENV")" \
	| gzip -n -9 > "$OUT"
# The pipeline hides a tar failure from set -e; a truncated archive fails here.
tar -tzf "$OUT" > /dev/null
echo "wrote $OUT ($(stat -c%s "$OUT") bytes)"
