#!/usr/bin/env bash
# Assembles the interactive courses from their source parts into static/.
#
# Each course page is a concatenation of _base.html, its modules in filename
# order, and _footer.html. Shared CSS/JS is copied once to
# static/course-assets/ and referenced by both courses.
#
# Run from the docusaurus/ directory:  bash course-src/build.sh
# The Docusaurus build runs this automatically via the prebuild npm script.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$SRC/../static"

# Shared assets, copied once rather than duplicated per course.
mkdir -p "$OUT/course-assets"
cp "$SRC/shared/styles.css" "$SRC/shared/main.js" "$OUT/course-assets/"

# course_dir -> output_dir
build_course() {
  local name="$1" dest="$2"
  mkdir -p "$OUT/$dest"
  cat "$SRC/$name/_base.html" \
      "$SRC/$name"/modules/*.html \
      "$SRC/$name/_footer.html" > "$OUT/$dest/index.html"
  echo "  built $dest/index.html"
}

echo "Building courses..."
build_course fundamentals course
build_course advanced      course-advanced
echo "Done."
