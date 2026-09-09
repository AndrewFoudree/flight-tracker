#!/usr/bin/env bash
# Stage dashboard/ and the data it reads into _site/ for GitHub Pages.
#
# This exists because it used to live inline in three workflows -- publish,
# check-prices and the coverage probe -- and they drifted. fare_baseline.json was
# added to one of them, so the panel deployed correctly on a manual publish and
# would have vanished again on the next weekly run.
#
# That failure is silent by construction: every optional panel renders only if
# its file loads, so a file missing here is an absent panel and a green build,
# never an error. Adding a panel means adding its file to the list below, once.
set -euo pipefail

sha="${1:-${GITHUB_SHA:-dev}}"

mkdir -p _site/data
cp dashboard/* _site/

# GitHub Pages serves assets with max-age=600, so a deploy is invisible for ten
# minutes unless the URL changes. Stamping the commit onto the script src makes
# every publish take effect on the next load instead of leaving people hard
# refreshing to see their own data.
sed -i "s|src=\"chart.js\"|src=\"chart.js?v=${sha}\"|" _site/index.html

# Required: without these the page has nothing to draw and says so loudly.
cp data/prices.csv data/routes.json _site/data/

# Optional, and separate so a missing one never masks a missing price file.
# runs.csv does not exist until the first run records an outcome, and the two
# hand-run panels not until someone runs them.
for optional in runs.csv source_probe.json fare_baseline.json; do
  cp "data/${optional}" _site/data/ 2>/dev/null || true
done
