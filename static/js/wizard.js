// Multi-step wizard: step navigation, chip groups, geocode autocomplete,
// map pin, timezone default, duration h/m/s, story char counter.
(function () {
  "use strict";
  const form = document.getElementById("sighting-form");
  if (!form) return;

  // --- timezone default ---
  const tzInput = document.getElementById("tz_name");
  if (tzInput && !tzInput.value) {
    tzInput.value = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  }
  const tzLabel = document.getElementById("tzlabel");
  if (tzLabel) tzLabel.textContent = tzInput.value;

  // --- flatpickr combined date+time -> hidden sighted_date / sighted_time ---
  const picker = document.getElementById("sighted_at_picker");
  if (picker && window.flatpickr) {
    const dEl = document.getElementById("sighted_date");
    const tEl = document.getElementById("sighted_time");
    const initial = dEl.value && tEl.value ? dEl.value + " " + tEl.value : null;
    flatpickr(picker, {
      enableTime: true,
      dateFormat: "Y-m-d H:i",
      altInput: true,
      altFormat: "F j, Y  h:i K",
      defaultDate: initial,
      maxDate: "today",
      time_24hr: false,
      disableMobile: true,
      onChange: function (sel, str, fp) {
        if (!sel.length) return;
        const d = sel[0];
        dEl.value = fp.formatDate(d, "Y-m-d");
        tEl.value = fp.formatDate(d, "H:i");
        // bubbles to the form's revalidation listener
        dEl.dispatchEvent(new Event("change", { bubbles: true }));
      },
    });
  }

  // --- duration h/m/s -> hidden duration_value (seconds) ---
  const durInputs = ["dur_h", "dur_m", "dur_s"].map((id) => document.getElementById(id));
  const durationValue = document.getElementById("duration_value");
  function syncDuration() {
    const [h, m, s] = durInputs.map((el) => (el && parseInt(el.value, 10)) || 0);
    const total = h * 3600 + m * 60 + s;
    durationValue.value = total > 0 ? String(total) : "";
  }
  durInputs.forEach((el) => el && el.addEventListener("input", syncDuration));

  // --- story char counter ---
  const story = form.elements["description"];
  const counter = document.getElementById("charcount");
  if (story && counter) {
    const update = () => { counter.textContent = story.value.length + " / 150 min"; };
    story.addEventListener("input", update);
    update();
  }

  // --- chip groups (single via data-target; multi via data-multi="1") ---
  document.querySelectorAll(".chips").forEach((group) => {
    const target = form.elements[group.dataset.target];
    const multi = group.dataset.multi === "1";
    const chips = [...group.querySelectorAll(".chip")];
    let selected = [];
    try {
      selected = multi ? JSON.parse(target.value || "[]") : target.value ? [target.value] : [];
    } catch (e) { selected = []; }
    chips.forEach((chip) => {
      if (selected.includes(chip.dataset.value)) chip.classList.add("on");
      chip.addEventListener("click", () => {
        if (multi) {
          chip.classList.toggle("on");
          const values = chips.filter((c) => c.classList.contains("on")).map((c) => c.dataset.value);
          target.value = JSON.stringify(values);
        } else {
          const wasOn = chip.classList.contains("on");
          chips.forEach((c) => c.classList.remove("on"));
          target.value = wasOn ? "" : chip.dataset.value;
          if (!wasOn) chip.classList.add("on");
        }
        group.classList.remove("field-invalid");
      });
    });
  });

  // --- "other…" chips reveal a free-text input (shape_other / movement_other)
  document.querySelectorAll(".chips").forEach((group) => {
    const otherChip = group.querySelector('.chip[data-value="other"]');
    if (!otherChip) return;
    const base = group.dataset.target.replace(/_json$/, "");
    const input = form.elements[base + "_other"];
    if (!input) return;
    const sync = () => {
      const on = otherChip.classList.contains("on");
      input.hidden = !on;
      if (!on) input.value = "";
      else input.focus();
    };
    group.addEventListener("click", () => setTimeout(sync, 0));
    if (otherChip.classList.contains("on")) input.hidden = false;
  });

  // --- map pin + geocode autocomplete ---
  const latInput = document.getElementById("lat");
  const lonInput = document.getElementById("lon");
  let map = null, marker = null;

  function setPin(lat, lon, zoom) {
    latInput.value = (+lat).toFixed(5);
    lonInput.value = (+lon).toFixed(5);
    if (!map) return;
    if (marker) marker.setLatLng([lat, lon]);
    else marker = L.marker([lat, lon]).addTo(map);
    map.setView([lat, lon], zoom || 10);
  }

  const locInput = document.getElementById("location_text");

  // Dropped pin -> nearest town/city via reverse geocode. The location field
  // is required, so this is what makes pin-only submissions pass validation.
  // Guards: a sequence token drops stale/out-of-order responses (the server
  // throttles Nominatim to ~1 req/s, so multi-second latency is normal), and
  // a response never overwrites text the user edited after the pin drop.
  let reverseSeq = 0;
  let lastAutoFill = "";
  function fieldIsOurs() {
    const v = locInput.value.trim();
    return v === "" || v === lastAutoFill;
  }
  function autoFill(text, city, country) {
    locInput.value = text;
    lastAutoFill = text;
    document.getElementById("city").value = city || "";
    document.getElementById("country").value = country || "";
  }
  function reversePin(lat, lon) {
    const seq = ++reverseSeq;
    const coordLabel = (+lat).toFixed(3) + ", " + (+lon).toFixed(3);
    // fill coordinates immediately so Next works even before the lookup lands
    if (fieldIsOurs()) autoFill(coordLabel, "", "");
    fetch("/api/reverse?lat=" + lat + "&lon=" + lon)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((d) => {
        if (seq !== reverseSeq || !fieldIsOurs()) return; // stale or user-edited
        autoFill(d.label || coordLabel, d.city, d.country);
      })
      .catch(() => {});
  }

  if (window.L && document.getElementById("pinmap")) {
    const hasPin = latInput.value !== "";
    map = L.map("pinmap").setView(hasPin ? [+latInput.value, +lonInput.value] : [30, 0], hasPin ? 8 : 2);
    L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png", {
      attribution: "&copy; OpenStreetMap &copy; CARTO",
      subdomains: "abcd",
    }).addTo(map);
    // labels above the pin marker so city names stay readable while zooming
    map.createPane("labels");
    map.getPane("labels").style.zIndex = 650;
    map.getPane("labels").style.pointerEvents = "none";
    L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_only_labels/{z}/{x}/{y}{r}.png", {
      subdomains: "abcd", pane: "labels",
    }).addTo(map);
    if (hasPin) marker = L.marker([+latInput.value, +lonInput.value]).addTo(map);
    map.on("click", (e) => {
      setPin(e.latlng.lat, e.latlng.lng, map.getZoom());
      reversePin(e.latlng.lat, e.latlng.lng);
    });
  }

  const sugBox = document.getElementById("geo-suggestions");
  let geoTimer = null;
  const COORD_RE = /^(-?\d{1,3}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)$/;
  if (locInput && sugBox) {
    locInput.addEventListener("input", () => {
      clearTimeout(geoTimer);
      const q = locInput.value.trim();
      // typed coordinates: offer to pin them directly, skip the geocoder
      const cm = COORD_RE.exec(q.replace(/°/g, ""));
      if (cm && Math.abs(+cm[1]) <= 90 && Math.abs(+cm[2]) <= 180) {
        sugBox.innerHTML = "";
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "suggestion";
        btn.textContent = "\u{1F4CD} Use coordinates " + (+cm[1]).toFixed(4) + ", " + (+cm[2]).toFixed(4);
        btn.onclick = () => {
          reverseSeq++;           // cancel any in-flight pin lookup
          lastAutoFill = "";      // their coordinates — never overwrite
          setPin(+cm[1], +cm[2], 10);
          sugBox.innerHTML = "";
        };
        sugBox.appendChild(btn);
        return;
      }
      if (q.length < 3) { sugBox.innerHTML = ""; return; }
      geoTimer = setTimeout(async () => {
        try {
          const resp = await fetch("/api/geocode?q=" + encodeURIComponent(q));
          if (!resp.ok) return;
          const data = await resp.json();
          sugBox.innerHTML = "";
          data.results.forEach((r) => {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "suggestion";
            btn.textContent = r.display_name;
            btn.onclick = () => {
              reverseSeq++; // an explicit choice beats any in-flight pin lookup
              locInput.value = r.display_name;
              lastAutoFill = ""; // user-chosen text — never auto-overwrite it
              document.getElementById("city").value = r.city || "";
              document.getElementById("country").value = r.country || "";
              setPin(r.lat, r.lon, 10);
              sugBox.innerHTML = "";
            };
            sugBox.appendChild(btn);
          });
        } catch (e) { /* geocoder down — pin drop still works */ }
      }, 350);
    });
  }

  // --- step navigation ---
  const steps = [...form.querySelectorAll(".step")];
  const prevBtn = document.getElementById("prevbtn");
  const nextBtn = document.getElementById("nextbtn");
  const submitBtn = document.getElementById("submitbtn");
  const bar = document.getElementById("progressbar");
  const showAll = form.dataset.showAll === "1";
  let current = 0;

  const errBanner = document.getElementById("wizard-error");

  function highlightTarget(field) {
    if (field.type === "checkbox") return field.closest(".check") || field;
    return field;
  }

  function fieldValid(field) {
    // the date picker's visible input is a flatpickr altInput with no
    // constraints of its own — its truth lives in the hidden sighted_date
    if (field.id === "sighted_at_picker" || field.classList.contains("flatpickr-input")) {
      return !!document.getElementById("sighted_date").value;
    }
    return field.checkValidity();
  }

  function markInvalid(field) {
    // native bubbles vanish fast — keep a persistent banner + red outline
    // until the field is actually valid (continuous revalidation below)
    if (errBanner) errBanner.hidden = false;
    highlightTarget(field).classList.add("field-invalid");
    field.dataset.invalid = "1";
  }

  function revalidate() {
    // clear highlights only once a field is genuinely valid — and the
    // banner only once every flagged field is
    form.querySelectorAll("[data-invalid]").forEach((field) => {
      if (fieldValid(field)) {
        highlightTarget(field).classList.remove("field-invalid");
        delete field.dataset.invalid;
      }
    });
    if (errBanner && !form.querySelector("[data-invalid]")) errBanner.hidden = true;
    syncReqMarkers();
  }

  function syncReqMarkers() {
    // red * turns green once its field is satisfied — live feedback
    form.querySelectorAll("input[required], textarea[required]").forEach((field) => {
      if (field.type === "hidden") return;
      const label = field.closest("label") || field.closest(".step");
      const marker = label && label.querySelector(".req");
      if (marker) marker.classList.toggle("req-done", fieldValid(field));
    });
    // step-1 heading marker follows the location field
    const loc = document.getElementById("location_text");
    const h1req = loc && loc.closest(".step") && loc.closest(".step").querySelector("h1 .req");
    if (h1req) h1req.classList.toggle("req-done", loc.checkValidity());
  }

  form.addEventListener("input", revalidate);
  form.addEventListener("change", revalidate);

  // --- shared (second-hand) sighting: waive the eyewitness + capture confirms
  //     and require a source instead. requiredOk skips hidden fields
  //     (offsetParent === null), so we toggle both `required` and visibility. ---
  const sharedCb = document.getElementById("is_shared");
  if (sharedCb) {
    const eyewitness = document.getElementById("eyewitness-check");
    const eyewitnessCb = form.elements["confirm_eyewitness"];
    const sharedFields = document.getElementById("shared-fields");
    const sourceInput = document.getElementById("source_note");
    const captureConfirms = document.getElementById("capture-confirms");
    const syncShared = () => {
      const shared = sharedCb.checked;
      if (eyewitness) eyewitness.hidden = shared;
      if (eyewitnessCb) eyewitnessCb.required = !shared;
      if (sharedFields) sharedFields.hidden = !shared;
      if (sourceInput) sourceInput.required = shared;
      if (captureConfirms) {
        captureConfirms.hidden = shared;
        if (shared) captureConfirms.querySelectorAll("input[type=checkbox]")
          .forEach((cb) => { cb.required = false; });
      }
      revalidate();
    };
    sharedCb.addEventListener("change", syncShared);
    syncShared();
  }

  function requiredOk(index) {
    // flatpickr with altInput turns #sighted_at_picker into a hidden input;
    // hidden required fields can't be validated by reportValidity(), so we
    // check the resulting sighted_date value explicitly instead.
    const step = steps[index];
    let firstBad = null;
    for (const field of step.querySelectorAll("input[required], textarea[required]")) {
      if (field.type === "hidden" || field.offsetParent === null) continue;
      if (!field.checkValidity()) {
        markInvalid(field);
        if (!firstBad) firstBad = field;
      }
    }
    // chip groups write to hidden inputs, which native validation skips —
    // data-required="1" groups are checked by value instead
    let badGroup = null;
    for (const group of step.querySelectorAll('.chips[data-required="1"]')) {
      const target = form.elements[group.dataset.target];
      const val = (target.value || "").trim();
      let empty = group.dataset.multi === "1" ? (val === "" || val === "[]") : val === "";
      // "other" selected but not described counts as unanswered
      const otherOn = group.querySelector('.chip[data-value="other"].on');
      if (!empty && otherOn) {
        const other = form.elements[group.dataset.target.replace(/_json$/, "") + "_other"];
        if (other && !other.value.trim()) empty = true;
      }
      group.classList.toggle("field-invalid", empty);
      if (empty && !badGroup) badGroup = group;
    }
    if (badGroup && !firstBad) {
      badGroup.scrollIntoView({ block: "center", behavior: "smooth" });
      return false;
    }
    if (firstBad) {
      firstBad.reportValidity();
      return false;
    }
    if (step.querySelector("#sighted_at_picker")) {
      const dEl = document.getElementById("sighted_date");
      if (!dEl.value) {
        const alt = step.querySelector(".flatpickr-input.form-control, input.form-control")
          || document.getElementById("sighted_at_picker");
        if (alt && alt.setCustomValidity) {
          markInvalid(alt);
          alt.setCustomValidity("Pick a date and time");
          alt.reportValidity();
          setTimeout(() => alt.setCustomValidity(""), 0);
        }
        return false;
      }
    }
    return true;
  }

  // --- visual scenes: one motif per step, random starting scene each load ---
  const scenes = [...document.querySelectorAll(".scene")];
  const sceneOffset = scenes.length ? Math.floor(Math.random() * scenes.length) : 0;
  function showScene(stepIndex) {
    if (!scenes.length) return;
    const pick = (stepIndex + sceneOffset) % scenes.length;
    scenes.forEach((s, i) => s.classList.toggle("on", i === pick));
  }

  function render() {
    steps.forEach((s, i) => { s.hidden = i !== current; });
    prevBtn.hidden = current === 0;
    nextBtn.hidden = current === steps.length - 1;
    submitBtn.hidden = current !== steps.length - 1;
    if (bar) bar.style.width = ((current + 1) / steps.length) * 100 + "%";
    if (map && current === 0) setTimeout(() => map.invalidateSize(), 50);
    showScene(current);
    window.scrollTo(0, 0);
  }

  if (showAll) {
    prevBtn.hidden = true;
    nextBtn.hidden = true;
    if (bar) bar.style.width = "100%";
    showScene(0);
  } else {
    render();
    nextBtn.addEventListener("click", () => {
      if (requiredOk(current)) { current++; render(); }
    });
    prevBtn.addEventListener("click", () => { current--; render(); });

    // Safety net: native validation can't focus a required control inside a
    // hidden step (the browser then blocks the POST with no visible feedback)
    // — jump to the offending step instead so the message is seen.
    form.addEventListener("submit", (e) => {
      for (let i = 0; i < steps.length; i++) {
        const bad = [...steps[i].querySelectorAll("input[required], textarea[required]")]
          .find((f) => f.type !== "hidden" && !f.checkValidity());
        if (bad) {
          if (i !== current) { current = i; render(); }
          e.preventDefault();
          markInvalid(bad);
          setTimeout(() => bad.reportValidity(), 60);
          return;
        }
      }
    });
  }
})();
