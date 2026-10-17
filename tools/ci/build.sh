#!/bin/sh
# Build this port with pmbootstrap, as BUILDING.md describes, unattended.
#
#   tools/ci/build.sh packages   bluez-alsa, boot-deploy, the kernel (the device
#                                package depends on it) and the device packages
#   tools/ci/build.sh full       the above, the disk and boot image (cleaned as
#                                in BUILDING.md section 5) and the four TWRP zips
#
# Runs on a Linux x86_64 machine with sudo; GitHub Actions' ubuntu runners are
# what it is written for (.github/workflows/build.yml). Results land in $OUT:
#   $OUT/apk/    every package this tree builds (the feed's contents)
#   $OUT/zips/   the TWRP zips and their SHA256SUMS (full only)
#   $OUT/image/  pmos.img and boot.img (full only)
set -eu

MODE=${1:-packages}
REPO=$(cd "$(dirname "$0")/../.." && pwd)
PMB_DIR=${PMB_DIR:-$HOME/pmbootstrap}
APORTS=${APORTS:-$HOME/pmaports-build}
WORK=${WORK:-$HOME/.local/var/pmbootstrap}
OUT=${OUT:-$REPO/out}

# BUILDING.md pins these; change them there and here together.
PMB_VERSION=3.10.1
PMAPORTS_URL=https://gitlab.postmarketos.org/postmarketOS/pmaports.git
PMAPORTS_BASE=1ab5c4ed75a362067e27a0238fd80612e3454717
IMAGE_PASSWORD=147147      # postmarketOS's documented default, see SECURITY.md

log() { printf '\n==> %s\n' "$*"; }
die() { echo "build.sh: $*" >&2; exit 1; }
pmb() { python3 "$PMB_DIR/pmbootstrap.py" -y -p "$APORTS" "$@"; }

case "$MODE" in packages|full) ;; *) die "usage: $0 packages|full" ;; esac

log "pmbootstrap $PMB_VERSION"
if [ ! -d "$PMB_DIR" ]; then
	git clone -q --depth 1 --branch "$PMB_VERSION" \
		https://gitlab.postmarketos.org/postmarketOS/pmbootstrap.git "$PMB_DIR"
	# boot_size = 128 needs pmbootstrap to accept a /boot below its 512 MiB
	# default (BUILDING.md section 2). Fail loudly if the code moved.
	f="$PMB_DIR/pmb/install/_install.py"
	grep -q 'if int(config.boot_size) >= int(default)' "$f" ||
		die "pmbootstrap's boot_size check moved; update the patch in build.sh"
	sed -i 's/if int(config.boot_size) >= int(default)/if int(config.boot_size) >= 64/' "$f"
fi

