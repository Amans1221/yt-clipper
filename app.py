"""
YT Clipper — web UI (local + Render)
Run:  python app.py  →  http://localhost:5000

Env vars:
  GROQ_API_KEY      -> AI-powered captions (get free key at console.groq.com)
  CLIPS_DIR         -> storage folder (default: clips_output)
  USE_BROWSER_COOKIES=0  -> disable live browser cookie attempts
  YOUTUBE_COOKIES   -> paste cookies.txt content here for Render deploys
  MIN_FREE_GB       -> minimum free space before rejecting jobs (default 1.0)
"""

import os, sys, json, uuid, threading, subprocess, re, shutil, time
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 ** 3

WORK_DIR    = Path(os.environ.get("CLIPS_DIR", "clips_output"))
WORK_DIR.mkdir(parents=True, exist_ok=True)
MIN_FREE_GB = float(os.environ.get("MIN_FREE_GB", "1.0"))
GROQ_KEY    = os.environ.get("GROQ_API_KEY", "").strip()

jobs: dict  = {}
jobs_lock   = threading.Lock()
ACTIVE_STATUSES = {"queued", "downloading", "analyzing", "clipping"}

# ── executables ───────────────────────────────────────────────────────────────

def find_exe(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    local = Path(__file__).parent / (name + (".exe" if sys.platform == "win32" else ""))
    if local.exists():
        return str(local)
    if name == "yt-dlp":
        candidates = [
            Path(os.environ.get("APPDATA",      "")) / "Python/Scripts/yt-dlp.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Python/pythoncore-3.15-64/Scripts/yt-dlp.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Python/Python312/Scripts/yt-dlp.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Python/Python311/Scripts/yt-dlp.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Python/Python310/Scripts/yt-dlp.exe",
            Path(sys.executable).parent / "Scripts/yt-dlp.exe",
        ]
        for c in candidates:
            if c.exists():
                return str(c)
        return None
    if name in ("ffmpeg", "ffprobe"):
        drive = Path(os.environ.get("SystemDrive", "C:") + "\\")
        for root in [
            drive / "ffmpeg", drive / "Program Files/ffmpeg",
            drive / "Program Files (x86)/ffmpeg",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Packages",
            Path(os.environ.get("USERPROFILE",  "")) / "ffmpeg",
        ]:
            if root.exists():
                for p in root.rglob(name + ".exe"):
                    return str(p)
    raise FileNotFoundError(f"Could not find '{name}'.")

YTDLP = FFMPEG = FFPROBE = None

def init_tools():
    global YTDLP, FFMPEG, FFPROBE
    try:
        YTDLP = find_exe("yt-dlp")
    except FileNotFoundError:
        YTDLP = None
    try:
        FFMPEG  = find_exe("ffmpeg")
        FFPROBE = find_exe("ffprobe")
    except FileNotFoundError as e:
        print(f"WARNING: {e}")

init_tools()

# ── cookies from env (Render / production) ────────────────────────────────────
COOKIES_FILE = Path(__file__).parent / "cookies.txt"
_cookies_env = os.environ.get("YOUTUBE_COOKIES", "").strip()
if _cookies_env:
    try:
        COOKIES_FILE.write_text(_cookies_env, encoding="utf-8")
        print("cookies.txt written from YOUTUBE_COOKIES env var")
    except Exception as _e:
        print(f"WARNING: could not write cookies.txt: {_e}")

def js_runtime_args() -> list[str]:
    args = []
    for rt in ("deno", "node"):
        if shutil.which(rt):
            args += ["--js-runtimes", rt]
    return args

JS_ARGS            = js_runtime_args()
USE_BROWSER_COOKIES = os.environ.get("USE_BROWSER_COOKIES", "1") != "0"

# ── helpers ───────────────────────────────────────────────────────────────────

def free_space_gb() -> float:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(WORK_DIR).free / (1024 ** 3)

def folder_size_bytes(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try: total += p.stat().st_size
            except OSError: pass
    return total

def purge_source_files(job_dir: Path):
    for p in job_dir.glob("source.*"):
        try: p.unlink(missing_ok=True)
        except OSError: pass

def cleanup_stale_partials():
    if not WORK_DIR.exists(): return
    for d in WORK_DIR.iterdir():
        if not d.is_dir(): continue
        purge_source_files(d)
        if not any(d.glob("clip_*.mp4")):
            shutil.rmtree(d, ignore_errors=True)

def set_job(job_id, **kwargs):
    with jobs_lock:
        jobs.setdefault(job_id, {}).update(kwargs)

def get_job(job_id):
    with jobs_lock:
        return dict(jobs.get(job_id, {}))

def run_cmd(cmd: list[str]) -> tuple[int, str]:
    r = subprocess.run(cmd, capture_output=True, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    return r.returncode, r.stdout + r.stderr

def run_cmd_live(cmd, job_id, progress_start, progress_end):
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc  = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, bufsize=1, creationflags=flags)
    output = []
    for line in proc.stdout:
        output.append(line)
        m = re.search(r'\[download\]\s+([\d.]+)%\s+of\s+([\S]+)', line)
        if m:
            pct_dl = float(m.group(1)); size = m.group(2)
            pct = int(progress_start + (progress_end - progress_start) * pct_dl / 100)
            set_job(job_id, progress=pct, message=f"Downloading… {pct_dl:.1f}% of {size}")
    proc.wait()
    return proc.returncode, "".join(output)

def ytdlp_cmd(args):
    if YTDLP: return [YTDLP] + args
    return [sys.executable, "-m", "yt_dlp"] + args

def parse_duration(ffprobe_out: str) -> float:
    m = re.search(r'"duration"\s*:\s*"([\d.]+)"', ffprobe_out)
    if m: return float(m.group(1))
    m = re.search(r'Duration:\s*(\d+):(\d+):([\d.]+)', ffprobe_out)
    if m:
        h, mi, s = m.groups()
        return int(h)*3600 + int(mi)*60 + float(s)
    return 600.0

def fmt_time(sec: float) -> str:
    sec = int(sec); h, rem = divmod(sec, 3600); m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

# ── clip plan ─────────────────────────────────────────────────────────────────

def generate_clip_plan(duration: float, count: int, style: str,
                       clip_duration: int = 60) -> list[dict]:
    all_templates = {
        "viral":    ["Viral Hook","Peak Moment","Reaction Clip","Comedy Moment","Climax Scene","Teaser"],
        "insights": ["Key Takeaway","Practical Tip","Expert Quote","Data Point","Case Study","Hot Take"],
        "mixed":    ["Scroll-Stop Hook","Viral Moment","Key Insight","Story Peak","Practical Tip","Teaser / Cliffhanger"],
    }
    templates = all_templates.get(style, all_templates["mixed"])
    clips = []
    seg   = duration / count
    for i in range(count):
        start = max(0, seg * i + seg * 0.1)
        dur   = min(clip_duration, duration - start - 2)
        dur   = max(10, dur)
        end   = min(start + dur, duration - 1)
        clips.append({
            "num":        i + 1,
            "title":      templates[i % len(templates)],
            "start":      round(start, 1),
            "end":        round(end,   1),
            "dur":        round(end - start, 1),
            "start_fmt":  fmt_time(start),
            "end_fmt":    fmt_time(end),
        })
    return clips

# ── AI captions (Groq) ────────────────────────────────────────────────────────

def groq_chat(prompt: str, system: str = "", max_tokens: int = 400) -> str:
    """Call Groq API; returns text or raises RuntimeError."""
    import urllib.request
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body = json.dumps({
        "model":      "llama3-8b-8192",
        "messages":   messages,
        "max_tokens": max_tokens,
        "temperature": 0.8,
    }).encode()
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {GROQ_KEY}",
            "Content-Type":  "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"].strip()

def build_caption_ai(meta: dict, clip: dict, transcript: str,
                     count: int, url: str, orientation: str) -> dict:
    """Generate attractive caption using Groq; fall back to keyword method on error."""
    vtitle  = meta.get("title") or "Video"
    channel = meta.get("uploader") or meta.get("channel") or ""
    src_url = meta.get("webpage_url") or url
    fmt_lbl = "YouTube Short (vertical)" if orientation == "vertical" else "YouTube / social video"

    if GROQ_KEY and transcript:
        system = (
            "You are a social media expert who writes viral, engaging captions. "
            "Be concise, punchy, and use emojis sparingly but effectively. "
            "Always respond with valid JSON only — no markdown fences, no extra text."
        )
        prompt = f"""
Video title: {vtitle}
Clip {clip['num']} of {count} ({clip['start_fmt']} to {clip['end_fmt']})
Format: {fmt_lbl}
Transcript excerpt: {transcript[:600]}

Write a caption package for this clip. Return ONLY this JSON:
{{
  "title": "compelling title under 90 chars",
  "hook": "first line that stops the scroll (1 sentence)",
  "description": "2-3 sentence description with key insight from the clip",
  "hashtags": "#tag1 #tag2 #tag3 #tag4 #tag5"
}}
"""
        try:
            raw  = groq_chat(prompt, system, max_tokens=300)
            # strip any accidental markdown fences
            raw  = re.sub(r"```[a-z]*", "", raw).strip().strip("`")
            data = json.loads(raw)
            title = (data.get("title") or vtitle)[:100].replace("<","").replace(">","")
            hook  = data.get("hook", "")
            desc  = data.get("description", "")
            tags  = data.get("hashtags", "")
            lines = []
            if hook:   lines.append(hook)
            if desc:   lines += ["", desc]
            lines += ["",
                      f'Clip {clip["num"]}/{count} ({clip["start_fmt"]}–{clip["end_fmt"]}) from "{vtitle}"']
            if channel: lines.append(f"Credit: {channel}")
            if src_url: lines.append(f"Full video: {src_url}")
            if tags:    lines += ["", tags]
            return {
                "yt_title":      title,
                "description":   "\n".join(lines),
                "has_transcript": True,
                "ai_powered":    True,
            }
        except Exception as e:
            print(f"Groq caption failed for clip {clip['num']}: {e} — falling back")

    # ── keyword fallback ──────────────────────────────────────────────────────
    return build_caption_basic(meta, clip, transcript, count, url)

STOPWORDS = set("""about above after again also always am an and any are because been before being below
between both but can could did does doing down during each even every few for from further get gets
getting going gonna got had has have having her here hers him his how into its just kind know let like
make many more most much must need not now off once one only other our out over own really right said
same say she should since some something such than that thats the their them then there these they thing
things think this those through too under until very want was way well were what when where which while
who whom why will with would yeah yes you your youre actually basically literally maybe people""".split())

def _make_hashtags(text: str, fallback: str, n: int = 5) -> str:
    freq: dict[str, int] = {}
    for w in re.findall(r"[a-z][a-z']{3,}", (text or fallback).lower()):
        if w not in STOPWORDS:
            freq[w] = freq.get(w, 0) + 1
    ranked = sorted(freq, key=lambda w: (-freq[w], w))[:n]
    return " ".join("#" + w.replace("'","") for w in ranked)

def _trim(text: str, n: int) -> str:
    text = text.strip()
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0].rstrip(",;:-") + "…"

