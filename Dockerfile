FROM denoland/deno:bin AS deno
FROM python:3.12-slim

# System deps for yt-dlp merging and ffmpeg clip cutting
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Modern JS runtime for yt-dlp's YouTube challenge solver
COPY --from=deno /deno /usr/local/bin/deno

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render injects $PORT automatically; keep --workers 1 so in-memory jobs stay consistent
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 4 --timeout 300"]
