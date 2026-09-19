#!/usr/bin/env python3
"""
identify_and_rename.py — identify unlabeled TV episode rips by their subtitle
dialogue and rename them to "SxxEyy - Title.mkv".

MakeMKV and similar tools leave discs as meaningless titles like
"Show- Season 1- Disc 1_t00.mkv". This tool reads each rip's subtitles (an
embedded text track if present, otherwise OCR of the image subtitles), matches
the dialogue against a folder of correctly-labeled reference subtitles for that
show, and renames each file to the episode it actually contains. Anything it
can't identify confidently (extras, bloopers, a "play all" title) is set aside.

It is content-based: it does not trust disc/title order, so it catches
out-of-order discs and quietly ignores non-episode titles.

Two-part episodes: box sets often author a double-length premiere/finale as ONE
title (e.g. "Part 1" + "Part 2" in a single file). This tool detects a rip that
runs far longer than the season's typical episode, samples a second window near
its end to identify the later part, and names it as a span, e.g.
"S02E01-E02 - Title.mkv" (which Plex understands natively).

--------------------------------------------------------------------------------
LAYOUT IT EXPECTS

  <show>/
    Season 1/   ...rip .mkv files...
    Season 2/   ...
    Subtitles/
      <anything with "season 1" in the name>/  Show - 1x01 - Title.srt, ...
      <anything with "season 2" in the name>/  Show - 2x01 - Title.srt, ...

  Reference subtitle filenames must contain the episode as "1x01" or "S01E01".
  Release tags after the title (e.g. ".720p HDTV.GROUP") are stripped
  automatically. Reference subs are plain-text .srt.

--------------------------------------------------------------------------------
REQUIREMENTS

  ffmpeg + ffprobe on PATH ......... always
  tesseract + pillow + pytesseract . only for rips whose subtitles are images
                                      (BluRay PGS / DVD VobSub, i.e. no text track)

  macOS:   brew install ffmpeg tesseract
           python3 -m pip install --break-system-packages pillow pytesseract
  Debian:  sudo apt install ffmpeg tesseract-ocr
           pip3 install pillow pytesseract

--------------------------------------------------------------------------------
USAGE

  # dry-run: identify everything, write a report, rename nothing
  python3 identify_and_rename.py --show-root "/path/to/Show"

  # once the report looks right, actually rename + move extras
  python3 identify_and_rename.py --show-root "/path/to/Show" --apply

  Run from inside a show folder and --show-root defaults to the current dir.
  Transcripts are cached in <show>/.idcache so re-runs never re-OCR.

MIT licensed. No warranty — always review the dry-run report before --apply.
"""
import os, re, sys, json, time, shutil, argparse, tempfile, subprocess, statistics

# ---- release-tag vocabulary used to clean reference episode titles ------------
QUALITY = re.compile(r"(?i)^(dsr|hdtv|dvdrip|dvd|bluray|blu-ray|web[- ]?dl|webrip|web|"
                     r"720p|1080p|2160p|480p|x264|x265|h264|h265|hevc|proper|repack|"
                     r"internal|amzn|nf|dsnp|hmax|aac|ac3|dd5|remux)$")
# scene-group names seen after the quality tag; extend for your own packs.
GROUPS = set(x.lower() for x in [
    "0TV","LOL","FQM","ORPHEUS","DIMENSION","KILLERS","IMMERSE","DEFiNE","CTU","FoV",
    "aAF","ASAP","fever","2HD","TB","TLA","NoTV","DOT","RRR","Delta9","ORENJI","BHB",
    "BiA","SME","P0W4","AVS","BATV","EVOLVE","KYR","MeGusta","NTb","CasStudio","ION10",
    "SVA","AFG","RARBG","GHOULS","BTN","TrollHD","STRiFE","MEMENTO","CtrlHD"])


def sh(a):
    return subprocess.run(a, capture_output=True, text=True)