def build_caption_basic(meta, clip, text, count, url) -> dict:
    vtitle  = meta.get("title") or "Video"
    channel = meta.get("uploader") or meta.get("channel") or ""
    src_url = meta.get("webpage_url") or url
    title   = _trim(f"{vtitle} | Part {clip['num']}/{count}", 100).replace("<","").replace(">","")
    lines   = []
    if text: lines += [_trim(text, 300), ""]
    lines.append(f'Clip {clip["num"]}/{count} ({clip["start_fmt"]}–{clip["end_fmt"]}) from "{vtitle}"')
    if channel: lines.append(f"Credit: {channel}")
    if src_url: lines.append(f"Full video: {src_url}")
    tags = _make_hashtags(text, vtitle)
    if tags: lines += ["", tags]
    return {"yt_title": title, "description": "\n".join(lines),
            "has_transcript": bool(text), "ai_powered": False}

# ── transcript helpers ────────────────────────────────────────────────────────

def load_meta(job_dir: Path) -> dict:
    try: return json.loads((job_dir / "source.info.json").read_text(encoding="utf-8"))
    except Exception: return {}

def load_transcript(job_dir: Path) -> list[dict]:
    files = sorted(job_dir.glob("source.*.json3"))
    if not files: return []
    files.sort(key=lambda p: (0 if "-orig" in p.name else 1, p.name))
    try: data = json.loads(files[0].read_text(encoding="utf-8"))
    except Exception: return []
    cues = []
    for ev in data.get("events", []):
        segs = ev.get("segs")
        if not segs: continue
        text = "".join(sg.get("utf8","") for sg in segs).replace("\n"," ").strip()
        if not text: continue
        start = ev.get("tStartMs", 0) / 1000
        cues.append({"start": start, "end": start + ev.get("dDurationMs",0)/1000, "text": text})
    return cues

