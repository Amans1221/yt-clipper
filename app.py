"""
YT Clipper — local web UI
Run:  python app.py
Open: http://localhost:5000

Optional env vars:
  CLIPS_DIR=D:\clips_output   -> store downloads/clips on another drive
  USE_BROWSER_COOKIES=0       -> disable browser cookie fallback
"""

import os, sys, json, uuid, threading, subprocess, re, shutil
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 ** 3   # allow uploads up to 4 GB

# Change the default path or set CLIPS_DIR env var if C: is low on space
WORK_DIR = Path(os.environ.get("CLIPS_DIR", "clips_output"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

MIN_FREE_GB = 1.0  # minimum free space required before starting a job

# Job store: job_id -> {status, progress, clips, error}
jobs: dict = {}
jobs_lock = threading.Lock()
ACTIVE_STATUSES = {"queued", "downloading", "analyzing", "clipping"}

# ── find executables (Windows-safe) ──────────────────────────────────────────

def find_exe(name: str) -> str:
    """Find an executable, checking common Windows install locations."""
    found = shutil.which(name)
    if found:
        return found

    local = Path(__file__).parent / (name + (".exe" if sys.platform == "win32" else ""))
    if local.exists():
        return str(local)

    if name == "yt-dlp":
        candidates = [
            Path(os.environ.get("APPDATA", "")) / "Python/Scripts/yt-dlp.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Python/pythoncore-3.15-64/Scripts/yt-dlp.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Python/Python312/Scripts/yt-dlp.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Python/Python311/Scripts/yt-dlp.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Python/Python310/Scripts/yt-dlp.exe",
            Path(sys.executable).parent / "Scripts/yt-dlp.exe",
        ]
        for c in candidates:
            if c.exists():
                return str(c)
        return None  # fall back to `python -m yt_dlp`

    if name in ("ffmpeg", "ffprobe"):
        drive = Path(os.environ.get("SystemDrive", "C:") + "\\")
        search_roots = [
            drive / "ffmpeg",
            drive / "Program Files/ffmpeg",
            drive / "Program Files (x86)/ffmpeg",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Packages",
            Path(os.environ.get("USERPROFILE", "")) / "ffmpeg",
        ]
        for root in search_roots:
            if root.exists():
                for p in root.rglob(name + ".exe"):
                    return str(p)

    raise FileNotFoundError(
        f"Could not find '{name}'. Make sure it is installed and restart PowerShell."
    )

YTDLP = None
FFMPEG = None
FFPROBE = None

def init_tools():
    global YTDLP, FFMPEG, FFPROBE
    try:
        YTDLP = find_exe("yt-dlp")
    except FileNotFoundError:
        YTDLP = None
    try:
        FFMPEG = find_exe("ffmpeg")
        FFPROBE = find_exe("ffprobe")
    except FileNotFoundError as e:
        print(f"WARNING: {e}")

init_tools()

def js_runtime_args() -> list[str]:
    """yt-dlp needs a JS runtime to solve YouTube's player challenge (EJS)."""
    args = []
    for rt in ("deno", "node"):
        if shutil.which(rt):
            args += ["--js-runtimes", rt]
    return args

JS_ARGS = js_runtime_args()
USE_BROWSER_COOKIES = os.environ.get("USE_BROWSER_COOKIES", "1") != "0"

# ── disk & job helpers ───────────────────────────────────────────────────────

def free_space_gb() -> float:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(WORK_DIR).free / (1024 ** 3)

def folder_size_bytes(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total

def purge_source_files(job_dir: Path):
    """Remove source video and partial download files inside a job folder."""
    for p in job_dir.glob("source.*"):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass

def cleanup_stale_partials():
    """On startup, remove leftover partial downloads or empty job folders from crashed runs."""
    if not WORK_DIR.exists():
        return
    for d in WORK_DIR.iterdir():
        if not d.is_dir():
            continue
        purge_source_files(d)
        # If no rendered clips exist in this folder, remove the whole folder
        if not any(d.glob("clip_*.mp4")):
            shutil.rmtree(d, ignore_errors=True)

def set_job(job_id, **kwargs):
    with jobs_lock:
        jobs.setdefault(job_id, {}).update(kwargs)

def get_job(job_id):
    with jobs_lock:
        return dict(jobs.get(job_id, {}))

def run_cmd(cmd: list[str]) -> tuple[int, str]:
    r = subprocess.run(
        cmd, capture_output=True, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    )
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
        m = re.search(r'\[download\]\s+([\d.]+)%\s+of\s+([\S]+)', line)
        if m:
            pct_dl = float(m.group(1))
            size = m.group(2)
            pct = int(progress_start + (progress_end - progress_start) * pct_dl / 100)
            set_job(job_id, progress=pct,
                    message=f"Downloading video… {pct_dl:.1f}% of {size}")
    proc.wait()
    return proc.returncode, "".join(output)

def ytdlp_cmd(args: list[str]) -> list[str]:
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
        return int(h) * 3600 + int(mi) * 60 + float(s)
    return 600.0

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
        "viral":    ["Viral Hook", "Peak Moment", "Reaction Clip", "Comedy Moment", "Climax Scene", "Teaser"],
        "insights": ["Key Takeaway", "Practical Tip", "Expert Quote", "Data Point", "Case Study", "Hot Take"],
        "mixed":    ["Scroll-Stop Hook", "Viral Moment", "Key Insight", "Story Peak", "Practical Tip", "Teaser / Cliffhanger"],
    }
    templates = all_templates.get(style, all_templates["mixed"])

    lengths = [45, 60, 55, 50, 70, 40]
    clips = []
    seg = duration / count

    for i in range(count):
        start = max(0, seg * i + seg * 0.1)
        dur = min(lengths[i % len(lengths)], duration - start - 2)
        dur = max(10, dur)
        end = min(start + dur, duration - 1)
        clips.append({
            "num": i + 1,
            "title": templates[i % len(templates)],
            "start": round(start, 1),
            "end": round(end, 1),
            "dur": round(end - start, 1),
            "start_fmt": fmt_time(start),
            "end_fmt": fmt_time(end),
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
    files = sorted(job_dir.glob("source.*.json3"))
    if not files:
        return []
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
    t = re.sub(r"\[[^\]]*\]", " ", t)
    t = re.sub(r"&gt;&gt;|>>", " ", t)
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
    vtitle = meta.get("title") or "Video"
    channel = meta.get("uploader") or meta.get("channel") or ""
    src_url = meta.get("webpage_url") or url
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
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Pre-flight disk space check
        free_gb = free_space_gb()
        if free_gb < MIN_FREE_GB:
            shutil.rmtree(job_dir, ignore_errors=True)
            set_job(
                job_id, status="error",
                message=(
                    f"Low disk space: only {free_gb:.2f} GB free in {WORK_DIR.resolve()}.\n"
                    f"Click 'Clear old clips' in the top bar, free up at least {MIN_FREE_GB:.0f} GB, "
                    f"or set CLIPS_DIR to another drive."
                )
            )
            return

        if not (FFMPEG and FFPROBE):
            shutil.rmtree(job_dir, ignore_errors=True)
            set_job(job_id, status="error", message="ffmpeg/ffprobe not found. Install ffmpeg and restart.")
            return

        video_path = job_dir / "source.mp4"
        if not local:
            if not JS_ARGS:
                print("WARNING: no JS runtime (deno/node) found - YouTube downloads will likely fail.")

            # 1a. Download video + metadata ONLY (subtitles fetched separately so 429s never abort downloads)
            set_job(job_id, status="downloading", progress=5, message="Downloading video…")

            quality_fmt = {
                "360":  "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best[height<=360]/best",
                "480":  "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best[height<=480]/best",
                "720":  "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]/best",
                "1080": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
            }.get(quality, "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best")

            ffmpeg_args = ["--ffmpeg-location", str(Path(FFMPEG).parent)] if FFMPEG else []

            dl_args_base = JS_ARGS + ffmpeg_args + [
                "-v",
                "-f", quality_fmt,
                "--merge-output-format", "mp4",
                "--remux-video", "mp4",
                "--no-playlist",
                "--newline",
                "--extractor-retries", "3",
                "--write-info-json",
                "-o", str(video_path),
                url,
            ]

            cookies_file = Path(__file__).parent / "cookies.txt"

            def client_args(c):
                return ["--extractor-args", f"youtube:player_client={c}"]

            attempts = []
            if cookies_file.exists():
                attempts.append(("cookies.txt file", ["--cookies", str(cookies_file)]))
            attempts.append(("no cookies", []))
            for c in ["android_vr", "web_safari", "tv"]:
                attempts.append((f"{c} client", client_args(c)))
            if USE_BROWSER_COOKIES:
                for browser in ["firefox", "edge", "chrome"]:
                    attempts.append((f"{browser} browser cookies", ["--cookies-from-browser", browser]))

            code, out = -1, ""
            failures, full_log = [], []
            winning_args = []
            disk_full = False

            for label, extra_args in attempts:
                purge_source_files(job_dir)
                set_job(job_id, message=f"Downloading… (trying: {label})")
                code, out = run_cmd_live(
                    ytdlp_cmd(extra_args + dl_args_base), job_id,
                    progress_start=5, progress_end=28
                )
                full_log.append(f"===== {label} (exit {code}) =====\n{out}")

                if video_path.exists():
                    winning_args = extra_args
                    break

                # Stop immediately if the disk ran out of space
                if "No space left on device" in out or "Errno 28" in out:
                    disk_full = True
                    failures.append(f"[{label}] No space left on device ({WORK_DIR.resolve()})")
                    break

                lines = out.splitlines()
                err = [l.strip() for l in lines if "ERROR" in l]
                msg = err[-1] if err else out[-200:].strip()
                if "Sign in to confirm" in msg:
                    msg = "blocked by YouTube bot check"
                failures.append(f"[{label}] " + msg[:260])

            if not video_path.exists():
                shutil.rmtree(job_dir, ignore_errors=True)
                if disk_full:
                    set_job(
                        job_id, status="error",
                        message=(
                            f"Download stopped: drive ran out of space ({free_space_gb():.2f} GB free in {WORK_DIR.resolve()}).\n"
                            f"Click 'Clear old clips', free up disk space, or set CLIPS_DIR to another drive."
                        )
                    )
                    return

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
                set_job(
                    job_id, status="error",
                    message="Download failed. Full log saved to last_error.log\n\n"
                            + "\n".join(diag[:6]) + "\n\n" + "\n".join(failures)
                )
                return

            # 1b. Best-effort subtitle fetch (non-fatal if YouTube returns HTTP 429)
            set_job(job_id, progress=29, message="Fetching captions (optional)…")
            sub_args = winning_args + JS_ARGS + [
                "--skip-download",
                "--no-playlist",
                "--write-subs", "--write-auto-subs",
                "--sub-langs", "en,.*-orig",
                "--sub-format", "json3",
                "--sleep-subtitles", "2",
                "--ignore-errors",
                "-o", str(video_path),
                url,
            ]
            run_cmd(ytdlp_cmd(sub_args))

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

            out_name = f"clip_{i+1:02d}_{clip['title'].replace(' ', '_').replace('/', '')}.mp4"
            out_path = job_dir / out_name

            code, ffout = run_cmd([
                FFMPEG, "-y",
                "-ss", str(clip["start"]),
                "-to", str(clip["end"]),
                "-i", str(video_path),
                "-c:v", "libx264", "-crf", "20", "-preset", "fast",
                "-profile:v", "baseline", "-level", "3.1",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
                "-movflags", "+faststart",
                "-avoid_negative_ts", "make_zero",
                str(out_path),
            ])

            if code != 0 and ("No space left on device" in ffout or "Errno 28" in ffout):
                shutil.rmtree(job_dir, ignore_errors=True)
                set_job(job_id, status="error",
                        message=f"Clipping stopped: disk ran out of space in {WORK_DIR.resolve()}.")
                return

            thumb_name = f"Part_{i+1}_thumbnail.jpg"
            thumb_path = job_dir / thumb_name
            t_at = clip["start"] + clip["dur"] * 0.3
            run_cmd([
                FFMPEG, "-y",
                "-ss", str(round(t_at, 2)),
                "-i", str(video_path),
                "-t", "3",
                "-vf", "thumbnail=60,scale=min(1280\\,iw):-2",
                "-frames:v", "1", "-q:v", "2",
                str(thumb_path),
            ])

            size_mb = round(out_path.stat().st_size / 1024 / 1024, 1) if out_path.exists() else 0
            cap = build_caption(
                meta, clip, transcript_for(cues, clip["start"], clip["end"]),
                count, url
            )
            clips_done.append({
                **clip,
                **cap,
                "filename": out_name,
                "thumb_filename": thumb_name,
                "thumb_ok": thumb_path.exists(),
                "size_mb": size_mb,
                "ok": out_path.exists() and size_mb > 0,
            })

        # 5. Remove source & temp metadata to save space
        purge_source_files(job_dir)

        set_job(job_id, status="done", progress=100,
                message=f"Done! {count} clips ready.", clips=clips_done)

    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        set_job(job_id, status="error", message=str(e))

# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/disk")
def api_disk():
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    folders = [d for d in WORK_DIR.iterdir() if d.is_dir()]
    used_mb = round(sum(folder_size_bytes(d) for d in folders) / (1024 ** 2), 1)
    return jsonify(
        free_gb=round(free_space_gb(), 2),
        used_mb=used_mb,
        folders=len(folders),
        work_dir=str(WORK_DIR.resolve()),
    )

@app.route("/api/cleanup", methods=["POST"])
def api_cleanup():
    """Delete all finished/failed clip folders in WORK_DIR (keeps active jobs untouched)."""
    with jobs_lock:
        active_ids = {jid for jid, j in jobs.items() if j.get("status") in ACTIVE_STATUSES}

    freed_bytes = 0
    removed = 0
    if WORK_DIR.exists():
        for d in WORK_DIR.iterdir():
            if d.is_dir() and d.name not in active_ids:
                freed_bytes += folder_size_bytes(d)
                shutil.rmtree(d, ignore_errors=True)
                removed += 1

    return jsonify(
        removed=removed,
        freed_mb=round(freed_bytes / (1024 ** 2), 1),
        free_gb=round(free_space_gb(), 2),
    )

@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.json or {}
    url = (data.get("url") or "").strip()
    count = max(3, min(10, int(data.get("count", 6))))
    style = data.get("style", "mixed")
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
    if free_space_gb() < MIN_FREE_GB:
        return jsonify(
            error=f"Low disk space ({free_space_gb():.2f} GB free). Clear old clips or free up space first."
        ), 507

    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="No file uploaded"), 400
    try:
        count = max(3, min(10, int(request.form.get("count", 6))))
    except ValueError:
        count = 6
    style = request.form.get("style", "mixed")

    job_id = uuid.uuid4().hex
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        f.save(job_dir / "source.mp4")
        (job_dir / "source.info.json").write_text(
            json.dumps({"title": Path(f.filename).stem}), encoding="utf-8"
        )
    except OSError as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify(error=f"Could not save uploaded file: {e}"), 507

    set_job(job_id, status="queued", progress=0, message="Starting…", clips=[])
    threading.Thread(
        target=process_job, args=(job_id, "", count, style, "480", True),
        daemon=True
    ).start()
    return jsonify(job_id=job_id)

@app.route("/api/status/<job_id>")
def api_status(job_id):
    j = get_job(job_id)
    if not j:
        return jsonify(error="Unknown job"), 404
    return jsonify(j)

@app.route("/api/download/<job_id>/<filename>")
def api_download(job_id, filename):
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
    cleanup_stale_partials()
    print("🔄  Updating yt-dlp to latest version…")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp[default]", "-q"],
        capture_output=True, text=True
    )
    init_tools()
    _, ver = run_cmd(ytdlp_cmd(["--version"]))
    print(f"yt-dlp version: {ver.strip()}   JS runtime args: {JS_ARGS or 'NONE FOUND'}")
    print(f"Storage folder: {WORK_DIR.resolve()}  ({free_space_gb():.2f} GB free)")
    print("✅  yt-dlp ready" + ("" if JS_ARGS else "  (WARNING: no deno/node found)"))
    print("\n🎬  YT Clipper running at  http://localhost:5000\n")
    app.run(debug=False, port=5000, host="0.0.0.0")