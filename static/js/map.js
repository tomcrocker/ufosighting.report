(function () {
  "use strict";
  const el = document.getElementById("map");
  if (!el || !window.L) return;
  const map = L.map("map").setView([30, 0], 2);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(map);
  fetch("/api/pins")
    .then((r) => r.json())
    .then((data) => {
      const cluster = L.markerClusterGroup();
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
})();