def clean_title(raw):
    t = re.sub(r"\.(srt)$", "", raw, flags=re.I)
    t = re.sub(r"\.(en|eng)(\.sdh)?$", "", t, flags=re.I)
    toks = t.split(".")
    qi = next((i for i, tk in enumerate(toks)
               if any(QUALITY.match(p) for p in tk.split())), None)
    if qi is not None:
        toks = toks[:qi]
    else:
        while toks and toks[-1].strip().lower() in GROUPS:
            toks.pop()
    return re.sub(r"\s+", " ", ".".join(toks).strip())


def strip_part_marker(title):
    """Remove a 'Part 1' / '-pt2' / '(1)' style marker so a spanned two-parter
    gets one shared title."""
    t = re.sub(r"[ _-]*(part|pt)[ ._]?\d+\b", "", title, flags=re.I)
    t = re.sub(r"\s*\(\d\)\s*$", "", t)
    return re.sub(r"\s+", " ", t).strip(" -_")


def sanitize(s):
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|]', "", s)).strip()


def parse_srt_text(txt):
    out = []
    for line in txt.splitlines():
        line = line.strip()
        if not line or line.isdigit() or "-->" in line:
            continue
        if re.match(r"\d\d:\d\d:\d\d", line):
            continue
        out.append(re.sub(r"<[^>]+>", "", line))
    return re.sub(r"[^a-z0-9 ]", " ", " ".join(out).lower())


def read_srt(p):
    try:
        return parse_srt_text(open(p, encoding="utf-8", errors="ignore").read())
    except Exception:
        return ""


def read_txt(p):
    try:
        return open(p, encoding="utf-8", errors="ignore").read()
    except Exception:
        return ""


_WORDS = {"one":1,"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,"eight":8,
          "nine":9,"ten":10,"eleven":11,"twelve":12}

def season_of(name):
    m = re.search(r"season\s*(\d+)", name, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"\bs(\d{1,2})\b", name, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"season\s*(" + "|".join(_WORDS) + r")", name, re.I)
    return _WORDS[m.group(1).lower()] if m else None


def build_refs(subs_root, season):
    refs = {}
    if not os.path.isdir(subs_root):
        return refs
    for dp, _, fns in os.walk(subs_root):
        if season_of(dp) not in (season, None):
            continue
        for f in fns:
            if not f.lower().endswith(".srt"):
                continue
            m = re.search(r"\b(\d{1,2})x(\d{2})\b", f) or re.search(r"S(\d\d)E(\d\d)", f, re.I)
            if not m:
                continue
            s, e = int(m.group(1)), int(m.group(2))
            if s != season:
                continue
            raw = re.split(r"\b\d{1,2}x\d{2}\b[ _-]*|S\d\dE\d\d[ _-]*", f, flags=re.I)[-1]
            title = clean_title(raw)
            txt = read_srt(os.path.join(dp, f))
            if len(txt) > 200 and ((s, e) not in refs or len(txt) > len(refs[(s, e)][1])):
                refs[(s, e)] = (title, txt)
    return refs


def sub_streams(path):
    """Return (subrip_abs, image_abs, image_rel): absolute stream index of the
    English text (subrip) track, absolute index of the English image track, and
    the image track's index *among subtitle streams only* (the N in ffmpeg 0:s:N,
    which the OCR overlay filter needs)."""
    r = sh(["ffprobe", "-v", "error", "-select_streams", "s",
            "-show_entries", "stream=index,codec_name:stream_tags=language",
            "-of", "csv=p=0", path])
    subrip, image, image_rel = None, None, None
    rel = -1
    for line in r.stdout.splitlines():
        p = line.split(",")
        if len(p) < 2:
            continue
        rel += 1
        idx, codec = int(p[0]), p[1].strip()
        lang = p[2].strip() if len(p) > 2 else ""
        if codec == "subrip" and subrip is None and lang in ("eng", "en", ""):
            subrip = idx
        elif codec in ("dvd_subtitle", "hdmv_pgs_subtitle") and image is None and lang in ("eng", "en", ""):
            image, image_rel = idx, rel
    return subrip, image, image_rel


