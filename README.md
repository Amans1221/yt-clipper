# YT Clipper — Local Web UI

Paste a YouTube URL → get real downloadable MP4 clips, right in your browser.

## Setup (one time)

```bash
# 1. Install Python packages
pip install flask yt-dlp

# 2. Install ffmpeg (needed to cut clips)
#    Mac:
brew install ffmpeg
#    Windows: download from https://ffmpeg.org/download.html and add to PATH
#    Ubuntu/Debian:
sudo apt install ffmpeg
```

## Run

```bash
cd yt_clipper
python3 app.py
```

Then open **http://localhost:5000** in your browser.

## How it works

1. Paste any YouTube URL (watch, live, shorts, embed, youtu.be)
2. Choose number of clips (4–10) and style (Viral / Insights / Mixed)
3. Hit **Generate & Download** — the app:
   - Downloads the video at ≤720p using `yt-dlp`
   - Finds the best clip moments spread across the video
   - Cuts each clip with `ffmpeg` (fast, lossless-ish re-encode)
4. Each clip gets a **Preview** and **Download** button

## Clips are saved at

```
yt_clipper/clips_output/<job-id>/clip_01_Hook.mp4
                                 clip_02_Viral_Moment.mp4
                                 ...
```
