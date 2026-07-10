# Go-live runbook — ufosighting.report

## 1. Reddit apps (reddit.com/prefs/apps)
- [ ] Create **web app** "ufosighting-report" (prod):
      redirect uri `https://ufosighting.report/auth/callback`
      → REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET (prod .env)
- [ ] Create **web app** "ufosighting-report-dev":
      redirect uri `http://localhost:8010/auth/callback` (dev .env)
- [ ] Script app for sync: reuse an existing mod-account script app
      → SCRIPT_CLIENT_ID / SCRIPT_CLIENT_SECRET / SCRIPT_USERNAME / SCRIPT_PASSWORD
- [ ] Sighting flair template id: GET
      `https://oauth.reddit.com/r/UFOs/api/link_flair_v2` with the script token,
      copy the Sighting flair's `id` (UUID) → SIGHTING_FLAIR_ID

## 2. Cloudflare R2
- [ ] Create bucket `ufosighting-media`
- [ ] R2 → bucket → Settings → Custom Domains → add `media.ufosighting.report`
- [ ] Create R2 API token (Object Read & Write, this bucket) → R2_ACCESS_KEY / R2_SECRET_KEY
- [ ] Bucket CORS policy:
      [{"AllowedOrigins": ["https://ufosighting.report", "http://localhost:8010"],
        "AllowedMethods": ["PUT"],
        "AllowedHeaders": ["content-type", "content-length"],
        "MaxAgeSeconds": 3600}]
- [ ] Dev bucket `ufosighting-media-dev` (same steps, no custom domain needed —
      set dev MEDIA_BASE_URL to the bucket's r2.dev public URL)

## 3. DNS + tunnel (VM 170.9.36.91)
- [ ] FIRST verify the old archive is really off this tunnel:
      `dig +short ufosarchive.xyz` and check the Cloudflare dashboard — the
      ufosarchive.xyz DNS records should point at the LOCAL VM's tunnel, not
      tunnel 216a1dc8-…. If anything still points here, stop and investigate.
- [ ] Edit `/etc/cloudflared/config.yml` — replace the stale ufosarchive ingress:
      ingress:
        - hostname: ufosighting.report
          service: http://localhost:80
        - hostname: www.ufosighting.report
          service: http://localhost:80
        - service: http_status:404
      then `sudo systemctl restart cloudflared`
- [ ] Cloudflare DNS (ufosighting.report zone):
      CNAME @   216a1dc8-99e6-497b-90a0-45cfa04cd02c.cfargotunnel.com (proxied)
      CNAME www 216a1dc8-99e6-497b-90a0-45cfa04cd02c.cfargotunnel.com (proxied)
      (the media CNAME is created automatically by the R2 custom-domain step)

## 4. VM setup (one-time)
- [ ] `sudo apt update && sudo apt install -y python3-venv ffmpeg`
- [ ] `mkdir -p /home/ubuntu/ufosighting`
- [ ] First rsync from the Mac repo root: `bash deploy/deploy.sh`
      (the restart step fails on first run — expected, keep going)
- [ ] On the VM: `cd /home/ubuntu/ufosighting && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
- [ ] `cp .env.example .env` and fill every value; prod uses
      BASE_URL=https://ufosighting.report and SUBREDDIT=<test subreddit> for now
- [ ] `sudo cp deploy/nginx-ufosighting.conf /etc/nginx/sites-available/ufosighting`
      `sudo ln -s /etc/nginx/sites-available/ufosighting /etc/nginx/sites-enabled/`
      `sudo nginx -t && sudo systemctl reload nginx`
- [ ] `sudo cp deploy/ufosighting-*.service deploy/ufosighting-*.timer /etc/systemd/system/`
      `sudo systemctl daemon-reload`
      `sudo systemctl enable --now ufosighting-web ufosighting-sync.timer ufosighting-cleanup.timer`
- [ ] Passwordless restart for deploys:
      `echo 'ubuntu ALL=NOPASSWD: /usr/bin/systemctl restart ufosighting-web' | sudo tee /etc/sudoers.d/ufosighting`

## 5. End-to-end test (against the test subreddit)
- [ ] On the VM: `curl -s -H "Host: ufosighting.report" http://127.0.0.1/` → HTML
- [ ] Visit https://ufosighting.report → gallery loads
- [ ] Log in with Reddit, submit a full test sighting with an image and a video
- [ ] Confirm: post appears in the test subreddit as YOUR account, entry live
      in the gallery, media loads from media.ufosighting.report, thumbnail
      appears within ~30s, pin shows on the map
- [ ] Remove the test post as a mod → within 15 min the gallery entry hides
      (`journalctl -u ufosighting-sync.service -n 5`)
- [ ] Approve the post → within 15 min the entry returns

## 6. Flip to production
- [ ] Set `SUBREDDIT=UFOs` in the VM .env, `sudo systemctl restart ufosighting-web`
- [ ] Update AutoMod / sub wiki / pinned post to introduce ufosighting.report
