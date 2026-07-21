// Direct-to-R2 uploads via presigned PUT with progress + retry.
(function () {
  "use strict";
  const dropzone = document.getElementById("dropzone");
  const filepick = document.getElementById("filepick");
  const filelist = document.getElementById("filelist");
  const mediaInput = document.getElementById("media_json");
  const submitBtn = document.getElementById("submitbtn");
  if (!dropzone) return;

  const MAX_FILES = 10;
  let media = [];
  try { media = JSON.parse(mediaInput.value) || []; } catch (e) { media = []; }
  let inflight = 0;
  media.forEach((m) => renderRow(m.key.split("/").pop(), m, null));

  function syncState() {
    mediaInput.value = JSON.stringify(media);
    submitBtn.disabled = inflight > 0;
    if (inflight > 0) submitBtn.textContent = "Uploading…";
    else submitBtn.textContent = "Submit & post to r/UFOs";
    // While an upload is in flight the media confirmations aren't shown yet
    // (files join `media` only on success) — block Next so the user can't
    // leave the step and end up with required checkboxes inside a hidden
    // section, which would silently kill native form validation on submit.
    const nextBtn = document.getElementById("nextbtn");
    if (nextBtn) nextBtn.disabled = inflight > 0;
    // guideline confirmations apply only when media is attached
    const confirms = document.getElementById("media-confirms");
    if (confirms) {
      const hasMedia = media.length > 0;
      confirms.hidden = !hasMedia;
      confirms.querySelectorAll("input[type=checkbox]").forEach((cb) => {
        cb.required = hasMedia;
      });
    }
  }

  function renderRow(name, item, progressEl) {
    const row = document.createElement("div");
    row.className = "file";
    row.innerHTML = "<span>" + name + "</span>";
    if (progressEl) row.appendChild(progressEl);
    const rm = document.createElement("button");
    rm.type = "button";
    rm.className = "btn danger";
    rm.textContent = "remove";
    rm.onclick = () => {
      media = media.filter((m) => m !== item);
      row.remove();
      syncState();
    };
    row.appendChild(rm);
    filelist.appendChild(row);
    return row;
  }

  function putWithRetry(url, file, progress, attempt) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("PUT", url);
      xhr.setRequestHeader("Content-Type", file.type);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) progress.value = e.loaded / e.total;
      };
      const retry = () => {
        if (attempt < 3) {
          setTimeout(
            () => putWithRetry(url, file, progress, attempt + 1).then(resolve, reject),
            1000 * attempt
          );
        } else reject(new Error("upload failed after 3 attempts"));
      };
      xhr.onload = () => (xhr.status >= 200 && xhr.status < 300 ? resolve() : retry());
      xhr.onerror = retry;
      xhr.send(file);
    });
  }

  async function uploadFile(file) {
    if (media.length + inflight >= MAX_FILES) {
      alert("Maximum number of files reached.");
      return;
    }
    const progress = document.createElement("progress");
    progress.max = 1;
    progress.value = 0;
    const item = { key: null, kind: null, width: null, height: null, size_bytes: file.size };
    const row = renderRow(file.name, item, progress);
    inflight++;
    syncState();
    try {
      const presign = await fetch("/api/presign", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: file.name, content_type: file.type, size_bytes: file.size }),
      });
      if (!presign.ok) throw new Error((await presign.json()).detail || "presign failed");
      const info = await presign.json();
      await putWithRetry(info.upload_url, file, progress, 1);
      item.key = info.key;
      item.kind = info.kind;
      if (info.kind === "image") {
        await new Promise((done) => {
          const img = new Image();
          img.onload = () => { item.width = img.naturalWidth; item.height = img.naturalHeight; done(); };
          img.onerror = done;
          img.src = URL.createObjectURL(file);
        });
      }
      media.push(item);
      progress.remove();
      showMetaPreview(row, item);
    } catch (err) {
      row.innerHTML = "<span class='err'>" + file.name + " — " + err.message + "</span>";
    } finally {
      inflight--;
      syncState();
    }
  }

  // After upload: show what technical metadata the file carries and let the
  // reporter choose what gets published (device / time / location).
  async function showMetaPreview(row, item) {
    try {
      const resp = await fetch("/api/media-meta", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: item.key, kind: item.kind }),
      });
      if (!resp.ok) return;
      const data = await resp.json();
      let anchor = row;
      // Provenance nudge: warn (before the metadata table) if this doesn't look
      // like an original camera file — this must run even when there are no rows
      // to show, because a screenshot / stripped file is exactly the case to flag.
      if (data.provenance && !data.provenance.original) {
        const warn = document.createElement("div");
        warn.className = "meta-warn";
        warn.innerHTML =
          "<strong>⚠️ This may not be an original camera file.</strong> " +
          data.provenance.detail +
          " If you still have the original photo or video straight from your camera, please upload that instead.";
        anchor.after(warn);
        anchor = warn;
      }
      if (!data.rows.length) return;
      item.exif = { device: true, time: true, location: true };
      const box = document.createElement("div");
      box.className = "meta-preview";
      const rows = data.rows.map((r) => "<tr><td>" + r[0] + "</td><td>" + r[1] + "</td></tr>").join("");
      const cats = [
        ["device", "Device & camera settings", data.has.device],
        ["time", "Date & time taken", data.has.time],
        ["location", "Location (GPS)", data.has.location],
      ].filter((c) => c[2]);
      box.innerHTML =
        "<details open><summary>Metadata found in this file — choose what to publish</summary>" +
        "<table class='facts'>" + rows + "</table>" +
        "<div class='meta-choices'>" + cats.map(([k, label]) =>
          "<label class='check'><input type='checkbox' data-cat='" + k + "' checked> Publish " + label + "</label>"
        ).join("") + "</div>" +
        (data.has.location
          ? "<p class='muted meta-note'>Uncheck Location and we scrub GPS from the published file and page.</p>"
          : "") +
        "</details>";
      box.querySelectorAll("input[data-cat]").forEach((cb) => {
        cb.addEventListener("change", () => {
          item.exif[cb.dataset.cat] = cb.checked;
          syncState();
        });
      });
      anchor.after(box);
      syncState(); // persist item.exif into media_json
    } catch (e) { /* preview is best-effort */ }
  }

  dropzone.onclick = () => filepick.click();
  filepick.onchange = () => [...filepick.files].forEach(uploadFile);
  ["dragover", "dragleave", "drop"].forEach((ev) =>
    dropzone.addEventListener(ev, (e) => {
      e.preventDefault();
      dropzone.classList.toggle("drag", ev === "dragover");
      if (ev === "drop") [...e.dataTransfer.files].forEach(uploadFile);
    })
  );
  syncState();
})();
