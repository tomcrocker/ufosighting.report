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

---

## Pivot: anonymous submission + verify DM + ingest (2026-07-11)

The OAuth "post as self" path is dormant pending Reddit web-app approval. The
live flow is anonymous submission → bot DMs a verify link → verified click
posts instantly (else mod review). This all runs on ONE script app under the
`ufosightingsbot` account.

### Reddit
- [ ] `ufosightingsbot` must be a **moderator or approved submitter** of the
      target sub (done for r/tmoshtest; repeat for r/UFOs) or AutoMod's
      account-age rule removes its posts.
- [ ] Its script app needs scopes: `submit`, `privatemessages`, `read`
      (post, send/read DMs, read listings). Once approved, set in `.env`:
      SCRIPT_CLIENT_ID / SCRIPT_CLIENT_SECRET / SCRIPT_USERNAME=ufosightingsbot
      / SCRIPT_PASSWORD.
- [ ] Let the bot **age / earn a little karma** before prod so verify DMs
      aren't spam-filtered.

### Cloudflare Turnstile
- [ ] Create a Turnstile widget (dashboard → Turnstile) for ufosighting.report
      → TURNSTILE_SITE_KEY + TURNSTILE_SECRET_KEY in `.env`. If left empty the
      app skips the check (dev bypass) — set both before real launch.

### Deploy
- [ ] `bash deploy/deploy.sh` (migration runs automatically on restart via
      init_db — adds submitter_ip/username_verified/verify_token/verify_sent_at
      to the live table).
- [ ] Install the ingest timer:
      `sudo cp deploy/ufosighting-ingest.service deploy/ufosighting-ingest.timer /etc/systemd/system/`
      `sudo systemctl daemon-reload && sudo systemctl enable --now ufosighting-ingest.timer`
- [ ] One-shot backfill of existing Sighting posts:
      `cd /home/ubuntu/ufosighting && set -a; . .env; set +a; .venv/bin/python ingest.py --backfill`

### End-to-end test on r/tmoshtest
- [ ] **Verify path**: submit anonymously with your own Reddit username →
      bot DMs you a verify link → click it → post appears in r/tmoshtest as
      ufosightingsbot crediting you (verified) → gallery live + map + thumbnail.
- [ ] **Fallback path**: submit with a username you don't control (or don't
      click the DM) → after VERIFY_WINDOW_HOURS the sync sweep moves it to
      `/admin/review` → Approve → bot posts (self-reported).
- [ ] **Ingest**: make a native Sighting-flaired post on r/tmoshtest → within
      10 min it appears in the gallery, deduped against bot-posted ones.

### Ingest extraction (xAI + geocode)
- [ ] Add to VM .env: XAI_API_KEY (xai-...), XAI_MODEL=grok-3-mini, INGEST_SUBREDDIT=UFOs
- [ ] Deploy (geocode_cache table auto-created by init_db on restart)
- [ ] Dry-run small first: run one page and eyeball extracted rows in the gallery:
      `cd /home/ubuntu/ufosighting && set -a; . .env; set +a; .venv/bin/python ingest.py`
- [ ] Full 30-day backfill (throttled ~2s/post + 3s/page — expect several minutes):
      `.venv/bin/python ingest.py --backfill`
- [ ] Spot-check: map pins present for located posts, sighting dates look right,
      "from r/UFOs" badge shows on ingested cards
- [ ] Enable ongoing ingest: `sudo systemctl enable --now ufosighting-ingest.timer`

### Meilisearch (search + gallery + map, SQLite fallback)
- [ ] Install binary on the VM:
      `cd /home/ubuntu && curl -L https://install.meilisearch.com | sh`
- [ ] Generate a master key: `openssl rand -hex 24` → add to .env:
      MEILI_URL=http://127.0.0.1:7700 / MEILI_KEY=<key> / MEILI_INDEX=sightings
- [ ] `sudo cp deploy/meilisearch.service /etc/systemd/system/ && sudo systemctl daemon-reload`
      `sudo systemctl enable --now meilisearch` (unit caps RAM at 300M — 1GB VM!)
- [ ] Deploy app, then full index: `set -a; . .env; set +a; .venv/bin/python reindex.py`
- [ ] Verify: search works with a typo (e.g. "trangle"), `free -h` has headroom,
      `systemctl status meilisearch` shows no OOM kills
- [ ] If Meili ever misbehaves on this box: `systemctl stop meilisearch` and the
      site transparently falls back to SQLite — nothing breaks.
