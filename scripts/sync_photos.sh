#!/bin/sh
# Copy new photos from the Desktop folder into photos/ (top level only; subfolders such
# as Cutouts are ignored). Read-only on the source; never overwrites a file already here.
set -eu
SRC="${1:-$HOME/Desktop/Mazoud/Photos}"
DEST="$(cd "$(dirname "$0")/.." && pwd)/photos"
added=0
for f in "$SRC"/*; do
  [ -f "$f" ] && [ ! -L "$f" ] || continue
  name=$(basename "$f")
  echo "$name" | grep -Eiq '^Mazoud_[0-9]+\.jpe?g$' || { echo "skip (name): $name"; continue; }
  if [ -e "$DEST/$name" ]; then
    cmp -s "$f" "$DEST/$name" || echo "WARNING: $name differs from the copy already in photos/ (left unchanged)"
    continue
  fi
  cp -p "$f" "$DEST/$name" && cmp -s "$f" "$DEST/$name" && echo "added: $name" && added=$((added+1))
done
echo "$added new photo(s). Commit and push photos/ to add them to the rotation."
