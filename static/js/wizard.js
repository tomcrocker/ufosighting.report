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
      onChange: function (sel, str, fp) {
        if (!sel.length) return;
        const d = sel[0];
        dEl.value = fp.formatDate(d, "Y-m-d");
        tEl.value = fp.formatDate(d, "H:i");
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
      });
    });
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

  function markInvalid(field) {
    // native bubbles vanish fast — keep a persistent banner + red outline
    // until the field is corrected
    if (errBanner) errBanner.hidden = false;
    const target = field.classList.contains("flatpickr-input")
      ? field.previousElementSibling || field : field;
    (target.type === "checkbox" ? target.closest(".check") || target : target)
      .classList.add("field-invalid");
    field.addEventListener("input", clearInvalid, { once: true });
    field.addEventListener("change", clearInvalid, { once: true });
  }

  function clearInvalid(e) {
    const f = e.target;
    (f.type === "checkbox" ? f.closest(".check") || f : f).classList.remove("field-invalid");
    if (errBanner && !form.querySelector(".field-invalid")) errBanner.hidden = true;
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
          if (errBanner) errBanner.hidden = false;
          alt.classList.add("field-invalid");
          alt.addEventListener("change", clearInvalid, { once: true });
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
