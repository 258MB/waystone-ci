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
DEADLINE = float(os.environ.get("EVAL_DEADLINE_S") or 0)  # stop asking new questions after this many seconds (CI step limit)
QUICK_ONLY = os.environ.get("EVAL_QUICK_ONLY") == "1"  # model-free run (Linux quick job): only the instant first view (/api/quick), never /api/ask
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


def quick(q, prev, age="", cpr=False):
    """The instant first view (/api/quick), which people act on for minutes: banner, warning and the pages shown.
    age and cpr are the page's own memory (the app sends them with every question for 15 minutes, see ask() in app.js)."""
    t = time.time()
    try:
        r = call("/api/quick", {"q": q, "prev": prev, "age": age, "cpr": cpr}, timeout=120)
    except Exception as e:  # never stop a run for the first view
        return {"error": str(e)[:200]}, round((time.time() - t) * 1000), {}
    ms = round((time.time() - t) * 1000)
    return {"status": r.get("status", ""), "urgent": bool(r.get("urgent")), "warning": r.get("warning", ""),
            "message": r.get("message", ""), "cards": [b["card_id"] for b in r.get("blocks") or []],
            "reason": (r.get("debug") or {}).get("reason", "")}, ms, r


# r23q: what the person really sees first. The app draws the pinned "Not breathing" button, then the dispatch box
# (a question with big buttons, instead of the red banner), the scene warning, the light line "Is someone down right
# now?", the breathing question, the one checked line, the warning, the answer line, the pages, the "next hours" parts
# and "Is it one of these?" (renderAnswer in web/app.js). The old "shown" text had only the pages, so a box screen
# with no page under it came out as an empty answer. screen() records all of it, in screen order, as fields.
AGES = ("adult", "child", "baby")
BANNER_TEXT = "Act now. Make yourself and the scene safe, then do the steps in order. Stay with the person, and signal for help if you can (whistle: 3 blasts, a light, a radio)."


def _title(d, bid):
    b = (d.get("blocks") or {}).get(bid) or {}
    return b.get("title") or bid


def _page(d, bid, whole=False):
    b = (d.get("blocks") or {}).get(bid) or {}
    sents = [s.get("text", "") for s in (b.get("full") if whole and b.get("full") else b.get("sentences")) or []]
    return {"id": bid, "title": b.get("title") or bid, "sentences": sents}


def _open(tree, b, d, age):
    out = []
    add = lambda ids: [out.append(i) for i in ids or [] if i not in out]
    age = age if age in AGES else "adult"
    add(b.get("pages"))
    pba = b.get("pages_by_age")
    if pba:
        w = pba.get(age + "_water")
        add(w if (w and d.get("water")) else pba.get(age))
    if b.get("add_scene"):
        add(d.get("scene_pages"))
    nxt = b.get("next") or ""
    if nxt == "@then":
        nxt = d.get("then") or ""
    if b.get("add_cause"):
        cp = (d.get("pages_by_age") or {}).get(age) or d.get("pages") or []
        if not cp and b.get("if_no_cause"):
            add(b["if_no_cause"].get("pages"))
            nxt = b["if_no_cause"].get("next") or ""
        else:
            add(cp)
    return out, nxt


def _first(b, age, water):
    f = b.get("first_by_age")
    age = age if age in AGES else "adult"
    return ((f and ((water and f.get(age + "_water")) or f.get(age) or f.get("adult"))) or b.get("first") or "")


def box_screen(d):
    """The dispatch box as first drawn (web/dispatch.js view): the open question, its lines and buttons."""
    tree = d.get("tree") or {}
    n = (tree.get("nodes") or {}).get(d.get("node")) or {}
    tx = tree.get("texts") or {}
    out = {"why": d.get("why", ""), "node": d.get("node", ""), "then": d.get("then", ""), "age_hint": d.get("age_hint", ""),
           "auto_age": d.get("auto", ""), "auto_line": d.get("auto_line", ""), "animal_button": tx.get("animal_choice", "It is an animal") if d.get("animal_choice") else "",
           "question": n.get("question", ""), "subline": n.get("subline", ""),
           "past_hint": n.get("past_hint", "") if d.get("past") else "",
           "scene_lines": d.get("scene_lines") or [] if n.get("scene_lines") else [], "buttons": [],
           "found_pages": [_title(d, i) for i in d.get("found") or []]}
    for b in n.get("buttons") or []:
        pages, nxt = _open(tree, b, d, "adult")
        nq = ((tree.get("nodes") or {}).get(nxt) or {}).get("question", "")
        out["buttons"].append({"id": b.get("id"), "label": b.get("label"), "danger": b.get("style") == "danger",
                               "gives_first": _first(b, "adult", bool(d.get("water"))), "gives_lead": b.get("lead", ""),
                               "gives_pages": [_title(d, i) for i in pages], "then_asks": nq})
    return out


