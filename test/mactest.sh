#!/bin/bash
# Waystone macOS test on a clean runner: download, sha256, unzip to a path with a space and an accent,
# open the app like a double-click, wait for the AI, ask questions, single-instance check, screenshots.
set -u
WS="$GITHUB_WORKSPACE"; OUT="$WS/out"; mkdir -p "$OUT"
log() { echo "$(date +%H:%M:%S) $*" | tee -a "$OUT/log.txt"; }
ZIP="$RUNNER_TEMP/waystone.zip"
t=$(date +%s); curl -sSL --retry 3 -o "$ZIP" "$ZIP_URL"; log "download $(( $(date +%s) - t )) s, $(stat -f %z "$ZIP") bytes"
SHA=$(shasum -a 256 "$ZIP" | cut -d' ' -f1); log "sha256 $SHA"
if [ -n "$ZIP_SHA" ] && [ "$SHA" != "$ZIP_SHA" ]; then log "SHA MISMATCH"; exit 1; fi
QMARK="0083;$(printf %x $(date +%s));Safari;"
xattr -w com.apple.quarantine "$QMARK" "$ZIP"; log "zip quarantine mark: $(xattr -p com.apple.quarantine "$ZIP")"
DEST="$HOME/Téléchargements/Waystone test"; mkdir -p "$DEST"
t=$(date +%s); ditto -x -k "$ZIP" "$DEST"; log "unzip $(( $(date +%s) - t )) s"
APP=$(find "$DEST" -maxdepth 3 -name "Waystone.app" | head -1); log "app $APP"
# First run as a real buyer: quarantine mark on the app, then what Gatekeeper and the signature seal say (logged, not failed)
xattr -w -r com.apple.quarantine "$QMARK" "$APP" 2>&1 | tail -2
{ echo "--- spctl (ditto copy, quarantined)"; spctl --assess --type execute -vv "$APP" 2>&1; echo "exit $?"
  echo "--- codesign verify (ditto copy)"; codesign --verify --deep --strict -vv "$APP" 2>&1; echo "exit $?"; } | tee -a "$OUT/log.txt"
