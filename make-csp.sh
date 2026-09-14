#!/usr/bin/env bash
# Regenerate the CSP hashes for the inline <script>/<style> blocks.
#
#   ./make-csp.sh          print the directives
#   ./make-csp.sh --write  rewrite _headers in place (run this after ANY edit
#                          to index.html or 404.html, before deploying)
#
# The CSP pins the inline script/style by hash, so an edited file whose hash is
# not refreshed will be refused by the browser and the page will not run.
set -euo pipefail
cd "$(dirname "$0")"
read -r SCRIPT_SRC STYLE_SRC < <(python3 - <<'PY'
import re,hashlib,base64,io,os
scripts,styles=[],[]
for f in ('index.html','404.html'):
    if not os.path.exists(f): continue
    s=io.open(f,encoding='utf-8').read()
    for tag,bucket in (('script',scripts),('style',styles)):
        for m in re.finditer(r'<%s[^>]*>(.*?)</%s>'%(tag,tag), s, re.S):
            v="'sha256-%s'"%base64.b64encode(hashlib.sha256(m.group(1).encode('utf-8')).digest()).decode()
            if v not in bucket: bucket.append(v)
print("'self' "+" ".join(scripts)+"\t"+"'self' "+" ".join(styles))
PY
)
SCRIPT_SRC="${SCRIPT_SRC%%$'\t'*}"
FULL=$(python3 - <<'PY'
import re,hashlib,base64,io,os
scripts,styles=[],[]
for f in ('index.html','404.html'):
    if not os.path.exists(f): continue
    s=io.open(f,encoding='utf-8').read()
    for tag,bucket in (('script',scripts),('style',styles)):
        for m in re.finditer(r'<%s[^>]*>(.*?)</%s>'%(tag,tag), s, re.S):
            v="'sha256-%s'"%base64.b64encode(hashlib.sha256(m.group(1).encode('utf-8')).digest()).decode()
            if v not in bucket: bucket.append(v)
print("script-src 'self' "+" ".join(scripts))
print("style-src 'self' "+" ".join(styles))
PY
)
SC=$(printf '%s\n' "$FULL" | sed -n "1s/^script-src //p")
ST=$(printf '%s\n' "$FULL" | sed -n "2s/^style-src //p")

if [ "${1:-}" = "--write" ]; then
  python3 - "$SC" "$ST" <<'PY'
import sys,io,re
sc,st=sys.argv[1],sys.argv[2]
s=io.open('_headers',encoding='utf-8').read()
new,n=re.subn(r"script-src .*?; style-src .*?; style-src-attr",
              "script-src %s; style-src %s; style-src-attr"%(sc,st), s, count=1, flags=re.S)
if n==0: sys.exit("could not locate script-src/style-src in _headers")
io.open('_headers','w',encoding='utf-8').write(new)
print("_headers updated" if new!=s else "_headers already current")
PY
else
  printf '%s\n' "$FULL"
fi