def box_walk(d):
    """The taps that reach the CPR steps from this box (the sure-danger path: safe, the age the words hint or adult, not
    awake, not breathing; or Yes keep going on the one-tap screen), with what each tap shows. This is what the person
    reads AFTER the taps; the first screen is box_screen. Never more than 8 taps."""
    tree = d.get("tree") or {}
    nodes = tree.get("nodes") or {}
    node, age, steps = d.get("node"), "", []
    for _ in range(8):
        n = nodes.get(node)
        if not n:
            break
        bs = n.get("buttons") or []
        pick = None
        if n.get("age_choice"):
            hint = d.get("age_hint", "")
            pick = next((b for b in bs if b.get("id") == (hint if hint in AGES + ("newborn",) else "adult")), bs[0] if bs else None)
        else:
            pick = next((b for b in bs if b.get("cpr")), None)
            if not pick:
                want = {"scene": "yes", "safe_now": "safe", "awake": "no", "fit": "no", "after_fit": "no", "breathing": "no"}.get(node)
                pick = next((b for b in bs if b.get("id") == want), bs[0] if bs else None)
        if not pick:
            break
        sa = pick.get("set_age")
        age = d.get("auto", "") if sa == "@auto" else "" if sa == "@none" else sa if sa in AGES else age
        pages, nxt = _open(tree, pick, d, age)
        st = {"asked": n.get("question", ""), "tap": pick.get("label", ""), "age": age, "says": _first(pick, age, bool(d.get("water"))),
              "lead": pick.get("lead", ""), "pages": [_page(d, i) for i in pages], "cpr": bool(pick.get("cpr"))}
        steps.append(st)
        if pick.get("cpr") or (pages and not nxt):
            break
        node = nxt
        if pick.get("then_button"):
            node_b = nodes.get(node) or {}
            tb = next((b for b in node_b.get("buttons") or [] if b.get("id") == pick["then_button"]), None)
            if tb:
                pages2, nxt2 = _open(tree, tb, d, age)
                steps.append({"asked": node_b.get("question", ""), "tap": tb.get("label", "") + " (pressed for you)", "age": age,
                              "says": _first(tb, age, bool(d.get("water"))), "lead": tb.get("lead", ""), "pages": [_page(d, i) for i in pages2], "cpr": bool(tb.get("cpr"))})
                if tb.get("cpr"):
                    break
                node = nxt2
    return {"taps": len(steps), "reaches_cpr": any(s["cpr"] for s in steps), "steps": steps}


