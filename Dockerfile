FROM python:3.12-slim

RUN apt-get update && apt-get install -y ffmpeg curl unzip && \
    curl -fsSL https://deno.land/install.sh | sh && \
    ln -s /root/.deno/bin/deno /usr/local/bin/deno && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /data/clips_output

ENV CLIPS_DIR=/data/clips_output
ENV USE_BROWSER_COOKIES=0
ENV PORT=5000

EXPOSE 5000
CMD ["gunicorn", "app:app", "--workers", "1", "--threads", "4", "--timeout", "600", "--bind", "0.0.0.0:5000"]