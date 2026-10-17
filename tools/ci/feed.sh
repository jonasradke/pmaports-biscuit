#!/bin/sh
# Turn the packages tools/ci/build.sh collected into this fork's signed apk
# feed, laid out for GitHub Pages:
#
#   $SITE/edge/aarch64/*.apk, APKINDEX.tar.gz   what apk reads
#   $SITE/biscuit-apk-jr.rsa.pub                 the key the Echo verifies with
#   $SITE/index.html                             how to add the feed by hand
#
# The private key comes from $APK_SIGNING_KEY (PEM text; the repository secret
# of the same name). The index is built and signed in an Alpine container with
# Alpine's own apk and abuild-sign, the tools apk on the Echo expects.
#
# Usage: APK_SIGNING_KEY=... tools/ci/feed.sh <apk dir> <site dir>
set -eu

SRC=${1:?usage: feed.sh <apk dir> <site dir>}
SITE=${2:?usage: feed.sh <apk dir> <site dir>}
REPO=$(cd "$(dirname "$0")/../.." && pwd)
KEYNAME=biscuit-apk-jr.rsa
PUB="$REPO/device/testing/device-amazon-biscuit/$KEYNAME.pub"
FEED_URL=https://jonasradke.github.io/pmaports-biscuit
ARCH=aarch64

die() { echo "feed.sh: $*" >&2; exit 1; }
[ -n "${APK_SIGNING_KEY:-}" ] || die "APK_SIGNING_KEY is not set"
ls "$SRC"/*.apk >/dev/null 2>&1 || die "no packages in $SRC"

KEYDIR=$(mktemp -d)
trap 'rm -rf "$KEYDIR"' EXIT
printf '%s\n' "$APK_SIGNING_KEY" > "$KEYDIR/$KEYNAME"
chmod 600 "$KEYDIR/$KEYNAME"
cp "$PUB" "$KEYDIR/$KEYNAME.pub"
# The secret must be the private half of the key the package ships, or every
# Echo would refuse the feed.
openssl rsa -in "$KEYDIR/$KEYNAME" -pubout 2>/dev/null | cmp -s - "$PUB" ||
	die "APK_SIGNING_KEY does not match $PUB"

rm -rf "$SITE"
mkdir -p "$SITE/edge/$ARCH"
SITE=$(cd "$SITE" && pwd)     # docker -v takes a relative path as a volume name
cp "$SRC"/*.apk "$SITE/edge/$ARCH/"
cp "$PUB" "$SITE/"
: > "$SITE/.nojekyll"

# apk-tools 2's index format, which apk-tools 3 on the Echo still reads.
docker run --rm -v "$SITE/edge/$ARCH:/feed" -v "$KEYDIR:/keys:ro" alpine:3.21 sh -ec '
	apk add -q abuild apk-tools >/dev/null
	cd /feed
	apk index -q --allow-untrusted --rewrite-arch '"$ARCH"' -o APKINDEX.tar.gz *.apk
	abuild-sign -q -k /keys/'"$KEYNAME"' -p '"$KEYNAME"'.pub APKINDEX.tar.gz
	chown -R '"$(id -u):$(id -g)"' /feed'
tar tzf "$SITE/edge/$ARCH/APKINDEX.tar.gz" | grep -qx ".SIGN.RSA.$KEYNAME.pub" ||
	die "APKINDEX.tar.gz is not signed with $KEYNAME"

list=""
for f in "$SITE/edge/$ARCH"/*.apk; do
	n=$(basename "$f")
	list="$list<a href=\"edge/$ARCH/$n\">edge/$ARCH/$n</a>
"
done
cat > "$SITE/index.html" <<HTML
<!doctype html>
<meta charset="utf-8">
<title>biscuit apk feed (jonasradke fork)</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root { color-scheme: light dark; }
  body { margin: 0 auto; padding: 2.5rem 1rem; max-width: 44rem;
         font: 16px/1.6 ui-sans-serif, -apple-system, Segoe UI, Roboto, sans-serif; }
  pre { border: 1px solid #8884; border-radius: 8px; padding: .85rem 1rem; overflow-x: auto; }
</style>
<h1>biscuit apk feed</h1>
<p>Signed packages of the <a href="https://github.com/jonasradke/pmaports-biscuit">jonasradke fork</a>
of the postmarketOS port for the Amazon Echo Dot 2 (<code>biscuit</code>). An Echo installed from
one of the fork's releases already has this feed and its key.</p>
<h2>Add it by hand</h2>
<pre><code>wget -O /etc/apk/keys/$KEYNAME.pub $FEED_URL/$KEYNAME.pub
echo '$FEED_URL/edge' &gt;&gt; /etc/apk/repositories
apk update</code></pre>
<h2>Contents</h2>
<pre><code>$list</code></pre>
HTML
echo "feed ready in $SITE"