def _clean(t: str) -> str:
    t = re.sub(r"\[[^\]]*\]", " ", t)
    t = re.sub(r"&gt;&gt;|>>", " ", t)
    return re.sub(r"\s+", " ", t).strip()

def transcript_for(cues, start, end) -> str:
    parts, last = [], None
    for c in cues:
        if start <= c["start"] < end:
            t = _clean(c["text"])
            if t and t != last:
                parts.append(t); last = t
    return " ".join(parts)

# ── ffmpeg helpers ────────────────────────────────────────────────────────────

def video_filter(orientation: str, quality: str) -> str:
    """Return -vf value for the requested orientation."""
    if orientation == "vertical":
        # crop centre of frame to 9:16 then scale to 1080×1920
        return "crop=in_h*9/16:in_h:(in_w-in_h*9/16)/2:0,scale=1080:1920"
    # horizontal — just scale to requested height, keep aspect
    h = {"360": 360, "480": 480, "720": 720, "1080": 1080}.get(quality, 480)
    return f"scale=-2:{h}"

# ── background worker ─────────────────────────────────────────────────────────

def process_job(job_id: str, url: str, count: int, style: str,
                quality: str = "480", local: bool = False,
                orientation: str = "horizontal", clip_duration: int = 60):
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        if free_space_gb() < MIN_FREE_GB:
            shutil.rmtree(job_dir, ignore_errors=True)
            set_job(job_id, status="error",
                    message=f"Low disk space: {free_space_gb():.2f} GB free."); return

        if not (FFMPEG and FFPROBE):
            shutil.rmtree(job_dir, ignore_errors=True)
            set_job(job_id, status="error",
                    message="ffmpeg/ffprobe not found. Install ffmpeg and restart."); return

        video_path = job_dir / "source.mp4"

        if not local:
            set_job(job_id, status="downloading", progress=5, message="Downloading video…")
            quality_fmt = {
                "360":  "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best[height<=360]/best",
                "480":  "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best[height<=480]/best",
                "720":  "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]/best",
                "1080": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
            }.get(quality, "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best")

            ffmpeg_location = ["--ffmpeg-location", str(Path(FFMPEG).parent)] if FFMPEG else []
            dl_args_base = JS_ARGS + ffmpeg_location + [
                "-v", "-f", quality_fmt,
                "--merge-output-format", "mp4", "--remux-video", "mp4",
                "--no-playlist", "--newline", "--extractor-retries", "3",
                "--write-info-json", "-o", str(video_path), url,
            ]

            def client_args(c): return ["--extractor-args", f"youtube:player_client={c}"]

            attempts = []
            if COOKIES_FILE.exists():
                attempts.append(("cookies (saved)", ["--cookies", str(COOKIES_FILE)]))
            if USE_BROWSER_COOKIES:
                for b in ["edge", "chrome", "firefox"]:
                    attempts.append((f"{b} browser", [f"--cookies-from-browser", b]))
            attempts.append(("no cookies", []))
            for c in ["android_vr", "web_safari", "tv"]:
                attempts.append((f"{c} client", client_args(c)))

            failures, full_log, winning_args = [], [], []
            for label, extra in attempts:
                purge_source_files(job_dir)
                set_job(job_id, message=f"Downloading… (trying: {label})")
                code, out = run_cmd_live(ytdlp_cmd(extra + dl_args_base), job_id, 5, 28)
                full_log.append(f"===== {label} (exit {code}) =====\n{out}")
                if video_path.exists():
                    winning_args = extra; break
                lines = out.splitlines()
                err   = [l.strip() for l in lines if "ERROR" in l]
                msg   = err[-1] if err else out[-200:].strip()
                if "Sign in to confirm" in msg: msg = "blocked by YouTube bot check"
                failures.append(f"[{label}] " + msg[:260])

            if not video_path.exists():
                shutil.rmtree(job_dir, ignore_errors=True)
                try:
                    (Path(__file__).parent / "last_error.log").write_text(
                        "\n\n".join(full_log), encoding="utf-8")
                except Exception: pass
                diag = []
                for l in "\n".join(full_log).splitlines():
                    if any(k in l for k in ("JS runtimes:","JS Challenge","playability status","yt-dlp version")):
                        if l.strip() not in diag: diag.append(l.strip())
                hint = "" if COOKIES_FILE.exists() else \
                    "\n\n💡 Paste your YouTube cookies via ⚙ Cookies in the top bar."
                set_job(job_id, status="error",
                        message="Download failed. Full log → last_error.log\n\n"
                                + "\n".join(diag[:6]) + "\n\n"
                                + "\n".join(failures) + hint)
                return

            set_job(job_id, progress=29, message="Fetching captions…")
            run_cmd(ytdlp_cmd(winning_args + JS_ARGS + [
                "--skip-download", "--no-playlist",
                "--write-subs", "--write-auto-subs",
                "--sub-langs", "en,.*-orig", "--sub-format", "json3",
                "--sleep-subtitles", "2", "--ignore-errors",
                "-o", str(video_path), url,
            ]))

        # 2. analyse
        set_job(job_id, status="analyzing", progress=30, message="Analysing video…")
        _, probe = run_cmd([FFPROBE, "-v", "quiet", "-print_format", "json",
                            "-show_format", "-show_streams", str(video_path)])
        duration = parse_duration(probe)

        # 3. plan
        plan = generate_clip_plan(duration, count, style, clip_duration)
        set_job(job_id, clips_plan=plan)
        meta  = load_meta(job_dir)
        cues  = load_transcript(job_dir)
        vf    = video_filter(orientation, quality)

        # 4. cut
        clips_done = []
        for i, clip in enumerate(plan):
            pct = 35 + int(60 * i / count)
            set_job(job_id, status="clipping", progress=pct,
                    message=f"Cutting clip {i+1}/{count} ({orientation})… generating AI caption…")

            safe_title = clip["title"].replace(" ","_").replace("/","")
            out_name   = f"clip_{i+1:02d}_{safe_title}.mp4"
            out_path   = job_dir / out_name

            run_cmd([
                FFMPEG, "-y",
                "-ss", str(clip["start"]), "-to", str(clip["end"]),
                "-i", str(video_path),
                "-vf", vf,
                "-c:v", "libx264", "-crf", "20", "-preset", "fast",
                "-profile:v", "baseline", "-level", "3.1",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
                "-movflags", "+faststart", "-avoid_negative_ts", "make_zero",
                str(out_path),
            ])

            # thumbnail — pick representative frame
            thumb_name = f"Part_{i+1}_thumbnail.jpg"
            thumb_path = job_dir / thumb_name
            t_at = clip["start"] + clip["dur"] * 0.3
            if orientation == "vertical":
                # 9:16 thumbnail
                thumb_vf = "crop=in_h*9/16:in_h:(in_w-in_h*9/16)/2:0,thumbnail=60,scale=607:1080"
            else:
                thumb_vf = "thumbnail=60,scale=min(1280\\,iw):-2"
            run_cmd([
                FFMPEG, "-y",
                "-ss", str(round(t_at, 2)), "-i", str(video_path),
                "-t", "3", "-vf", thumb_vf,
                "-frames:v", "1", "-q:v", "2", str(thumb_path),
            ])

            transcript = transcript_for(cues, clip["start"], clip["end"])
            cap = build_caption_ai(meta, clip, transcript, count, url, orientation)

            size_mb = round(out_path.stat().st_size / 1024 / 1024, 1) if out_path.exists() else 0
            clips_done.append({
                **clip, **cap,
                "filename":      out_name,
                "thumb_filename": thumb_name,
                "thumb_ok":      thumb_path.exists(),
                "size_mb":       size_mb,
                "ok":            out_path.exists() and size_mb > 0,
                "orientation":   orientation,
            })

        purge_source_files(job_dir)
        set_job(job_id, status="done", progress=100,
                message=f"Done! {count} clips ready.", clips=clips_done)

    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        set_job(job_id, status="error", message=str(e))

# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index(): return render_template("index.html")

@app.route("/api/disk")
def api_disk():
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    folders = [d for d in WORK_DIR.iterdir() if d.is_dir()]
    used_mb = round(sum(folder_size_bytes(d) for d in folders) / (1024**2), 1)
    return jsonify(free_gb=round(free_space_gb(),2), used_mb=used_mb,
                   folders=len(folders), work_dir=str(WORK_DIR.resolve()),
                   ai_enabled=bool(GROQ_KEY))

@app.route("/api/cleanup", methods=["POST"])
def api_cleanup():
    with jobs_lock:
        active_ids = {jid for jid,j in jobs.items() if j.get("status") in ACTIVE_STATUSES}
    freed = removed = 0
    if WORK_DIR.exists():
        for d in WORK_DIR.iterdir():
            if d.is_dir() and d.name not in active_ids:
                freed += folder_size_bytes(d)
                shutil.rmtree(d, ignore_errors=True); removed += 1
    return jsonify(removed=removed, freed_mb=round(freed/(1024**2),1),
                   free_gb=round(free_space_gb(),2))

@app.route("/api/cookies", methods=["GET"])
def api_cookies_get():
    if not COOKIES_FILE.exists(): return jsonify(has_cookies=False)
    age_h = round((time.time() - COOKIES_FILE.stat().st_mtime) / 3600, 1)
    lines = sum(1 for _ in COOKIES_FILE.open(encoding="utf-8", errors="ignore"))
    return jsonify(has_cookies=True, age_hours=age_h, lines=lines)

