"""
YT Clipper — local web UI
Run: python3 app.py
Open: http://localhost:5000
"""

import os, sys, json, uuid, threading, subprocess, re, shutil
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 ** 3   # allow uploads up to 4 GB

WORK_DIR = Path("clips_output")
WORK_DIR.mkdir(exist_ok=True)

# Job store: job_id -> {status, progress, clips, error}
jobs: dict = {}
jobs_lock = threading.Lock()

# ── find executables (Windows-safe) ──────────────────────────────────────────

def find_exe(name: str) -> str:
    """Find an executable, checking common Windows install locations."""
    # 1. Already on PATH?
    found = shutil.which(name)
    if found:
        return found

    # 2. Same folder as this script (portable installs)
    local = Path(__file__).parent / (name + (".exe" if sys.platform == "win32" else ""))
    if local.exists():
        return str(local)

    # 3. Common Windows locations for yt-dlp
    if name == "yt-dlp":
        candidates = [
            Path(os.environ.get("APPDATA","")) / "Python/Scripts/yt-dlp.exe",
            Path(os.environ.get("LOCALAPPDATA","")) / "Python/pythoncore-3.15-64/Scripts/yt-dlp.exe",
            Path(os.environ.get("LOCALAPPDATA","")) / "Programs/Python/Python312/Scripts/yt-dlp.exe",
            Path(os.environ.get("LOCALAPPDATA","")) / "Programs/Python/Python311/Scripts/yt-dlp.exe",
            Path(os.environ.get("LOCALAPPDATA","")) / "Programs/Python/Python310/Scripts/yt-dlp.exe",
            Path(sys.executable).parent / "Scripts/yt-dlp.exe",
        ]
        for c in candidates:
            if c.exists():
                return str(c)
        # Last resort: run via python -m yt_dlp
        return None  # handled specially below

    # 4. Common Windows locations for ffmpeg / ffprobe
    if name in ("ffmpeg", "ffprobe"):
        drive = Path(os.environ.get("SystemDrive", "C:") + "\\")
        search_roots = [
            drive / "ffmpeg",
            drive / "Program Files/ffmpeg",
            drive / "Program Files (x86)/ffmpeg",
            Path(os.environ.get("LOCALAPPDATA","")) / "Microsoft/WinGet/Packages",
            Path(os.environ.get("USERPROFILE","")) / "ffmpeg",
        ]
        for root in search_roots:
            for p in root.rglob(name + ".exe"):
                return str(p)

    raise FileNotFoundError(
        f"Could not find '{name}'. Make sure it is installed and restart PowerShell."
    )

YTDLP  = None
FFMPEG = None
FFPROBE= None

def init_tools():
    global YTDLP, FFMPEG, FFPROBE
    # yt-dlp: prefer exe, fall back to `python -m yt_dlp`
    try:
        YTDLP = find_exe("yt-dlp")
    except FileNotFoundError:
        YTDLP = None  # will use python -m yt_dlp
    try:
        FFMPEG  = find_exe("ffmpeg")
        FFPROBE = find_exe("ffprobe")
    except FileNotFoundError as e:
        print(f"WARNING: {e}")

init_tools()

def js_runtime_args() -> list[str]:
    """yt-dlp needs a JS runtime to solve YouTube's player challenge (EJS).
    Deno is picked up by default; Node must be passed explicitly."""
    args = []
    for rt in ("deno", "node"):
        if shutil.which(rt):
            args += ["--js-runtimes", rt]
    return args

JS_ARGS = js_runtime_args()

# Live cookies from a browser where you are logged in to YouTube (on by default, like the
# original version of this app). Set env var USE_BROWSER_COOKIES=0 to turn it off.
USE_BROWSER_COOKIES = os.environ.get("USE_BROWSER_COOKIES", "1") != "0"

# ── helpers ──────────────────────────────────────────────────────────────────

def set_job(job_id, **kwargs):
    with jobs_lock:
        jobs.setdefault(job_id, {}).update(kwargs)

def get_job(job_id):
    with jobs_lock:
        return dict(jobs.get(job_id, {}))

