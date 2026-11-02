#!/bin/sh
# Write a GitHub release's notes: the release's own section of CHANGELOG.md,
# then how to install it and what it was built from.
#
#     release-notes.sh <release> <build> <sha> <owner/repo> <run id>
#
# The section is everything under "## <release> " up to the next "## "
# heading; the heading itself is left out, since GitHub shows the release's
# name as its title. A release with no section fails here, before anything is
# published: notes that only say how to install are what every release had
# until v1.3-jr, and they told nobody what had changed.
set -eu

RELEASE=$1
BUILD=$2
SHA=$3
REPO=$4
RUN=$5
HERE=$(cd "$(dirname "$0")" && pwd)
CHANGELOG=${CHANGELOG:-$HERE/../../CHANGELOG.md}

changes=$(awk -v rel="## $RELEASE " '
	/^## / { if (p) exit; if (index($0, rel) == 1) { p = 1; next } }
	p' "$CHANGELOG")
[ -n "$(printf '%s' "$changes" | tr -d '[:space:]')" ] || {
	echo "release-notes.sh: no \"## $RELEASE \" section in CHANGELOG.md" >&2
	exit 1
}

printf '%s\n' "$changes"
cat <<EOF

### Installing

A new Echo is installed with the zips below, as described in
[INSTALL.md](https://github.com/$REPO/blob/$RELEASE/installer/INSTALL.md). An
installed Echo updates itself from the fork's feed,
https://jonasradke.github.io/pmaports-biscuit/edge, on its settings page
under Updates.

Build $BUILD, built by GitHub Actions from \`$SHA\`
([run](https://github.com/$REPO/actions/runs/$RUN)). Every change is in
[CHANGELOG.md](https://github.com/$REPO/blob/$RELEASE/CHANGELOG.md).
EOF
