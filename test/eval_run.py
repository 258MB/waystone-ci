"""Run a question set through the real app (same program, library and models as shipped).

Usage: python3 -I tools/eval.py <stage-dir> <set: test|dev> <label>
  <stage-dir> is a built app folder with library.json, models/ and llama/ (see tools/stage.py).
Writes eval/results/<label>_<set>.json with every answer, and prints the automatic scores.
The automatic scores are a first pass only; the real score is the hand review.
"""
import json, os, re, signal, subprocess, sys, time, urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("EVAL_ROOT") or Path(__file__).resolve().parent.parent)
BIN = Path(os.environ.get("EVAL_BIN") or ROOT / "build" / "bin" / "guide-macos-arm64")  # EVAL_BIN on the Linux test machine
PORT = int(os.environ.get("EVAL_PORT", "18940"))  # several tests side by side on the cloud machine
CPU = os.environ.get("EVAL_CPU") == "1"  # run the AI on the processor (for the 8B model on an 8 GB Mac)


def footprint_mb():
    """Memory the app's AI servers really use (macOS footprint, includes graphics memory), in MB."""
    total = 0
    pids = subprocess.run(["pgrep", "-f", "llama-server"], capture_output=True, text=True).stdout.split()
    if sys.platform.startswith("linux"):  # no "footprint" tool: resident memory instead
        for pid in pids:
            try:
                m = re.search(r"VmRSS:\s+(\d+) kB", Path(f"/proc/{pid}/status").read_text())
                total += int(m.group(1)) / 1024 if m else 0
            except OSError:
                pass
        return round(total)
    for pid in pids:
        out = subprocess.run(["footprint", "-p", pid], capture_output=True, text=True).stdout
        m = re.search(r"Footprint: ([\d.]+) (KB|MB|GB)", out)
        if m:
            total += float(m.group(1)) * {"KB": 1 / 1024, "MB": 1, "GB": 1024}[m.group(2)]
    return round(total)


# Run through tools/heavy-guard.py (see AGENTS.md): it watches memory and stops the run before the Mac freezes.


def call(path, body=None, timeout=600):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def quick(q, prev):
    """The instant first view (/api/quick), which people act on for minutes: banner, warning and the pages shown."""
    t = time.time()
    try:
        r = call("/api/quick", {"q": q, "prev": prev}, timeout=120)
    except Exception as e:  # never stop a run for the first view
        return {"error": str(e)[:200]}, round((time.time() - t) * 1000)
    ms = round((time.time() - t) * 1000)
    return {"status": r.get("status", ""), "urgent": bool(r.get("urgent")), "warning": r.get("warning", ""),
            "message": r.get("message", ""), "cards": [b["card_id"] for b in r.get("blocks") or []],
            "reason": (r.get("debug") or {}).get("reason", "")}, ms


def shown(res):
    """All text a user sees as the answer (message plus the sentences shown, not the hidden full card)."""
    t = [res.get("situation", ""), res.get("message", "")]  # situation: the checked line on top when a banner fires
    if res.get("answer_line"):  # the one sentence shown on top
        t.append(res["answer_line"]["text"])
    last = None
    for b in res.get("blocks") or []:
        if b.get("for") and b["for"] != last:  # understand, then search: the short question heading
            t.append(b["for"])
            last = b["for"]
        sents = b["full"] if res["status"] in ("closest", "fixed") else b["sentences"]
        t += [s["text"] for s in sents]
    if res.get("next_hours"):  # r20: the no-hospital sections shown after the steps
        t.append("Over the next hours: " + " ".join(res["next_hours"]))
    if res.get("worse_if"):
        t.append("Getting worse if: " + " ".join(res["worse_if"]))
    if res.get("choices"):  # "Is it one of these?": page titles, one tap opens the whole page
        t.append(("Is it one of these? (shown first) " if res.get("unsure") else "Is it one of these? ")
                 + " | ".join(c["title"] for c in res["choices"]))
    return "\n".join(t)


def pages_shown(res):
    """The pages in screen order: the answer's cards, then the choices (the choices first when the search is unsure)."""
    blocks = [b["card_id"] for b in res.get("blocks") or []]
    choices = [c["card_id"] for c in res.get("choices") or []]
    return list(dict.fromkeys(choices + blocks if res.get("unsure") else blocks + choices))


def auto_score(q, exp, res):
    s = {}
    cards = [b["card_id"] for b in res.get("blocks") or []]
    answered = res["status"] in ("answer", "closest")
    text = shown(res)
    allowed = set(exp.get("cards", []))
    if exp.get("refuse"):
        s["refuse_ok"] = not answered
    elif exp.get("ok_refuse") and not answered:
        s["refused_acceptably"] = True
    else:
        s["answered"] = answered
        s["card_ok"] = any(c in allowed for c in cards)
        s["must_ok"] = all(re.search(m, text, re.I) for m in exp.get("must", []))
    s["must_not_ok"] = not any(re.search(m, text, re.I) for m in exp.get("must_not", []))
    s["urgent_ok"] = res["urgent"] if exp.get("urgent") else True
    s["pass"] = all(v for v in s.values())
    return s


