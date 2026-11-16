"""Scenario coverage test (r23d, 16 Nov 2026). Cheap: no AI grader.
python3 -I tools/scenario_coverage.py <scenarios.json> [library-en.json] [out.json]
scenarios: list of {id, q, topic, emergency, want}; want = list of acceptable kind ids (first = best),
"MISSING" (no kind exists for this real emergency) or [] for a non-emergency.
Runs every message through the real engine's word rules via the phone CLI (same Go engine and rules as the laptop)
and reports: right, close (an acceptable kind fired but not first), WRONG commit, miss, MISSING kind, false alarm."""
import json, sys, subprocess, collections, os
R = "/Users/marko/Documents/Codex/2026-10-22/do-u-see-my-surival-chat"
sc = json.load(open(sys.argv[1]))
libp = sys.argv[2] if len(sys.argv) > 2 else R + "/r16-tree/build/library-en.json"
out = sys.argv[3] if len(sys.argv) > 3 else sys.argv[1].replace(".json", ".coverage.json")
LE = json.load(open(libp))
L = json.load(open(R + "/phone-r1/build/library-phone.json"))
have = {c["id"] for c in L["cards"]}
# the phone CLI has a fixed-size page index: keep its own pages; only the rules (triage) come from the laptop library
L["triage"] = LE["triage"]
fam = {k["id"]: (k.get("group") or k["id"]) for k in LE["triage"]["kinds"]}
lib = out + ".lib.json"; json.dump(L, open(lib, "w"))
json.dump([{"id": s["id"], "q": s["q"]} for s in sc], open(out + ".in.json", "w"))
subprocess.run([R + "/phone-r1/build/phone-cli", lib, "set", out + ".in.json", out + ".raw.json"], check=True, capture_output=True)
res = {x["id"]: x["r"] for x in json.load(open(out + ".raw.json"))}
oc = collections.Counter(); rows = []
for s in sc:
    r = res.get(s["id"], {}); got = (r.get("debug") or {}).get("triage") or []
    ch = [c["kind"] for c in (r.get("choose") or [])]
    want = s.get("want")
    if not s.get("emergency"):
        o = "false alarm" if got and r.get("urgent") else "calm ok"
    elif want == "MISSING" or want == ["MISSING"]:
        o = "MISSING kind"
    elif not got and not ch:
        o = "miss"
    else:
        wf = {fam.get(w, w) for w in want}
        first = got[0] if got else None
        if first and fam.get(first) in wf: o = "right"
        elif any(fam.get(g) in wf for g in got) or any(fam.get(c) in wf for c in ch): o = "close"
        else: o = "WRONG"
    oc[o] += 1
    rows.append({"id": s["id"], "q": s["q"], "topic": s.get("topic"), "want": want, "got": got[:3], "choose": ch[:3], "outcome": o})
for f in (lib, out + ".in.json", out + ".raw.json"):
    os.remove(f)
json.dump({"outcomes": dict(oc), "rows": rows}, open(out, "w"), indent=1, ensure_ascii=False)
print("scenario coverage:", dict(sorted(oc.items())))
for o in ("WRONG", "MISSING kind", "miss", "false alarm"):
    for x in rows:
        if x["outcome"] == o:
            print(f"{o}: {x['id']} [{x['topic']}] want {x['want']} got {x['got']} :: {x['q'][:90]}")
