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
    } catch (err) {
      row.innerHTML = "<span class='err'>" + file.name + " — " + err.message + "</span>";
    } finally {
      inflight--;
      syncState();
    }
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