def run_cmd(cmd: list[str]) -> tuple[int, str]:
    r = subprocess.run(cmd, capture_output=True, text=True,
                       creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=="win32" else 0)
    return r.returncode, r.stdout + r.stderr

def run_cmd_live(cmd: list[str], job_id: str, progress_start: int, progress_end: int):
    """Run command and stream output, updating job progress from yt-dlp % lines."""
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
        creationflags=flags
    )
    output = []
    for line in proc.stdout:
        output.append(line)
        # yt-dlp lines like: [download]  23.4% of 145.23MiB at 2.10MiB/s ETA 01:02
        m = re.search(r'\[download\]\s+([\d.]+)%\s+of\s+([\S]+)', line)
        if m:
            pct_dl = float(m.group(1))
            size   = m.group(2)
            pct    = int(progress_start + (progress_end - progress_start) * pct_dl / 100)
            set_job(job_id, progress=pct,
                    message=f"Downloading video… {pct_dl:.1f}% of {size}")
    proc.wait()
    return proc.returncode, "".join(output)

def ytdlp_cmd(args: list[str]) -> list[str]:
    """Build yt-dlp command, using python -m yt_dlp as fallback."""
    if YTDLP:
        return [YTDLP] + args
    return [sys.executable, "-m", "yt_dlp"] + args

def parse_duration(ffprobe_out: str) -> float:
    m = re.search(r'"duration"\s*:\s*"([\d.]+)"', ffprobe_out)
    if m:
        return float(m.group(1))
    m = re.search(r'Duration:\s*(\d+):(\d+):([\d.]+)', ffprobe_out)
    if m:
        h, mi, s = m.groups()
        return int(h)*3600 + int(mi)*60 + float(s)
    return 600.0  # fallback

def fmt_time(sec: float) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

def generate_clip_plan(duration: float, count: int, style: str) -> list[dict]:
    """Spread clips intelligently across the video."""
    all_templates = {
        "viral":    ["Viral Hook","Peak Moment","Reaction Clip","Comedy Moment","Climax Scene","Teaser"],
        "insights": ["Key Takeaway","Practical Tip","Expert Quote","Data Point","Case Study","Hot Take"],
        "mixed":    ["Scroll-Stop Hook","Viral Moment","Key Insight","Story Peak","Practical Tip","Teaser / Cliffhanger"],
    }
    templates = all_templates.get(style, all_templates["mixed"])

    lengths = [45, 60, 55, 50, 70, 40]  # seconds per clip
    clips = []
    seg = duration / count

    for i in range(count):
        start = max(0, seg * i + seg * 0.1)
        dur   = min(lengths[i % len(lengths)], duration - start - 2)
        dur   = max(10, dur)
        end   = min(start + dur, duration - 1)
        clips.append({
            "num":   i + 1,
            "title": templates[i % len(templates)],
            "start": round(start, 1),
            "end":   round(end, 1),
            "dur":   round(end - start, 1),
            "start_fmt": fmt_time(start),
            "end_fmt":   fmt_time(end),
        })
    return clips

# ── captions / descriptions ──────────────────────────────────────────────────

STOPWORDS = set("""about above after again also always am an and any are because been before being below
between both but can could did does doing down during each even every few for from further get gets
getting going gonna got had has have having her here hers him his how into its just kind know let like
make many more most much must need not now off once one only other our out over own really right said
same say she should since some something such than that thats the their them then there these they thing
things think this those through too under until very want was way well were what when where which while
who whom why will with would yeah yes you your youre actually basically literally maybe people""".split())

def load_meta(job_dir: Path) -> dict:
    f = job_dir / "source.info.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}

def load_transcript(job_dir: Path) -> list[dict]:
    """Read YouTube captions (json3) saved by yt-dlp. Returns [{start,end,text}, ...]."""
    files = sorted(job_dir.glob("source.*.json3"))
    if not files:
        return []
    # prefer the original-language auto track, then plain 'en'
    files.sort(key=lambda p: (0 if "-orig" in p.name else 1, p.name))
    try:
        data = json.loads(files[0].read_text(encoding="utf-8"))
    except Exception:
        return []
    cues = []
    for ev in data.get("events", []):
        segs = ev.get("segs")
        if not segs:
            continue
        text = "".join(sg.get("utf8", "") for sg in segs).replace("\n", " ").strip()
        if not text:
            continue
        start = ev.get("tStartMs", 0) / 1000
        cues.append({"start": start, "end": start + ev.get("dDurationMs", 0) / 1000, "text": text})
    return cues