@app.route("/api/cookies", methods=["POST"])
def api_cookies_post():
    data    = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()
    if not content: return jsonify(error="Empty content"), 400
    if "youtube.com" not in content and "HTTP Cookie" not in content[:200]:
        return jsonify(error="Doesn't look like a YouTube cookies file."), 400
    try:
        COOKIES_FILE.write_text(content, encoding="utf-8")
        return jsonify(ok=True, lines=content.count("\n")+1)
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.route("/api/cookies", methods=["DELETE"])
def api_cookies_delete():
    try: COOKIES_FILE.unlink(missing_ok=True); return jsonify(ok=True)
    except Exception as e: return jsonify(error=str(e)), 500

@app.route("/api/start", methods=["POST"])
def api_start():
    data     = request.json or {}
    url      = (data.get("url") or "").strip()
    count    = max(3, min(10, int(data.get("count", 6))))
    style    = data.get("style", "mixed")
    quality  = data.get("quality", "480")
    orient   = data.get("orientation", "horizontal")
    clip_dur = int(data.get("clip_duration", 60))
    clip_dur = max(30, min(180, clip_dur))

    if not url: return jsonify(error="No URL provided"), 400
    job_id = uuid.uuid4().hex
    set_job(job_id, status="queued", progress=0, message="Starting…", clips=[])
    threading.Thread(target=process_job,
        args=(job_id, url, count, style, quality, False, orient, clip_dur),
        daemon=True).start()
    return jsonify(job_id=job_id)