log "pmaports at ${PMAPORTS_BASE%"${PMAPORTS_BASE#????????}"} plus base-pmaports/"
if [ ! -d "$APORTS/.git" ]; then
	git clone -q --filter=blob:none --no-checkout "$PMAPORTS_URL" "$APORTS"
	git -C "$APORTS" checkout -q -b biscuit-base "$PMAPORTS_BASE"
	git -C "$APORTS" -c user.name=ci -c user.email=ci@localhost \
		am -q "$REPO"/base-pmaports/*.patch
	# pmbootstrap reads channels.cfg from origin/main, not the working tree.
	git -C "$APORTS" fetch -q --depth 1 origin main
fi
# The overlay is refreshed every run, so a reused tree builds this commit.
rm -rf "$APORTS/device/testing/device-amazon-biscuit" \
	"$APORTS/device/testing/linux-amazon-biscuit" "$APORTS/temp/bluez-alsa"
cp -a "$REPO/device/testing/device-amazon-biscuit" "$APORTS/device/testing/"
cp -a "$REPO/device/testing/linux-amazon-biscuit" "$APORTS/device/testing/"
mkdir -p "$APORTS/temp"
cp -a "$REPO/temp/bluez-alsa" "$APORTS/temp/"

log "pmbootstrap configuration"
CFG=${XDG_CONFIG_HOME:-$HOME/.config}/pmbootstrap_v3.cfg
mkdir -p "$(dirname "$CFG")"
cat > "$CFG" <<EOF
[pmbootstrap]
aports = $APORTS
work = $WORK
device = amazon-biscuit
ui = none
systemd = never
extra_packages = none
boot_size = 128
timezone = Etc/UTC
user = user
jobs = $(nproc)
EOF
# init with every answer defaulted: it takes the values above and creates the
# work directory. Its prompts change between versions; the config is the
# source of truth, and the checks below catch a wrong default. init rewrites
# the file with only the values that differ from pmbootstrap's defaults, so
# the effective values are asked of pmbootstrap rather than grepped.
yes '' 2>/dev/null | python3 "$PMB_DIR/pmbootstrap.py" -p "$APORTS" init > "$OUT.init.log" 2>&1 ||
	{ tail -n 40 "$OUT.init.log"; die "pmbootstrap init failed"; }
for kv in "device=amazon-biscuit" "ui=none" "systemd=never" "boot_size=128" \
		"extra_packages=none" "timezone=Etc/UTC"; do
	got=$(python3 "$PMB_DIR/pmbootstrap.py" -p "$APORTS" config "${kv%%=*}" 2>/dev/null | tail -n 1)
	[ "$got" = "${kv#*=}" ] || {
		tail -n 40 "$OUT.init.log"; cat "$CFG"
		die "pmbootstrap config ${kv%%=*} is '$got', expected '${kv#*=}'"; }
done

PKG="$WORK/packages/edge/aarch64"
version_of() {   # $1 package dir: pkgver-rpkgrel from its APKBUILD
	( . "$1/APKBUILD" >/dev/null 2>&1; echo "$pkgver-r$pkgrel" )
}

build() {   # $1 package, $2.. extra flags
	log "build $1"
	pmb build "$@"
}

# The kernel's source archive. GitHub generates archive downloads on the fly
# and their bytes are not stable: today's download of the pinned commit no
# longer matches the APKBUILD's sha512, though the tree is the same commit.
# The archive the releases were built from is inside the kernel source bundle
# every release carries, so pmbootstrap's download cache is seeded from that.
K_DIR="$REPO/device/testing/linux-amazon-biscuit"
K_V=$(version_of "$K_DIR")
K_ARCHIVE=$(sed -n 's/^\([0-9a-f]\{128\}\)  \(linux-amazon-biscuit-[0-9a-f]\{40\}\.tar\.gz\)$/\2/p' "$K_DIR/APKBUILD")
DISTFILES="$WORK/cache_distfiles"
if [ -n "$K_ARCHIVE" ] && ! sudo test -f "$DISTFILES/$K_ARCHIVE"; then
	log "kernel source archive from the release bundle"
	t=$(mktemp -d)
	gh release download "${KERNEL_SOURCE_RELEASE:-v1.0}" \
		-R "${KERNEL_SOURCE_REPO:-liamtw22/pmaports-biscuit}" -D "$t" \
		-p 'linux-amazon-biscuit-r*-source.tar.gz'
	tar xzf "$t"/linux-amazon-biscuit-r*-source.tar.gz -C "$t" --wildcards "*/$K_ARCHIVE"
	sudo mkdir -p "$DISTFILES"
	sudo cp "$t"/*/"$K_ARCHIVE" "$DISTFILES/"
	rm -rf "$t"
fi

# A built kernel is kept in $KERNEL_CACHE between runs (the workflow caches it
# by the kernel package's files), with the abuild key that signed it: a cold
# kernel build takes most of an hour, and it only changes when its APKBUILD
# or patches do. The key travels with it so apk in the chroots trusts it.
ABUILD_KEYS="$WORK/config_abuild"
if [ -n "${KERNEL_CACHE:-}" ] && [ -f "$KERNEL_CACHE/linux-amazon-biscuit-$K_V.apk" ]; then
	log "kernel $K_V from the cache"
	sudo mkdir -p "$PKG" "$ABUILD_KEYS"
	sudo cp "$KERNEL_CACHE/linux-amazon-biscuit-$K_V.apk" "$PKG/"
	sudo cp -a "$KERNEL_CACHE/config_abuild/." "$ABUILD_KEYS/"
	pmb index
fi

build bluez-alsa --arch aarch64
build boot-deploy --arch aarch64
build linux-amazon-biscuit
build device-amazon-biscuit --arch aarch64

if [ -n "${KERNEL_CACHE:-}" ] && [ ! -f "$KERNEL_CACHE/linux-amazon-biscuit-$K_V.apk" ]; then
	mkdir -p "$KERNEL_CACHE/config_abuild"
	sudo cp "$PKG/linux-amazon-biscuit-$K_V.apk" "$KERNEL_CACHE/"
	sudo cp -a "$ABUILD_KEYS/." "$KERNEL_CACHE/config_abuild/"
	sudo chown -R "$(id -u):$(id -g)" "$KERNEL_CACHE"
fi

# pmbootstrap exits 0 even when it built something else (BUILDING.md section
# 1): check every package carries the version this tree says.
DEV_V=$(version_of "$REPO/device/testing/device-amazon-biscuit")
ls "$PKG/device-amazon-biscuit-$DEV_V.apk" >/dev/null 2>&1 ||
	die "no device-amazon-biscuit-$DEV_V.apk in $PKG"

log "collect packages"
mkdir -p "$OUT/apk"
for p in device-amazon-biscuit device-amazon-biscuit-voice device-amazon-biscuit-sendspin \
		device-amazon-biscuit-pulseaudio device-amazon-biscuit-nonfree-firmware; do
	cp "$PKG/$p-$DEV_V.apk" "$OUT/apk/"
done
BA_V=$(version_of "$REPO/temp/bluez-alsa")
for p in bluez-alsa bluez-alsa-openrc bluez-alsa-utils; do
	cp "$PKG/$p-$BA_V.apk" "$OUT/apk/"
done
BD_V=$(version_of "$APORTS/main/boot-deploy")
cp "$PKG/boot-deploy-$BD_V.apk" "$OUT/apk/"
sudo cp "$PKG/linux-amazon-biscuit-$K_V.apk" "$OUT/apk/"
sudo chown "$(id -u):$(id -g)" "$OUT"/apk/*.apk
ls -l "$OUT/apk"

[ "$MODE" = full ] || exit 0

log "disk image"
pmb zap
pmb install --password "$IMAGE_PASSWORD"
pmb export
mkdir -p "$OUT/image"
cp -L /tmp/postmarketOS-export/amazon-biscuit.img "$OUT/image/pmos.img"
cp -L /tmp/postmarketOS-export/boot.img "$OUT/image/boot.img"

log "clean the image (BUILDING.md section 5)"
LOOP=$(sudo losetup --find --show --partscan "$OUT/image/pmos.img")
MNT=$(mktemp -d)
sudo mount "${LOOP}p2" "$MNT"
grep -q "release=" "$MNT/usr/share/biscuit/release" ||
	die "the image has no /usr/share/biscuit/release"
cat "$MNT/usr/share/biscuit/release"
sudo truncate -s 0 "$MNT/var/log/apk.log"
sudo rm -f "$MNT/etc/machine-id"
sudo rm -rf "$MNT"/var/cache/apk/*
printf '# Written by udhcpc when the network comes up.\n' | sudo tee "$MNT/etc/resolv.conf" >/dev/null
[ ! -e "$MNT/usr/share/biscuit/speaker.fir" ] || die "speaker.fir must not ship"
sudo umount "$MNT"
sudo zerofree "${LOOP}p2"
sudo zerofree "${LOOP}p1"
sudo losetup -d "$LOOP"
n=$(LC_ALL=C grep -a -c "$HOME" "$OUT/image/pmos.img" || true)
[ "$n" = 0 ] || die "the image still names the build machine's home directory ($n times)"

log "TWRP zips"
python3 "$REPO/installer/zip/build_zips.py" --image "$OUT/image/pmos.img" \
	--boot "$OUT/image/boot.img" --out "$OUT/zips"
ls -l "$OUT/zips"