def clean_caption_text(t: str) -> str:
    t = re.sub(r"\[[^\]]*\]", " ", t)      # [Music], [Applause]
    t = re.sub(r"&gt;&gt;|>>", " ", t)     # speaker-change markers
    return re.sub(r"\s+", " ", t).strip()

def transcript_for(cues: list[dict], start: float, end: float) -> str:
    parts, last = [], None
    for c in cues:
        if start <= c["start"] < end:
            t = clean_caption_text(c["text"])
            if t and t != last:
                parts.append(t)
                last = t
    return " ".join(parts)

def trim_words(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0].rstrip(",;:-") + "…"

def make_hashtags(text: str, fallback_text: str, n: int = 5) -> str:
    freq: dict[str, int] = {}
    for w in re.findall(r"[a-z][a-z']{3,}", (text or fallback_text).lower()):
        if w not in STOPWORDS:
            freq[w] = freq.get(w, 0) + 1
    ranked = sorted(freq, key=lambda w: (-freq[w], w))[:n]
    return " ".join("#" + w.replace("'", "") for w in ranked)

def build_caption(meta: dict, clip: dict, text: str, count: int, url: str) -> dict:
    vtitle   = meta.get("title") or "Video"
    channel  = meta.get("uploader") or meta.get("channel") or ""
    src_url  = meta.get("webpage_url") or url
    # YouTube: title max 100 chars, no < or >
    title = trim_words(f"{vtitle} | Part {clip['num']}/{count}", 100).replace("<", "").replace(">", "")

    lines = []
    if text:
        lines += [trim_words(text, 300), ""]
    lines.append(f'Clip {clip["num"]}/{count} ({clip["start_fmt"]}–{clip["end_fmt"]}) from "{vtitle}"')
    if channel:
        lines.append(f"Credit: {channel}")
    if src_url:
        lines.append(f"Full video: {src_url}")
    tags = make_hashtags(text, vtitle)
    if tags:
        lines += ["", tags]
    return {"yt_title": title, "description": "\n".join(lines), "has_transcript": bool(text)}

# ── background worker ─────────────────────────────────────────────────────────

