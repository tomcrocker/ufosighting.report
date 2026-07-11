(function () {
  "use strict";
  const el = document.getElementById("map");
  if (!el || !window.L) return;
  const map = L.map("map").setView([30, 0], 2);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(map);

  let cluster = null;

  function loadPins() {
    const params = new URLSearchParams();
    const from = document.getElementById("map-from");
    const to = document.getElementById("map-to");
    const shape = document.getElementById("map-shape");
    if (from && from.value) params.set("from", from.value);
    if (to && to.value) params.set("to", to.value);
    if (shape && shape.value) params.set("shape", shape.value);
    fetch("/api/pins" + (params.toString() ? "?" + params.toString() : ""))
      .then((r) => r.json())
      .then((data) => {
        if (cluster) map.removeLayer(cluster);
        cluster = L.markerClusterGroup();
        data.pins.forEach((p) => {
          const marker = L.marker([p.lat, p.lon]);
          marker.bindPopup(
            '<a href="' + p.url + '"><strong>' + p.title + "</strong></a><br>" +
            p.date + (p.shape ? " · " + p.shape : "") +
            (p.thumb ? '<br><img src="' + p.thumb + '" style="max-width:180px;border-radius:6px;margin-top:6px">' : "")
          );
          cluster.addLayer(marker);
        });
        map.addLayer(cluster);
      });
  }

  const apply = document.getElementById("map-apply");
  const clear = document.getElementById("map-clear");
  if (apply) apply.addEventListener("click", loadPins);
  if (clear) clear.addEventListener("click", function () {
    ["map-from", "map-to", "map-shape"].forEach(function (id) {
      const f = document.getElementById(id);
      if (f) f.value = "";
    });
    loadPins();
  });

  loadPins();
})();
