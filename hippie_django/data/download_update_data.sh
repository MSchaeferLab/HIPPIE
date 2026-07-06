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

# Emit one TSV row per RUNTIME source (fetched live by a Python command, not stored here):
# key<TAB>url. These are only HEAD-probed so their version/Last-Modified can be recorded.
emit_runtime_rows() {
  "$PY" - "$CONFIG" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
for key, s in cfg["sources"].items():
    if s.get("runtime") and s.get("url"):
        print("\t".join([key, s["url"]]))
PY
}

# Release-notes file whose first line is the current UniProt release id.
UNIPROT_RELNOTES="https://ftp.uniprot.org/pub/databases/uniprot/relnotes.txt"

# Stamp fetched date + Last-Modified + ETag (parsed from a saved header file) back into
# sources.json for the given key. If "track_extracted" is set, also record the real name
# the archive/file expanded into (e.g. BIOGRID-ALL-5.0.259.mitab.txt) as "filename".
#
# "version" is derived by the first rule that yields a value (else it falls through):
#   1. UniProt URL      -> the passed UniProt release id (relnotes.txt line 1)
#   2. version_file_line -> that 1-indexed line of the file, verbatim (e.g. RGD line 2
#                           "# MODULE: genes  build 2024-06-24")
#   3. track_extracted + version_regex -> capture from the extracted filename (e.g. BioGRID)
#   4. DEFAULT           -> the upstream Last-Modified date (also stored in "last_modified")
# Sources with "version_pinned": true keep their hand-set "version" (no default applied).
record_meta() {
  key="$1"; header_file="$2"; extracted="${3:-}"; uniprot_version="${4:-}"
  "$PY" - "$CONFIG" "$key" "$header_file" "$extracted" "$uniprot_version" <<'PY'
import json, os, re, sys, tempfile, urllib.request
from datetime import date
cfg_path, key, header_file = sys.argv[1], sys.argv[2], sys.argv[3]
extracted = sys.argv[4] if len(sys.argv) > 4 else ""
uniprot_version = sys.argv[5] if len(sys.argv) > 5 else ""
UNIPROT_PREFIX = "https://ftp.uniprot.org/pub/databases/uniprot"
data_dir = os.path.dirname(os.path.abspath(cfg_path))

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

def head_text(entry, url, nbytes=8192):
    """First bytes of the source file: the local copy if present, else fetched."""
    fn = entry.get("filename")
    if fn and os.path.exists(os.path.join(data_dir, fn)):
        with open(os.path.join(data_dir, fn), "r", errors="replace") as fh:
            return fh.read(nbytes)
    if url:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "hippie-downloader/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read(nbytes).decode("utf-8", "replace")
        except Exception:
            return ""
    return ""

doc = json.load(open(cfg_path))
e = doc["sources"][key]
url = e.get("url") or e.get("url_dir") or ""
e["fetched"] = date.today().isoformat()
if last_mod is not None:
    e["last_modified"] = last_mod
if etag is not None:
    e["etag"] = etag
if extracted and e.get("track_extracted"):
    e["filename"] = extracted

version = None
if uniprot_version and url.startswith(UNIPROT_PREFIX):
    version = uniprot_version
if version is None and e.get("version_file_template"):
    # Build the version from the file's own header lines. "{n}" in the template is
    # replaced by the LAST whitespace token of the file's n-th (1-indexed) line — the
    # header dates sit at the end of those lines. E.g. RGD "build {2}.generated{3}" ->
    # "build 2024-06-24.generated2026/06/27".
    lines = head_text(e, url).splitlines()
    def _last_token(i):
        i = int(i)
        parts = lines[i - 1].split() if 1 <= i <= len(lines) else []
        return parts[-1] if parts else ""
    version = re.sub(r"\{(\d+)\}", lambda m: _last_token(m.group(1)), e["version_file_template"]).strip()
if version is None and e.get("track_extracted") and e.get("version_regex") and extracted:
    m = re.search(e["version_regex"], extracted)
    version = m.group(1) if m else None
if version is not None:
    e["version"] = version
elif not e.get("version_pinned") and last_mod:
    e["version"] = last_mod          # DEFAULT: version follows Last-Modified

fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(cfg_path)), suffix=".json")
with os.fdopen(fd, "w") as f:
    json.dump(doc, f, indent=2, ensure_ascii=False)
    f.write("\n")