def screen(res, pinned_text=""):
    """Everything on the first screen, in screen order, as fields plus one plain text (screen_text)."""
    d = res.get("dispatch")
    lt = res.get("light")
    bc = None if d else res.get("breathing_check")
    sc = {"pinned_button": pinned_text, "banner": BANNER_TEXT if res.get("urgent") and not d and not bc else "",
          "status": res.get("status", ""), "urgent": bool(res.get("urgent")),
          "box": box_screen(d) if d else None, "box_walk": box_walk(d) if d else None,
          "scene_warning": res.get("scene_warning", "") if d else (res.get("scene_warning", "") or ""),
          "light": None, "breathing_check": None, "situation": res.get("situation", ""), "warning": res.get("warning", ""),
          "message": res.get("message", ""), "answer_line": (res.get("answer_line") or {}).get("text", ""),
          "pages": [], "next_hours": res.get("next_hours") or [], "worse_if": res.get("worse_if") or [],
          "choices": [{"id": c["card_id"], "title": c["title"]} for c in res.get("choices") or []], "unsure": bool(res.get("unsure"))}
    if lt and not d:
        tx = (lt.get("tree") or {}).get("texts") or {}
        sc["light"] = {"question": tx.get("light_q", "Is someone down right now?"), "buttons": [tx.get("light_yes", "Yes"), tx.get("light_no", "No")],
                       "yes_opens_box_at": lt.get("node", "")}
    if bc:
        sc["breathing_check"] = {"question": bc.get("question", ""), "subline": bc.get("subline", ""),
                                 "buttons": [{"id": c.get("id"), "label": c.get("label"), "lead": c.get("lead", ""), "pages": [(b.get("title") or "") for b in c.get("blocks") or []]} for c in bc.get("choices") or []]}
    whole = res.get("status") in ("closest", "fixed")
    for b in res.get("blocks") or []:
        sents = b.get("full") if whole else b.get("sentences")
        sc["pages"].append({"id": b["card_id"], "title": b.get("title", ""), "for": b.get("for", ""), "sentences": [s["text"] for s in sents or []]})
    sc["screen_text"] = screen_text(sc, bool(res.get("unsure")))
    return sc


def screen_text(sc, unsure):
    t = []
    if sc["pinned_button"]:
        t.append("[button always on top] " + sc["pinned_button"])
    if sc["banner"]:
        t.append("[red banner] " + sc["banner"])
    b = sc["box"]
    if b:
        t.append("[box] " + b["question"] + (" " + b["subline"] if b["subline"] else ""))
        if b["auto_line"]:
            t.append("[box first line] " + b["auto_line"])
        t += ["[box danger line] " + l for l in b["scene_lines"]]
        t.append("[box buttons] " + " | ".join(x["label"] for x in b["buttons"]) + (" | " + b["animal_button"] if b["animal_button"] else ""))
        if b["found_pages"]:
            t.append("[box, folded below] Other pages found: " + " | ".join(b["found_pages"]))
    if sc["scene_warning"]:
        t.append("[scene warning] " + sc["scene_warning"])
    if sc["light"]:
        t.append("[light line] " + sc["light"]["question"] + " " + " | ".join(sc["light"]["buttons"]))
    if sc["breathing_check"]:
        t.append("[breathing question] " + sc["breathing_check"]["question"] + " " + sc["breathing_check"]["subline"] + " | " + " | ".join(x["label"] for x in sc["breathing_check"]["buttons"]))
    if unsure and sc["choices"]:
        t.append("[Is it one of these? first] " + " | ".join(c["title"] for c in sc["choices"]))
    for k, tag in (("situation", "checked line"), ("warning", "warning box"), ("message", "message")):
        if sc[k]:
            t.append(f"[{tag}] " + sc[k])
    if sc["answer_line"]:
        t.append("[answer line] " + sc["answer_line"])
    for p in sc["pages"]:
        t.append(f"[page {p['id']}] {p['title']}")
        t += ["- " + s for s in p["sentences"]]
    if sc["next_hours"]:
        t.append("[next hours] " + " ".join(sc["next_hours"]))
    if sc["worse_if"]:
        t.append("[getting worse] " + " ".join(sc["worse_if"]))
    if not unsure and sc["choices"]:
        t.append("[Is it one of these?] " + " | ".join(c["title"] for c in sc["choices"]))
    return "\n".join(t)


def earlier_turns(prev):
    """The earlier messages of a question: a text, a list of texts (the conversation so far) or nothing."""
    if isinstance(prev, list):
        return [str(x) for x in prev if str(x).strip()]
    return [str(prev)] if prev else []