def video_dims(path):
    r = sh(["ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=p=0", path])
    try:
        w, h = r.stdout.strip().split(",")[:2]
        return int(w), int(h)
    except Exception:
        return 1280, 720


def runtime_sec(path):
    r = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "csv=p=0", path])
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def extract_subrip(path, idx):
    with tempfile.NamedTemporaryFile(suffix=".srt", delete=False) as tf:
        tmp = tf.name
    # -threads 4 avoids a demux hang some MKVs hit on subtitle extraction.
    try:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-threads", "4",
                        "-i", path, "-map", f"0:{idx}", "-c:s", "srt", tmp],
                       capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        pass
    txt = read_srt(tmp)
    try:
        os.unlink(tmp)
    except OSError:
        pass
    return txt


def ocr_sample(path, image_rel, start, dur, fps):
    """OCR the image subtitle by rendering it onto a BLACK canvas (crisp
    white-on-black, no video clutter), de-duping identical frames, and reading
    each with tesseract. Input-seek (`-ss start`) windows the sample so a late
    window can identify the second half of a two-part episode."""
    try:
        from PIL import Image
        import pytesseract
    except ImportError:
        return "", "NO_OCR_DEPS"
    if image_rel is None:
        return "", "ocr"
    import hashlib
    W, H = video_dims(path)
    d = tempfile.mkdtemp()
    vf = f"[0:s:{image_rel}]setpts=PTS-STARTPTS[s];[1:v][s]overlay=shortest=1[o]"
    try:
        subprocess.run(["ffmpeg", "-y", "-v", "error",
                        "-ss", str(start), "-i", path,
                        "-f", "lavfi", "-t", str(dur), "-i", f"color=c=black:s={W}x{H}",
                        "-filter_complex", vf, "-map", "[o]", "-r", str(fps),
                        os.path.join(d, "f_%05d.png")],
                       capture_output=True, timeout=1800)
    except subprocess.TimeoutExpired:
        pass
    texts, last = [], None
    for fn in sorted(os.listdir(d)):
        fp = os.path.join(d, fn)
        try:
            im = Image.open(fp).convert("L")
            if sum(im.histogram()[200:]) >= 40:          # frame has bright (text) pixels
                h = hashlib.md5(im.tobytes()).hexdigest()
                if h != last:                            # skip repeats of the same subtitle
                    last = h
                    im2 = im.point(lambda p: 255 if p > 140 else 0)
                    t = pytesseract.image_to_string(im2, config="--psm 6")
                    if t.strip():
                        texts.append(t)
        except Exception:
            pass
        try:
            os.unlink(fp)
        except OSError:
            pass
    os.rmdir(d)
    return re.sub(r"[^a-z0-9 ]", " ", " ".join(texts).lower()), "ocr"


def get_transcript(path, cache, args, start=None, dur=None, suffix=""):
    """Return (text, method). `suffix` gives a distinct cache slot for a second
    (e.g. late-window) sample of the same file. subrip extraction returns the
    whole track regardless of start/dur; OCR honors the window."""
    start = args.start if start is None else start
    dur = args.dur if dur is None else dur
    base = os.path.basename(path)
    cp = os.path.join(cache, base + suffix + ".txt")
    mp = os.path.join(cache, base + suffix + ".method")
    if os.path.exists(cp):
        return read_txt(cp), (open(mp).read().strip() if os.path.exists(mp) else "cache")
    subrip, image, image_rel = sub_streams(path)
    if subrip is not None:
        txt, method = extract_subrip(path, subrip), "subrip"
    elif image is not None and not args.no_ocr:
        txt, method = ocr_sample(path, image_rel, start, dur, args.fps)
    else:
        txt, method = "", ("NO_OCR_DEPS" if image is not None else "NO_SUBS")
    if len(txt) >= 100:
        os.makedirs(cache, exist_ok=True)
        open(cp, "w").write(txt)
        open(mp, "w").write(method)
    return txt, method