@app.route("/api/upload", methods=["POST"])
def api_upload():
    if free_space_gb() < MIN_FREE_GB:
        return jsonify(error=f"Low disk space ({free_space_gb():.2f} GB free)."), 507
    f = request.files.get("file")
    if not f or not f.filename: return jsonify(error="No file uploaded"), 400
    try:    count = max(3, min(10, int(request.form.get("count", 6))))
    except: count = 6
    style   = request.form.get("style", "mixed")
    orient  = request.form.get("orientation", "horizontal")
    clip_dur= int(request.form.get("clip_duration", 60))
    clip_dur= max(30, min(180, clip_dur))

    job_id  = uuid.uuid4().hex
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        f.save(job_dir / "source.mp4")
        (job_dir / "source.info.json").write_text(
            json.dumps({"title": Path(f.filename).stem}), encoding="utf-8")
    except OSError as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify(error=f"Could not save file: {e}"), 507

    set_job(job_id, status="queued", progress=0, message="Starting…", clips=[])
    threading.Thread(target=process_job,
        args=(job_id, "", count, style, "480", True, orient, clip_dur),
        daemon=True).start()
    return jsonify(job_id=job_id)

@app.route("/api/status/<job_id>")
def api_status(job_id):
    j = get_job(job_id)
    if not j: return jsonify(error="Unknown job"), 404
    return jsonify(j)

@app.route("/api/download/<job_id>/<filename>")
def api_download(job_id, filename):
    if ".." in filename or "/" in filename: return "Bad filename", 400
    path = WORK_DIR / job_id / filename
    if not path.exists(): return "File not found", 404
    return send_file(path, as_attachment=True, download_name=filename)

@app.route("/api/jobs")
def api_jobs():
    with jobs_lock: return jsonify(list(jobs.keys()))

if __name__ == "__main__":
    cleanup_stale_partials()
    subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade",
                    "yt-dlp[default]", "-q"], capture_output=True, text=True)
    init_tools()
    _, ver = run_cmd(ytdlp_cmd(["--version"]))
    print(f"yt-dlp {ver.strip()}  JS:{JS_ARGS or 'NONE'}  "
          f"cookies:{'yes' if COOKIES_FILE.exists() else 'no'}  "
          f"AI:{'Groq enabled' if GROQ_KEY else 'no key — keyword mode'}")
    print(f"Storage: {WORK_DIR.resolve()}  ({free_space_gb():.2f} GB free)")
    print("\n🎬  YT Clipper  http://localhost:5000\n")
    app.run(debug=False, port=int(os.environ.get("PORT", 5000)), host="0.0.0.0")