def page_state(turns, pinned_text):
    """What the page remembers from the earlier turns, as the real page would send it with the next question.
    The page sends only the LAST question as prev (lastAsk.q), plus the age last tapped and whether CPR was shown in
    the last 15 minutes. Each earlier turn is replayed through /api/quick (no AI); when its first screen is the box (or
    a CPR page) the person is taken to have followed it to the CPR steps: cpr true and the age its hint names (adult
    if none). A question may set its own "page": {"age": ..., "cpr": ...} instead. Set EVAL_PAGE_STATE=none to send neither."""
    age, cpr, seen = "", False, []
    if os.environ.get("EVAL_PAGE_STATE") == "none":
        return age, cpr, seen
    prior = ""
    for t in turns:
        _, _, r = quick(t, prior, age, cpr)
        d = r.get("dispatch")
        seen.append({"turn": t, "box": bool(d), "box_node": (d or {}).get("node", ""), "cpr_shown": bool(r.get("cpr_shown"))})
        if r.get("cpr_shown") or (d and d.get("node") != "awake" and (d.get("node") in ("scene", "cprcheck") or d.get("why") == "newborn")):
            cpr = True
            hint = (d or {}).get("age_hint", "") or (d or {}).get("auto", "")
            age = hint if hint in AGES else ("baby" if hint == "newborn" else age or "adult")
        prior = t
    return age, cpr, seen


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
    pinned_text = ""
    try:
        t0 = time.time()
        while True:
            try:
                st = call("/api/status")
                if QUICK_ONLY:  # the chat model is switched off: wait for the search model only
                    if st.get("embed") == "ready" or st.get("embed") == "error":
                        break
                elif st.get("chat") == "ready" or st.get("error"):
                    break
            except Exception:
                pass
            time.sleep(1)
        load_s = time.time() - t0
        mem_ready = footprint_mb()
        mem_peak = mem_ready
        try:  # the button the app puts on top of every answer ("Not breathing"); its text is in the box tree
            pinned_text = ((call("/api/pinned") or {}).get("tree") or {}).get("texts", {}).get("pinned", "")
        except Exception:
            pinned_text = ""
        print("ready", round(load_s, 1), "s", mem_ready, "MB", st, flush=True)
        for q in qs:
            if stopped:
                break
            if DEADLINE and time.time() - t0 > DEADLINE:
                print("deadline reached, saving what is done", flush=True)
                stopped = True
                break
            if q["id"] in done:
                continue
            # r23: the instant first view, as the page asks for it before the answer (same question, prev, age and cpr).
            # r23q: an earlier conversation (a list of messages, or one text) is sent like the page does: only the last
            # message as prev, with the age and CPR flag the page would remember (page_state). Before r23q a list went
            # to the app as it was, the app could not read it, and the follow-up was answered with no conversation.
            turns = earlier_turns(q.get("prev"))
            own = q.get("page")
            if own is not None:
                age, cpr, seen = own.get("age", ""), bool(own.get("cpr")), [{"given_by_question": True}]
            else:
                age, cpr, seen = page_state(turns, pinned_text)
            prev = turns[-1] if turns else ""
            qk, qk_ms, qres = quick(q["q"], prev, age, cpr)
            t = time.time()
            if QUICK_ONLY:  # no model: the first view is the answer being judged (emergency rows never call the chat model)
                res = dict(qres or {})
                res.setdefault("status", "quick" if qres else "error")
                res.setdefault("urgent", False)
                res.setdefault("debug", {})
            else:
                res = call("/api/ask", {"q": q["q"], "prev": prev, "age": age, "cpr": cpr})  # as the app sends the question before
            el = time.time() - t
            mem_peak = max(mem_peak, footprint_mb())
            exp = exps.get(q["id"], {})
            sc = auto_score(q, exp, res)
            rows.append({"id": q["id"], "q": q["q"], "topic": q.get("topic"), "expect_text": q.get("expect"),
                         "expect": exp, "status": res["status"], "urgent": bool(res.get("urgent")), "warning": res.get("warning", ""), "situation": res.get("situation", ""), "fixed_rule": res.get("debug", {}).get("fixed", ""),
                         "cards": [b["card_id"] for b in res.get("blocks") or []], "shown": shown(res),
                         "choices": [c["card_id"] for c in res.get("choices") or []], "unsure": bool(res.get("unsure")),
                         "pages_shown": pages_shown(res),
                         "earlier_turns": turns, "sent": {"prev": prev, "age": age, "cpr": cpr}, "replayed_turns": seen,
                         "screen": screen(res, pinned_text), "screen_quick": screen(qres, pinned_text) if qres else {},
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
