#!/usr/bin/env bash
#
# Build the VI Package from source/.vipb and compare the result against the
# released reference fixture. This is the inner loop for iterating on the .vipb
# and packaging until the build reaches structural parity with the last release.
#
# The build's LabVIEW version is auto-detected from the .vipb (currently 2020),
# and the output .vip lands in the repo root (per the .vipb Library_Output_Folder).
#
# Usage:
#   tools/build_and_compare.sh [extra args forwarded to the comparator]
#
# Examples:
#   tools/build_and_compare.sh                 # build, then PASS/FAIL summary
#   tools/build_and_compare.sh --verbose       # show full values and diffs
#   tools/build_and_compare.sh --json          # machine-readable report
#   BASELINE=path/to/other.vip tools/build_and_compare.sh
#
# Exit code is the comparator's: 0 = structural parity, 1 = drift, 2 = error.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
baseline="${BASELINE:-$repo_root/tests/fixtures/oglib_error-6.0.0.26.vip}"
vipb="$repo_root/source/.vipb"

if [[ ! -f "$baseline" ]]; then
  echo "error: baseline not found: $baseline" >&2
  exit 2
fi

# vipm build aborts if its output file already exists, so clear any prior build
# of this package first. These are gitignored artifacts in the output folder
# (where the .vipb writes), never source. Scope the glob to the package name so
# unrelated .vip files are left alone.
pkg_name="$(sed -n 's:.*<Package_File_Name>\(.*\)</Package_File_Name>.*:\1:p' "$vipb" | head -n1)"
pkg_name="${pkg_name:-oglib_error}"
rm -f "$repo_root/$pkg_name"-*.vip

echo ">> Building $vipb"
echo "   (LabVIEW version auto-detected from the build spec; output -> $repo_root)"
VIPM_DESKTOP_LIVELINESS_TIMEOUT="${VIPM_DESKTOP_LIVELINESS_TIMEOUT:-300}" \
  vipm build "$vipb" --timeout "${VIPM_BUILD_TIMEOUT:-900}"

# The just-built package is the newest matching .vip in the output folder.
candidate="$(ls -t "$repo_root/$pkg_name"-*.vip 2>/dev/null | head -n1 || true)"
if [[ -z "$candidate" ]]; then
  echo "error: build produced no .vip in $repo_root" >&2
  exit 2
fi

echo
echo ">> Built:    $candidate"
echo ">> Baseline: $baseline"
echo
exec uv run python "$repo_root/tools/compare_package_structure.py" \
  "$baseline" "$candidate" "$@"