def windows(text, n=6):
    w = text.split()
    return set(" ".join(w[i:i+n]) for i in range(len(w) - n + 1))


def score(qs, refs):
    return sorted(((k, sum(1 for w in qs if w in v[1])) for k, v in refs.items()),
                  key=lambda x: -x[1])


def is_confident(hits, second, words, method, args):
    """Accept a match when it clears the hit floor and either beats the runner-up
    by 2x or is decisive on its own (strong-hits). For OCR, also require a minimum
    hits/words ratio so garbage transcripts can't win by luck."""
    if hits < args.min_hits:
        return False
    if method != "subrip" and words and (hits / words) < args.min_ratio:
        return False
    return hits >= 2 * max(second, 1) or hits >= args.strong_hits


def check_env(root, subs_root):
    problems = []
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            problems.append(f"'{tool}' not found on PATH — install ffmpeg.")
    if not os.path.isdir(root):
        problems.append(f"--show-root does not exist: {root}")
    if not os.path.isdir(subs_root):
        problems.append(f"No reference subtitles folder found at: {subs_root}\n"
                        f"   Create it and add per-season .srt files named like "
                        f"'Show - 1x01 - Title.srt'.")
    if not any(season_of(d) for d in (os.listdir(root) if os.path.isdir(root) else [])):
        problems.append(f"No 'Season N' subfolders found in: {root}")
    if problems:
        print("Setup problems:\n" + "\n".join("  - " + p for p in problems))
        print("\nSee the header of this script for the full requirements/layout.")
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="Identify unlabeled TV rips by subtitle "
                                             "dialogue and rename them.")
    ap.add_argument("--show-root", default=".", help="show folder (default: current dir)")
    ap.add_argument("--subs", default="", help="reference subtitles folder "
                    "(default: <show-root>/Subtitles)")
    ap.add_argument("--apply", action="store_true", help="perform renames/moves "
                    "(default is a dry-run)")
    ap.add_argument("--no-ocr", action="store_true", help="skip image-subtitle OCR")
    ap.add_argument("--season", type=int, default=0,
                    help="only process this season number (default: all)")
    ap.add_argument("--start", type=int, default=180, help="OCR sample start (sec)")
    ap.add_argument("--dur", type=int, default=240, help="OCR sample length (sec)")
    ap.add_argument("--fps", type=float, default=1.0, help="OCR sample frames/sec")
    ap.add_argument("--min-hits", type=int, default=8, help="min dialogue hits to accept")
    ap.add_argument("--min-ratio", type=float, default=0.12, dest="min_ratio",
                    help="min (hits / OCR-words) ratio to accept an OCR match")
    ap.add_argument("--strong-hits", type=int, default=40, dest="strong_hits",
                    help="hit count that confirms a match regardless of the runner-up")
    ap.add_argument("--span-ratio", type=float, default=1.55,
                    help="a rip this many times the season's median runtime is treated "
                         "as a possible multi-part episode")
    args = ap.parse_args()

    root = os.path.abspath(args.show_root.rstrip("/"))
    subs_root = os.path.abspath(args.subs) if args.subs else os.path.join(root, "Subtitles")
    check_env(root, subs_root)

    cache = os.path.join(root, ".idcache")
    jpath = os.path.join(root, "id_results.json")
    mpath = os.path.join(root, "id_report.md")

    season_dirs = sorted([os.path.join(root, d) for d in os.listdir(root)
                          if os.path.isdir(os.path.join(root, d)) and season_of(d)],
                         key=lambda p: season_of(os.path.basename(p)))
    print(f"Show: {os.path.basename(root)} — {len(season_dirs)} season folder(s)"
          f"{'  [DRY-RUN]' if not args.apply else '  [APPLY]'}\n", flush=True)

    plan = []
    coverage = {}   # season -> set of episode numbers covered
    for sd in season_dirs:
        season = season_of(os.path.basename(sd))
        if args.season and season != args.season:
            continue
        refs = build_refs(subs_root, season)
        rips = sorted([os.path.join(sd, f) for f in os.listdir(sd) if f.lower().endswith(".mkv")])
        print(f"== Season {season}: {len(rips)} rips vs {len(refs)} reference episodes ==", flush=True)
        if not refs:
            print("   (no reference subs for this season — skipping)\n", flush=True)
            continue

        # pass 1: transcript, primary score, runtime
        recs = []
        for path in rips:
            t0 = time.time()
            txt, method = get_transcript(path, cache, args)
            scored = score(windows(txt), refs) if len(txt) >= 100 else []
            recs.append({"path": path, "file": os.path.basename(path), "txt_len": len(txt),
                         "words": len(txt.split()), "method": method, "scored": scored,
                         "rt": runtime_sec(path), "dt": time.time() - t0})
        rts = [r["rt"] for r in recs if r["rt"] > 0]
        median = statistics.median(rts) if rts else 0

        # pass 2: decide single vs span vs unidentified
        items = []
        for r in recs:
            f, path, method, scored = r["file"], r["path"], r["method"], r["scored"]
            if not scored:
                items.append({**base_item(f, path, season, method), "status": "UNIDENTIFIED"})
                print(f"   {f}\n      -> {method} (no dialogue to match)", flush=True)
                continue
            (bs, be), bh = scored[0]
            (ss, se), shh = (scored[1][0], scored[1][1]) if len(scored) > 1 else ((0, 0), 0)
            is_long = median and r["rt"] >= args.span_ratio * median
            nums = None
            if is_long and is_confident(bh, 0, r["words"], method, args):
                nums = [be]
                other = None
                if method == "subrip":
                    # full transcript holds both parts: the runner-up is the other part
                    if shh >= max(args.min_hits, 0.4 * bh) and abs(se - be) == 1:
                        other = se
                else:
                    # OCR only saw the early window: sample a late window for part 2
                    late = int(r["rt"] * 0.62)
                    txt2, _ = get_transcript(path, cache, args, start=late, dur=args.dur, suffix=".late")
                    w2 = len(txt2.split())
                    sc2 = score(windows(txt2), refs) if len(txt2) >= 100 else []
                    if sc2 and is_confident(sc2[0][1], 0, w2, method, args) \
                            and sc2[0][0][1] != be and abs(sc2[0][0][1] - be) == 1:
                        other = sc2[0][0][1]
                if other:
                    nums = sorted([be, other])
                status = "MATCH"
            elif is_confident(bh, shh, r["words"], method, args):
                nums = [be]
                status = "MATCH"
            else:
                status = "UNIDENTIFIED"
            it = base_item(f, path, season, method)
            if status == "MATCH":
                title = strip_part_marker(refs[(season, nums[0])][0]) if len(nums) > 1 \
                        else refs[(season, nums[0])][0]
                it.update({"nums": nums, "title": title, "hits": bh, "second": shh,
                           "status": "MATCH"})
                span = f"E{nums[0]:02d}" if len(nums) == 1 else f"E{nums[0]:02d}-E{nums[-1]:02d}"
                print(f"   {f}\n      -> S{season:02d}{span} '{title}' "
                      f"({bh}/{shh}) [{method} {r['dt']:.0f}s]"
                      f"{'  [SPAN]' if len(nums) > 1 else ''}", flush=True)
            else:
                it.update({"hits": bh, "second": shh, "status": "UNIDENTIFIED"})
                print(f"   {f}\n      -> UNIDENTIFIED ({bh}/{shh}) [{method} {r['dt']:.0f}s]", flush=True)
            items.append(it)

        # duplicate resolution: strongest rip keeps each episode
        claims = {}
        for it in items:
            if it["status"] == "MATCH":
                for n in it["nums"]:
                    claims.setdefault(n, []).append(it)
        for n, its in claims.items():
            its.sort(key=lambda x: -x["hits"])
            for loser in its[1:]:
                if loser["status"] == "MATCH":
                    loser["status"] = "DUPLICATE"
        cov = set()
        for it in items:
            if it["status"] == "MATCH":
                span = (f"E{it['nums'][0]:02d}" if len(it["nums"]) == 1
                        else f"E{it['nums'][0]:02d}-E{it['nums'][-1]:02d}")
                it["target"] = sanitize(f"S{season:02d}{span} - {it['title']}") + ".mkv"
                cov.update(it["nums"])
            else:
                it["target"] = None
            plan.append(it)
        coverage[season] = (cov, set(e for (s, e) in refs))
        missing = sorted(coverage[season][1] - cov)
        print(f"   => {sum(1 for it in items if it['status']=='MATCH')} identified, "
              f"{sum(1 for it in items if it['status']!='MATCH')} to Extras"
              f"{('  MISSING: ' + ', '.join('E%02d'%e for e in missing)) if missing else ''}\n",
              flush=True)

    json.dump(plan, open(jpath, "w"), indent=1)
    matched = [it for it in plan if it["status"] == "MATCH"]
    extras = [it for it in plan if it["status"] != "MATCH"]
    with open(mpath, "w") as w:
        w.write(f"# {os.path.basename(root)} — identification\n\n")
        w.write(f"{len(matched)} identified, {len(extras)} to Extras.\n\n## Renames\n\n")
        for it in sorted(matched, key=lambda x: (x['season'], x['nums'][0])):
            w.write(f"- `{it['file']}`  ->  **{it['target']}**  ({it['hits']} hits)\n")
        if extras:
            w.write("\n## To Extras (unidentified / duplicate)\n\n")
            for it in extras:
                w.write(f"- `{it['file']}` — {it['status']} ({it['method']})\n")
        gaps = {s: sorted(full - cov) for s, (cov, full) in coverage.items() if (full - cov)}
        if gaps:
            w.write("\n## Missing episodes (no rip matched)\n\n")
            for s in sorted(gaps):
                w.write(f"- Season {s}: " + ", ".join("E%02d" % e for e in gaps[s]) + "\n")

    if not args.apply:
        print(f"DRY-RUN complete. {len(matched)} to rename, {len(extras)} to Extras.")
        print(f"Review {mpath}, then re-run with --apply.")
        return

    import uuid
    for it in extras:
        sd = os.path.dirname(it["path"])
        ex = os.path.join(sd, "Extras")
        os.makedirs(ex, exist_ok=True)
        dst = os.path.join(ex, it["file"])
        if os.path.abspath(it["path"]) != os.path.abspath(dst) and not os.path.exists(dst):
            os.rename(it["path"], dst)
    staged = []
    for it in matched:
        sd = os.path.dirname(it["path"])
        tmp = os.path.join(sd, f".id_{uuid.uuid4().hex}.tmp")
        os.rename(it["path"], tmp)
        staged.append((tmp, os.path.join(sd, it["target"])))
    done = 0
    for tmp, dst in staged:
        if os.path.exists(dst):
            os.rename(tmp, dst + ".CONFLICT"); print(f"   CONFLICT: {os.path.basename(dst)}")
        else:
            os.rename(tmp, dst); done += 1
    print(f"APPLIED: {done} renamed, {len(extras)} moved to Extras/.")


def base_item(f, path, season, method):
    return {"file": f, "path": path, "season": season, "nums": [], "title": "",
            "hits": 0, "second": 0, "method": method}


if __name__ == "__main__":
    main()