os.replace(tmp, cfg_path)
PY
}

# basename of a URL (the name curl -O would save it under)
url_basename() { echo "${1##*/}"; }

# Print sources.json[key][field] (empty string if absent/null). Used to read the stored
# ETag so we can send a conditional request and skip unchanged downloads.
config_field() {
  "$PY" - "$CONFIG" "$1" "$2" <<'PY'
import json, sys
e = json.load(open(sys.argv[1]))["sources"].get(sys.argv[2], {})
v = e.get(sys.argv[3])
print(v if v is not None else "")
PY
}

# Current UniProt release id = first line of relnotes.txt (e.g. "UniProt Release 2026_03").
# Fetched once; empty if unreachable (then existing versions are left untouched).
UNIPROT_VERSION="$(curl -fsSL --max-time 30 "$UNIPROT_RELNOTES" 2>/dev/null | head -n1 | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' || true)"
[ -n "$UNIPROT_VERSION" ] && echo "UniProt release: $UNIPROT_VERSION"

emit_rows | while IFS="$(printf '\t')" read -r key url filename decompress; do
  artifact="$(url_basename "$url")"
  headers="$(mktemp)"
  echo "==> $key: $url"

  # Conditional GET: only re-download if the file changed. If we still have the extracted
  # output and a stored ETag, send If-None-Match (primary) + If-Modified-Since (fallback);
  # otherwise fall back to a date check, or an unconditional GET when the output is missing.
  # Positional params ("set --") build the optional flags without needing bash arrays.
  set --
  if [ -e "$filename" ]; then
    set -- "$@" -z "$filename"
    stored_etag="$(config_field "$key" etag)"
    [ -n "$stored_etag" ] && set -- "$@" -H "If-None-Match: $stored_etag"
  fi
  # No -f here: we branch on the HTTP status ourselves (200 download / 304 skip / else warn).
  http="$(curl -sSL "$@" -o "$artifact" -D "$headers" -w '%{http_code}' "$url" || echo 000)"

  case "$http" in
    304)
      echo "  unchanged (ETag/date match) — skipping download"
      ;;
    2??)
      extracted=""
      case "$decompress" in
        gz)
          gunzip -f "$artifact"
          extracted="${artifact%.gz}"   # gunzip drops the .gz suffix
          ;;
        zip)
          unzip -o "$artifact"
          # the archive's real inner data file (versioned names like BIOGRID-ALL-5.0.258.mitab.txt)
          extracted="$(unzip -Z1 "$artifact" 2>/dev/null | grep -Ei '\.(mitab\.txt|txt|tsv|dat|gct)$' | head -1)"
          rm -f "$artifact"
          ;;
        none) : ;;
        *) echo "WARN: unknown decompress '$decompress' for $key" >&2 ;;
      esac
      record_meta "$key" "$headers" "$extracted" "$UNIPROT_VERSION"
      ;;
    *)
      echo "WARN: download failed for $key ($url) — HTTP $http" >&2
      ;;
  esac
  rm -f "$headers"
done

# Runtime sources (fetched live by a Python command) are not downloaded here, but we
# HEAD-probe them so their version/Last-Modified stays current in sources.json.
emit_runtime_rows | while IFS="$(printf '\t')" read -r key url; do
  headers="$(mktemp)"
  echo "==> [meta] $key: $url"
  if curl -fsSL -I -L --max-time 30 -o /dev/null -D "$headers" "$url"; then
    record_meta "$key" "$headers" "" "$UNIPROT_VERSION"
  else
    echo "WARN: HEAD probe failed for $key ($url) — version left unchanged" >&2
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
