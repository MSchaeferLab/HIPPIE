#!/usr/bin/env bash
set -euo pipefail

# Always operate from the script's own directory (the project's data/ folder) so this
# works from any CWD:
#   local:  cd hippie_django && sh data/download_update_data.sh
#   Docker: docker compose exec web sh data/download_update_data.sh
cd "$(dirname "$0")"

CONFIG="sources.json"
[ -f "$CONFIG" ] || { echo "ERROR: $CONFIG not found in $(pwd)" >&2; exit 1; }

PY="$(command -v python3 || command -v python)"
[ -n "$PY" ] || { echo "ERROR: python3 not found" >&2; exit 1; }

# Emit one TSV row per download-managed source: key<TAB>url<TAB>filename<TAB>decompress
# Skips manual (browser-only), runtime (fetched live by a Python command) and local
# (no upstream URL) sources.
emit_rows() {
  "$PY" - "$CONFIG" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
for key, s in cfg["sources"].items():
    if s.get("manual") or s.get("runtime") or s.get("local"):
        continue
    url = s.get("url")
    if not url:
        continue
    print("\t".join([key, url, s["filename"], s.get("decompress", "none")]))
PY
}

# Stamp fetched date + Last-Modified + ETag (parsed from a saved header file) back into
# sources.json for the given key. For sources flagged "track_extracted", also record the
# real name the archive expanded into (e.g. BIOGRID-ALL-5.0.258.mitab.txt) as "filename"
# and, if "version_regex" is set, capture the version out of that name.
record_meta() {
  key="$1"; header_file="$2"; extracted="${3:-}"
  "$PY" - "$CONFIG" "$key" "$header_file" "$extracted" <<'PY'
import json, os, re, sys, tempfile
from datetime import date
cfg_path, key, header_file = sys.argv[1], sys.argv[2], sys.argv[3]
extracted = sys.argv[4] if len(sys.argv) > 4 else ""
last_mod = etag = None
try:
    with open(header_file, "r", errors="replace") as f:
        for line in f:
            low = line.lower()
            if low.startswith("last-modified:"):
                last_mod = line.split(":", 1)[1].strip()
            elif low.startswith("etag:"):
                etag = line.split(":", 1)[1].strip()
except FileNotFoundError:
    pass
doc = json.load(open(cfg_path))
e = doc["sources"][key]
e["fetched"] = date.today().isoformat()
if last_mod is not None:
    e["last_modified"] = last_mod
if etag is not None:
    e["etag"] = etag
if extracted and e.get("track_extracted"):
    e["filename"] = extracted
    rx = e.get("version_regex")
    if rx:
        m = re.search(rx, extracted)
        if m:
            e["version"] = m.group(1)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(cfg_path)), suffix=".json")
with os.fdopen(fd, "w") as f:
    json.dump(doc, f, indent=2, ensure_ascii=False)
    f.write("\n")
os.replace(tmp, cfg_path)
PY
}

# basename of a URL (the name curl -O would save it under)
url_basename() { echo "${1##*/}"; }

emit_rows | while IFS="$(printf '\t')" read -r key url filename decompress; do
  artifact="$(url_basename "$url")"
  headers="$(mktemp)"
  echo "==> $key: $url"
  # -z: conditional GET against the previously downloaded artifact (only fetch if newer)
  if curl -fsSL -z "$artifact" -o "$artifact" -D "$headers" "$url"; then
    extracted=""
    case "$decompress" in
      gz)  gunzip -f "$artifact" ;;
      zip)
        unzip -o "$artifact"
        # the archive's real inner data file (versioned names like BIOGRID-ALL-5.0.258.mitab.txt)
        extracted="$(unzip -Z1 "$artifact" 2>/dev/null | grep -Ei '\.(mitab\.txt|txt|tsv|dat|gct)$' | head -1)"
        rm -f "$artifact"
        ;;
      none) : ;;
      *) echo "WARN: unknown decompress '$decompress' for $key" >&2 ;;
    esac
    record_meta "$key" "$headers" "$extracted"
  else
    echo "WARN: download failed for $key ($url)" >&2
  fi
  rm -f "$headers"
done

# Manual (browser-only) sources: print instructions, one per manual entry.
"$PY" - "$CONFIG" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
for key, s in cfg["sources"].items():
    if not s.get("manual"):
        continue
    print(f"\nMANUAL: {key} must be downloaded in a browser (host blocks scripted downloads),")
    print("        then placed in this data/ folder:")
    print(f"  {s.get('url_dir') or s.get('url')}")
    glob = s.get("filename_glob")
    if glob:
        print(f"  -> any current release matching: {glob}")
        print("     (no config edit needed on a version bump)")
    else:
        print(f"  -> save as: {s['filename']}")
PY