class Stopped(Exception):
    pass


def _stop(signum, frame):
    raise Stopped()


def main():
    # The big cloud run stops jobs at its time limit with SIGTERM: keep the answers done so far.
    signal.signal(signal.SIGTERM, _stop)
    stopped = False
    stage, qset, label = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    qs = json.loads((ROOT / "eval" / f"questions_{qset}.json").read_text())
    ef = ROOT / "eval" / f"expect_{qset}.json"
    exps = json.loads(ef.read_text()) if ef.exists() else {}
    log = open(ROOT / "eval" / "results" / f"{label}_{qset}.server.txt", "w")
    proc = subprocess.Popen([str(BIN), "--resources", str(stage), "--port", str(PORT), "--no-browser", "--no-idle-exit"] + (["--cpu-only"] if CPU else []),
                            stdout=log, stderr=log)
    # Every answer is also appended to a progress file, so a run stopped by the guard resumes where it stopped
    # (temperature 0 and no prompt cache: an answer does not depend on the questions asked before it).
    prog = ROOT / "eval" / "results" / f"{label}_{qset}.progress.jsonl"
    rows = [json.loads(l) for l in prog.read_text().splitlines() if l.strip()] if prog.exists() else []
    done = {r["id"] for r in rows}
    pf = open(prog, "a")
    load_s, mem_ready, mem_peak, st = 0, 0, 0, {}
    try:
        t0 = time.time()
        while True:
            try:
                st = call("/api/status")
                if st.get("chat") == "ready" or st.get("error"):
                    break
            except Exception:
                pass
            time.sleep(1)
        load_s = time.time() - t0
        mem_ready = footprint_mb()
        mem_peak = mem_ready
        print("ready", round(load_s, 1), "s", mem_ready, "MB", st, flush=True)
        for q in qs:
            if stopped:
                break
            if q["id"] in done:
                continue
            # r23: the instant first view, as the page asks for it before the answer (same question and prev).
            qk, qk_ms = quick(q["q"], q.get("prev") or "")
            t = time.time()
            res = call("/api/ask", {"q": q["q"], "prev": q.get("prev") or ""})  # as the app sends the question before
            el = time.time() - t
            mem_peak = max(mem_peak, footprint_mb())
            exp = exps.get(q["id"], {})
            sc = auto_score(q, exp, res)
            rows.append({"id": q["id"], "q": q["q"], "topic": q.get("topic"), "expect_text": q.get("expect"),
                         "expect": exp, "status": res["status"], "urgent": res["urgent"], "warning": res.get("warning", ""), "situation": res.get("situation", ""), "fixed_rule": res.get("debug", {}).get("fixed", ""),
                         "cards": [b["card_id"] for b in res.get("blocks") or []], "shown": shown(res),
                         "choices": [c["card_id"] for c in res.get("choices") or []], "unsure": bool(res.get("unsure")),
                         "pages_shown": pages_shown(res),
                         "seconds": round(el, 2), "auto": sc, "debug": res["debug"],
                         "quick": qk, "quick_ms": qk_ms})
            pf.write(json.dumps(rows[-1], ensure_ascii=False) + "\n")
            pf.flush()
            print(q["id"], res["status"], rows[-1]["cards"], f"{el:.1f}s", "PASS" if sc["pass"] else "FAIL " + json.dumps(sc), flush=True)
    except (Stopped, BrokenPipeError):
        stopped = True
    finally:
        pf.close()
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            call("/api/quit", {})
        except Exception:
            pass
        time.sleep(2)
        proc.terminate()
    n = len(rows)
    passed = sum(r["auto"]["pass"] for r in rows)
    secs = sorted(r["seconds"] for r in rows)
    meta = {"label": label, "set": qset, "n": n, "auto_pass": passed, "load_s": round(load_s, 1),
            "median_s": secs[n // 2] if n else None, "max_s": secs[-1] if n else None,
            "mem_ready_mb": mem_ready, "mem_peak_mb": mem_peak, "cpu_only": CPU, "status": st,
            "partial": stopped, "of": len(qs)}
    # A stopped set is saved as .partial.json, so a later run does it again in full.
    out = ROOT / "eval" / "results" / f"{label}_{qset}{'.partial' if stopped else ''}.json"
    out.write_text(json.dumps({"meta": meta, "rows": rows}, ensure_ascii=False, indent=1))
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
