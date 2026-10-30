#!/usr/bin/env bash
# Apply a maildir-cleanup deletion list to a Maildir tree (the clone or the live server).
#
#   apply-list.sh ROOT LIST                 dry run: report what resolves
#   apply-list.sh ROOT LIST --move DIR      move files to DIR, keeping relative paths (undoable)
#   apply-list.sh ROOT LIST --rm            delete files
#
# LIST is either out/delete-*.txt (one path per line, relative to ROOT) or
# out/delete-*.keys.txt (folder<TAB>key). The keys form finds a message even if
# its flags or cur/new location changed since the clone was scanned.
set -euo pipefail

usage() { sed -n '2,10p' "$0" >&2; exit 2; }
[ $# -ge 2 ] || usage
root=${1%/}; list=$2; mode=dry; dest=
case "${3:-}" in
  "") ;;
  --rm) mode=rm ;;
  --move) [ -n "${4:-}" ] || usage; mode=move; dest=${4%/} ;;
  *) usage ;;
esac
[ -d "$root" ] || { echo "no such directory: $root" >&2; exit 1; }

found=0 missing=0 bytes=0
shopt -s nullglob
while IFS= read -r line || [ -n "$line" ]; do
  [ -n "$line" ] || continue
  if [[ "$line" == *$'\t'* ]]; then
    dir=${line%%$'\t'*}; key=${line#*$'\t'}
    matches=("$root/$dir"/cur/"$key" "$root/$dir"/cur/"$key":* "$root/$dir"/cur/"$key"\;* "$root/$dir"/cur/"$key"\!* \
             "$root/$dir"/new/"$key" "$root/$dir"/new/"$key":*)
  else
    matches=("$root/$line")
  fi
  existing=()
  for f in "${matches[@]}"; do [ -e "$f" ] && existing+=("$f"); done
  matches=("${existing[@]}")
  if [ ${#matches[@]} -eq 0 ]; then
    missing=$((missing + 1)); continue
  fi
  for f in "${matches[@]}"; do
    found=$((found + 1))
    bytes=$((bytes + $(stat -c %s "$f")))
    case $mode in
      rm) rm -f -- "$f" ;;
      move) rel=${f#"$root"/}; mkdir -p -- "$dest/$(dirname -- "$rel")"; mv -n -- "$f" "$dest/$rel" ;;
    esac
  done
done < "$list"

echo "$mode: $found files ($((bytes / 1048576)) MiB) resolved, $missing not found"