def process_job(job_id: str, url: str, count: int, style: str, quality: str = "480", local: bool = False):
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(exist_ok=True)

    try:
        video_path = job_dir / "source.mp4"
        if not local:
            # 1. Download video
            set_job(job_id, status="downloading", progress=5, message="Downloading video…")

            # quality: 480p is smoother for most connections, 360p for slow ones
            quality_fmt = {
                "360":  "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360][ext=mp4]/best[height<=360]",
                "480":  "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/best[height<=480]",
                "720":  "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]",
                "1080": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
            }.get(quality, "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/best")

            dl_args_base = JS_ARGS + [
                "-v",                                      # full debug output -> saved to last_error.log on failure
                "-f", quality_fmt,
                "--merge-output-format", "mp4",
                "--no-playlist",
                "--newline",
                "--extractor-retries", "3",
                "--write-subs", "--write-auto-subs",       # captions -> per-clip descriptions
                "--sub-langs", "en,.*-orig",
                "--sub-format", "json3",
                "--write-info-json",                       # title / channel metadata
                "-o", str(video_path),
                url,
            ]

            if not JS_ARGS:
                print("WARNING: no JS runtime (deno/node) found - YouTube downloads will likely fail.")
            if not (FFMPEG and FFPROBE):
                set_job(job_id, status="error", message="ffmpeg/ffprobe not found. Install ffmpeg and restart.")
                return

            # cookies.txt placed next to app.py takes top priority
            cookies_file = Path(__file__).parent / "cookies.txt"

            # Automatic fallback chain, tried per link. Different YouTube "player clients"
            # have different bot-check rules, so if one is blocked the next may pass.
            def client_args(c):
                return ["--extractor-args", f"youtube:player_client={c}"]

            attempts = []
            # 1) cookies read live from your browser (these fail locally and instantly if the
            #    browser isn't usable, so they don't hit YouTube at all)
            if USE_BROWSER_COOKIES:
                for browser in ["edge", "chrome", "firefox"]:
                    attempts.append((f"{browser} browser cookies", ["--cookies-from-browser", browser]))
            # 2) exported cookie file, if present
            if cookies_file.exists():
                attempts.append(("cookies.txt file", ["--cookies", str(cookies_file)]))
            # 3) no cookies, then two alternative player clients. Kept short on purpose:
            #    many rapid failed requests can make YouTube's block last longer.
            attempts.append(("no cookies", []))
            for c in ["tv", "web_safari"]:
                attempts.append((f"{c} client", client_args(c)))

            code, out = -1, ""
            failures, full_log = [], []
            for label, extra_args in attempts:
                if video_path.exists():
                    video_path.unlink()
                set_job(job_id, message=f"Downloading... (trying: {label})")
                code, out = run_cmd_live(ytdlp_cmd(extra_args + dl_args_base), job_id,
                                         progress_start=5, progress_end=28)
                full_log.append(f"===== {label} (exit {code}) =====\n{out}")
                if video_path.exists():
                    break  # success (subtitle warnings can make exit code non-zero)
                lines = out.splitlines()
                err = [l.strip() for l in lines if "ERROR" in l]
                msg = err[-1] if err else out[-200:].strip()
                if "Sign in to confirm" in msg:
                    msg = "blocked by YouTube bot check"
                elif "cookie" in msg.lower() or "decrypt" in msg.lower():
                    pass  # keep full text: this is the line that explains why browser cookies failed
                failures.append(f"[{label}] " + msg[:260])

            if not video_path.exists():
                # save the COMPLETE output of every attempt so nobody has to run anything by hand
                log_file = Path(__file__).parent / "last_error.log"
                try:
                    log_file.write_text("\n\n".join(full_log), encoding="utf-8")
                except Exception:
                    pass
                diag = []
                for l in "\n".join(full_log).splitlines():
                    if any(k in l for k in ("JS runtimes:", "JS Challenge Providers", "playability status", "yt-dlp version")):
                        if l.strip() not in diag:
                            diag.append(l.strip())
                set_job(job_id, status="error",
                        message="Download failed. Full log saved to last_error.log\n\n"
                                + "\n".join(diag[:6]) + "\n\n" + "\n".join(failures))
                return

        # 2. Get duration
        set_job(job_id, status="analyzing", progress=30, message="Analyzing video…")
        _, probe = run_cmd([
            FFPROBE, "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", str(video_path)
        ])
        duration = parse_duration(probe)

        # 3. Build clip plan
        plan = generate_clip_plan(duration, count, style)
        set_job(job_id, clips_plan=plan)

        meta = load_meta(job_dir)
        cues = load_transcript(job_dir)
        if not cues:
            set_job(job_id, message="No captions found - descriptions will be basic.")

        # 4. Cut clips
        clips_done = []
        for i, clip in enumerate(plan):
            pct = 35 + int(60 * i / count)
            set_job(job_id, status="clipping", progress=pct,
                    message=f"Cutting clip {i+1}/{count}: {clip['title']}…")

            out_name = f"clip_{i+1:02d}_{clip['title'].replace(' ','_').replace('/','')}.mp4"
            out_path = job_dir / out_name

            code, ffout = run_cmd([
                FFMPEG, "-y",
                "-ss", str(clip["start"]),
                "-to", str(clip["end"]),
                "-i", str(video_path),
                "-c:v", "libx264", "-crf", "20", "-preset", "fast",
                "-profile:v", "baseline", "-level", "3.1",  # max compatibility
                "-pix_fmt", "yuv420p",                       # smooth playback
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
                "-movflags", "+faststart",                   # instant browser play
                "-avoid_negative_ts", "make_zero",
                str(out_path),
            ])

            # thumbnail for this part: best-looking frame from the first part of the clip
            thumb_name = f"Part_{i+1}_thumbnail.jpg"
            thumb_path = job_dir / thumb_name
            t_at = clip["start"] + clip["dur"] * 0.3
            run_cmd([
                FFMPEG, "-y",
                "-ss", str(round(t_at, 2)),
                "-i", str(video_path),
                "-t", "3",
                "-vf", "thumbnail=60,scale=min(1280\\,iw):-2",   # never upscale
                "-frames:v", "1", "-q:v", "2",
                str(thumb_path),
            ])

            size_mb = round(out_path.stat().st_size / 1024 / 1024, 1) if out_path.exists() else 0
            cap = build_caption(meta, clip, transcript_for(cues, clip["start"], clip["end"]),
                                count, url)
            clips_done.append({
                **clip,
                **cap,
                "filename": out_name,
                "thumb_filename": thumb_name,
                "thumb_ok": thumb_path.exists(),
                "size_mb":  size_mb,
                "ok": out_path.exists() and size_mb > 0,
            })

        # 5. Remove source to save space
        video_path.unlink(missing_ok=True)

        set_job(job_id, status="done", progress=100,
                message=f"Done! {count} clips ready.", clips=clips_done)

    except Exception as e:
        set_job(job_id, status="error", message=str(e))

# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/start", methods=["POST"])
def api_start():
    data    = request.json or {}
    url     = (data.get("url") or "").strip()
    count   = max(3, min(10, int(data.get("count", 6))))
    style   = data.get("style", "mixed")
    quality = data.get("quality", "480")

    if not url:
        return jsonify(error="No URL provided"), 400

    job_id = uuid.uuid4().hex
    set_job(job_id, status="queued", progress=0, message="Starting…", clips=[])
    t = threading.Thread(target=process_job, args=(job_id, url, count, style, quality), daemon=True)
    t.start()
    return jsonify(job_id=job_id)

@app.route("/api/upload", methods=["POST"])
def api_upload():
    """Clip a video file from your own disk (no YouTube involved)."""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="No file uploaded"), 400
    try:
        count = max(3, min(10, int(request.form.get("count", 6))))
    except ValueError:
        count = 6
    style = request.form.get("style", "mixed")

    job_id  = uuid.uuid4().hex
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(exist_ok=True)
    f.save(job_dir / "source.mp4")   # ffmpeg detects the real format from the content
    (job_dir / "source.info.json").write_text(
        json.dumps({"title": Path(f.filename).stem}), encoding="utf-8")

    set_job(job_id, status="queued", progress=0, message="Starting…", clips=[])
    threading.Thread(target=process_job, args=(job_id, "", count, style, "480", True),
                     daemon=True).start()
    return jsonify(job_id=job_id)

@app.route("/api/status/<job_id>")
def api_status(job_id):
    j = get_job(job_id)
    if not j:
        return jsonify(error="Unknown job"), 404
    return jsonify(j)

@app.route("/api/download/<job_id>/<filename>")
def api_download(job_id, filename):
    # Basic safety: no path traversal
    if ".." in filename or "/" in filename:
        return "Bad filename", 400
    path = WORK_DIR / job_id / filename
    if not path.exists():
        return "File not found", 404
    return send_file(path, as_attachment=True, download_name=filename)

@app.route("/api/jobs")
def api_jobs():
    with jobs_lock:
        return jsonify(list(jobs.keys()))

if __name__ == "__main__":
    # Auto-update yt-dlp on startup (fixes bot detection & JS runtime issues)
    print("🔄  Updating yt-dlp to latest version…")
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp[default]", "-q"],
        capture_output=True, text=True
    )
    # Re-init tools after upgrade
    init_tools()
    _, ver = run_cmd(ytdlp_cmd(["--version"]))
    print(f"yt-dlp version: {ver.strip()}   JS runtime args: {JS_ARGS or 'NONE FOUND'}")
    print("✅  yt-dlp ready" + ("" if JS_ARGS else "  (WARNING: no deno/node found)"))
    print("\n🎬  YT Clipper running at  http://localhost:5000\n")
    app.run(debug=False, port=5000)