# Second copy with plain unzip (Keka-like tools): stray ._ files would break the signature seal
DEST2="$RUNNER_TEMP/unzip-copy"; mkdir -p "$DEST2"; unzip -q "$ZIP" -d "$DEST2" 2>&1 | tail -2
APP2=$(find "$DEST2" -maxdepth 3 -name "Waystone.app" | head -1)
log "plain unzip: $(find "$DEST2" -name '._*' | wc -l | tr -d ' ') stray ._ files"
{ echo "--- codesign verify (plain unzip copy)"; codesign --verify --deep --strict -vv "$APP2" 2>&1; echo "exit $?"; } | tee -a "$OUT/log.txt"
rm -rf "$DEST2"; rm "$ZIP"
DATA="$HOME/Library/Application Support/Waystone"; RUN="$DATA/en-cours.json"; rm -f "$RUN"
# The quarantine mark makes Gatekeeper wait for a click on "Open" that never comes on a runner, which hung
# every shard for hours (r23c). The buyer view is logged above; clear the mark, then launch without blocking.
xattr -d -r com.apple.quarantine "$APP" 2>/dev/null; log "quarantine cleared for the automated launch"
T0=$(date +%s); open "$APP" & OPEN_PID=$!
URL=""
for i in $(seq 1 300); do sleep 1; [ -f "$RUN" ] && URL=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['url'])" "$RUN" 2>/dev/null) && [ -n "$URL" ] && break; done
log "interface $URL after $(( $(date +%s) - T0 )) s"
kill "$OPEN_PID" 2>/dev/null
[ -z "$URL" ] && { cp "$DATA/journal.txt" "$OUT/" 2>/dev/null; exit 1; }
python3 - "$URL" "$T0" "$OUT" "$WS/test/questions.json" <<'EOF'
import json, sys, time, urllib.request
url, t0, out, qf = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
def call(p, body=None, timeout=300):
    req = urllib.request.Request(url + p, data=json.dumps(body).encode() if body else None, headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
R = {}
st = {}
for i in range(900):
    try: st = call("api/status", timeout=10)
    except Exception: pass
    if st.get("embed") == "ready" and "embed_ready_s" not in R: R["embed_ready_s"] = round(time.time() - t0, 1)
    if st.get("chat") == "ready" or st.get("error"): break
    time.sleep(1)
R["status"], R["chat_ready_s"] = st, round(time.time() - t0, 1)
R["models_ready"] = st.get("chat") == "ready" and st.get("embed") == "ready"
R["answers"] = []
for q in json.load(open(qf)):
    t = time.time()
    try:
        a = call("api/ask", {"q": q["q"]})
        d = a.get("debug") or {}
        R["answers"].append({"kind": q["kind"], "q": q["q"], "s": round(time.time() - t, 1), "status": a.get("status"), "urgent": a.get("urgent"), "triage": d.get("triage"), "ai_check": d.get("ai_check"), "reason": d.get("reason"), "warning": (a.get("warning") or "")[:400], "message": a.get("message"), "pages": [b.get("card_id") for b in a.get("blocks") or []], "choices": [c.get("title") for c in a.get("choices") or []]})
    except Exception as e:
        R["answers"].append({"kind": q["kind"], "q": q["q"], "error": str(e)})
json.dump(R, open(out + "/result.json", "w"), indent=1, ensure_ascii=False)
EOF
log "questions done"
PID1=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['pid'])" "$RUN")
S1=$(pgrep -f llama-server | wc -l | tr -d ' ')
open -n "$APP"; sleep 30
S2=$(pgrep -f llama-server | wc -l | tr -d ' ')
G=$(pgrep -f "Waystone.app/Contents/MacOS/guide" | wc -l | tr -d ' ')
PID2=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['pid'])" "$RUN")
log "single instance: guide processes $G, model servers $S1 -> $S2, run-file pid $PID1 -> $PID2"
GEN=$(python3 -c "
import json,sys;R=json.load(open(sys.argv[1]));a=[x for x in R['answers'] if x.get('ai_check')=='other_emergency'] or [x for x in R['answers'] if x['kind']=='generic_candidate'];print(a[0]['q'])" "$OUT/result.json")
RULE=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))[1]['q'])" "$WS/test/questions.json")
[ -f "$WS/test/shots-extra.json" ] && export SHOTS_EXTRA="$WS/test/shots-extra.json"
node "$WS/test/shots.js" "$URL" "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" "$OUT" "$RULE" "$GEN" 2>&1 | tee -a "$OUT/log.txt"
curl -s -X POST "${URL}api/quit" >/dev/null; sleep 10
log "after quit: guide processes $(pgrep -f 'Waystone.app/Contents/MacOS/guide' | wc -l | tr -d ' '), model servers $(pgrep -f llama-server | wc -l | tr -d ' ')"
cp "$DATA/journal.txt" "$OUT/journal-main.txt" 2>/dev/null
python3 - "$OUT/result.json" "$G" "$S1" "$S2" "$PID1" "$PID2" <<'EOF'
import json, sys
f, g, s1, s2, p1, p2 = sys.argv[1:]
R = json.load(open(f))
R["single_instance"] = {"guide_processes": int(g), "model_servers_before": int(s1), "model_servers_after": int(s2), "same_pid_in_run_file": p1 == p2}
R["single_instance"]["ok"] = int(g) == 1 and s1 == s2 and p1 == p2
R["pass"] = R["models_ready"] and R["single_instance"]["ok"] and not any("error" in a for a in R["answers"])
json.dump(R, open(f, "w"), indent=1, ensure_ascii=False)
print("PASS:", R["pass"])
EOF
