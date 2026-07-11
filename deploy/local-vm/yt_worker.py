#!/usr/bin/env python3
"""YouTube download worker for ufosighting.report — runs on the LOCAL VM
(192.168.8.224, residential IP; YouTube blocks the Oracle datacenter IP).

Per run (ufosighting-yt.timer every 10 min; Type=oneshot so runs never
overlap): claim pending jobs from Oracle over SSH, yt-dlp each (<=720p mp4,
200MB cap), upload to R2, report done/fail back over SSH.

Config: ~/ufosighting-yt/config.json (chmod 600) — see config.example.json.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone

import boto3

CONFIG_PATH = os.path.expanduser("~/ufosighting-yt/config.json")
YT_DLP = os.path.expanduser("~/.local/bin/yt-dlp")
DOWNLOAD_TIMEOUT = 600
SSH_TIMEOUT = 60


def ssh_ytq(cfg, *args):
    remote = ("cd /home/ubuntu/ufosighting && .venv/bin/python ytq.py "
              + " ".join(shlex.quote(str(a)) for a in args))
    proc = subprocess.run(
        ["ssh", "-i", os.path.expanduser(cfg["oracle_key"]),
         "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
         f"{cfg['oracle_user']}@{cfg['oracle_host']}", remote],
        capture_output=True, text=True, timeout=SSH_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError(f"ssh ytq {args[0]} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def video_id(url):
    m = re.search(r"v=([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else "unknown"


def download(url, out_dir):
    """Run yt-dlp; return the mp4 path or raise RuntimeError.
    yt-dlp exits 0 when --max-filesize skips, so 'no mp4' is also failure."""
    proc = subprocess.run(
        [YT_DLP, "--max-filesize", "200M",
         "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
         "--merge-output-format", "mp4", "--no-playlist",
         "--socket-timeout", "30",
         "-o", os.path.join(out_dir, "video.%(ext)s"), url],
        capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT)
    mp4 = os.path.join(out_dir, "video.mp4")
    if proc.returncode != 0 or not os.path.exists(mp4) or os.path.getsize(mp4) == 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-300:]
        raise RuntimeError(tail or "yt-dlp produced no mp4")
    return mp4


def main():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    jobs = json.loads(ssh_ytq(cfg, "claim") or "[]")
    if not jobs:
        return
    s3 = boto3.client("s3", endpoint_url=cfg["r2_endpoint"],
                      aws_access_key_id=cfg["r2_access_key"],
                      aws_secret_access_key=cfg["r2_secret_key"],
                      region_name="auto")
    for job in jobs:
        td = tempfile.mkdtemp(prefix="ufoyt_")
        try:
            mp4 = download(job["url"], td)
            now = datetime.now(timezone.utc)
            key = (f"uploads/{now:%Y}/{now:%m}/"
                   f"yt_{video_id(job['url'])}_{uuid.uuid4().hex[:8]}.mp4")
            size = os.path.getsize(mp4)
            with open(mp4, "rb") as f:
                s3.put_object(Bucket=cfg["r2_bucket"], Key=key, Body=f,
                              ContentType="video/mp4")
            ssh_ytq(cfg, "done", job["job_id"], "--key", key, "--size", size)
            print(f"job {job['job_id']} sighting {job['sighting_id']}: {key} ({size}B)")
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            msg = str(exc)[:300]
            print(f"job {job['job_id']} failed: {msg}", file=sys.stderr)
            try:
                ssh_ytq(cfg, "fail", job["job_id"], "--error", msg)
            except RuntimeError as exc2:
                print(f"could not report failure: {exc2}", file=sys.stderr)
        finally:
            shutil.rmtree(td, ignore_errors=True)


if __name__ == "__main__":
    main()
