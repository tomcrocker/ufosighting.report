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
  // is required, so this is what makes pin-only submissions pass validation;
  // if the geocoder is down we fall back to plain coordinates.
  function reversePin(lat, lon) {
    const coordLabel = (+lat).toFixed(3) + ", " + (+lon).toFixed(3);
    fetch("/api/reverse?lat=" + lat + "&lon=" + lon)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((d) => {
        locInput.value = d.label || coordLabel;
        document.getElementById("city").value = d.city || "";
        document.getElementById("country").value = d.country || "";
      })
      .catch(() => {
        if (!locInput.value.trim()) locInput.value = coordLabel;
      });
  }

  if (window.L && document.getElementById("pinmap")) {
    const hasPin = latInput.value !== "";
    map = L.map("pinmap").setView(hasPin ? [+latInput.value, +lonInput.value] : [30, 0], hasPin ? 8 : 2);
    L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
      attribution: "&copy; OpenStreetMap &copy; CARTO",
      subdomains: "abcd",
    }).addTo(map);
    if (hasPin) marker = L.marker([+latInput.value, +lonInput.value]).addTo(map);
    map.on("click", (e) => {
      setPin(e.latlng.lat, e.latlng.lng, map.getZoom());
      reversePin(e.latlng.lat, e.latlng.lng);
    });
  }

  const sugBox = document.getElementById("geo-suggestions");
  let geoTimer = null;
  if (locInput && sugBox) {
    locInput.addEventListener("input", () => {
      clearTimeout(geoTimer);
      const q = locInput.value.trim();
      if (q.length < 3) { sugBox.innerHTML = ""; return; }
      geoTimer = setTimeout(async () => {
        try {
          const resp = await fetch("/api/geocode?q=" + encodeURIComponent(q));
          if (!resp.ok) return;
          const data = await resp.json();
          sugBox.innerHTML = "";
          data.results.forEach((r) => {
            const div = document.createElement("div");
            div.className = "suggestion";
            div.textContent = r.display_name;
            div.onclick = () => {
              locInput.value = r.display_name;
              document.getElementById("city").value = r.city || "";
              document.getElementById("country").value = r.country || "";
              setPin(r.lat, r.lon, 10);
              sugBox.innerHTML = "";
            };
            sugBox.appendChild(div);
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

  function requiredOk(index) {
    // flatpickr with altInput turns #sighted_at_picker into a hidden input;
    // hidden required fields can't be validated by reportValidity(), so we
    // check the resulting sighted_date value explicitly instead.
    const step = steps[index];
    for (const field of step.querySelectorAll("input[required], textarea[required]")) {
      if (field.type === "hidden" || field.offsetParent === null) continue;
      if (!field.reportValidity()) return false;
    }
    if (step.querySelector("#sighted_at_picker")) {
      const dEl = document.getElementById("sighted_date");
      if (!dEl.value) {
        const alt = step.querySelector(".flatpickr-input.form-control, input.form-control")
          || document.getElementById("sighted_at_picker");
        if (alt && alt.setCustomValidity) {
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
  }
})();
