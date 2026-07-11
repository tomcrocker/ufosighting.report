/* Full-screen sightings map — Blue Book Archive treatment: dark CARTO tiles,
   glowing markers + clusters, regionally-normalized heatmap, military-base
   overlay. Pins come from /api/pins in one payload; no filters here. */
(function () {
  "use strict";

  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s || "";
    return d.innerHTML;
  }

  function markerIcon() {
    return L.divIcon({ className: "glow-marker", iconSize: [12, 12], iconAnchor: [6, 6] });
  }

  function pinPopup(p) {
    return `<div class="map-popup">
      ${p.thumb ? `<img class="map-popup-thumb" src="${p.thumb}" alt="" loading="lazy">` : ""}
      <div class="map-popup-title">${esc(p.title)}</div>
      <div class="map-popup-meta"><div>&#128197; ${p.date}</div>${p.shape ? `<div>&#128444; ${esc(p.shape)}</div>` : ""}</div>
      <a href="${p.url}" class="map-popup-btn">View sighting &rarr;</a>
    </div>`;
  }

  const inUS = (lat, lon) => lat >= 24 && lat <= 50 && lon >= -125 && lon <= -66;

  function regionalWeights(pins) {
    // Boost non-US intensity so US volume doesn't drown other hotspots
    // (sqrt-damped, capped — the Blue Book normalization).
    let us = 0, intl = 0;
    pins.forEach((p) => (inUS(p.lat, p.lon) ? us++ : intl++));
    if (!us || !intl) return { us: 0.7, intl: 0.7 };
    const boost = Math.min(Math.sqrt(us / intl), 4);
    return { us: 0.5, intl: Math.min(0.5 * boost, 1.0) };
  }

  function loadBases(map) {
    const basesLayer = L.layerGroup();
    MILITARY_BASES.forEach((b) => {
      const icon = L.divIcon({
        className: b.special ? "base-marker-special" : "base-marker",
        iconSize: [12, 12], iconAnchor: [6, 6],
      });
      L.marker([b.lat, b.lng], { icon })
        .bindPopup(`<div class="map-popup"><div class="map-popup-title">${esc(b.name)}</div>
          <div class="map-popup-meta"><div>${esc(b.type)}</div><div>${esc(b.note)}</div></div></div>`)
        .addTo(basesLayer);
    });
    basesLayer.addTo(map);
    document.getElementById("bases-toggle").addEventListener("change", function () {
      if (this.checked) map.addLayer(basesLayer);
      else map.removeLayer(basesLayer);
    });
  }

  function loadPins(map, clusterLayer) {
    fetch("/api/pins")
      .then((r) => r.json())
      .then((data) => {
        const pins = data.pins;
        document.getElementById("map-count").textContent = pins.length;
        document.getElementById("map-total").textContent = pins.length;
        const w = regionalWeights(pins);
        const heat = [];
        pins.forEach((p) => {
          L.marker([p.lat, p.lon], { icon: markerIcon() })
            .bindPopup(pinPopup(p), { minWidth: 220 })
            .addTo(clusterLayer);
          heat.push([p.lat, p.lon, inUS(p.lat, p.lon) ? w.us : w.intl]);
        });
        // maxZoom 7 (Blue Book uses 10): intensity scales by 2^(zoom-maxZoom),
        // and with ~10x fewer points than Blue Book the glow needs the boost
        // to match the reference visually at the initial zoom-4 view.
        L.heatLayer(heat, {
          radius: 18, blur: 20, maxZoom: 7, minOpacity: 0.05, max: 1.0,
          gradient: {
            0.0: "rgba(74, 222, 128, 0)",
            0.3: "rgba(74, 222, 128, 0.08)",
            0.5: "rgba(120, 230, 100, 0.15)",
            0.65: "rgba(200, 240, 80, 0.3)",
            0.8: "rgba(250, 204, 21, 0.5)",
            0.9: "rgba(251, 146, 60, 0.6)",
            1.0: "rgba(255, 100, 50, 0.7)",
          },
        }).addTo(map);
      });
  }

  function init() {
    const el = document.getElementById("map");
    if (!el || !window.L) return;
    const map = L.map("map", { zoomControl: false, worldCopyJump: true }).setView([39.8, -98.5], 4);
    L.control.zoom({ position: "bottomright" }).addTo(map);
    L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
      attribution: "&copy; OpenStreetMap &copy; CARTO",
      subdomains: "abcd", maxZoom: 19,
    }).addTo(map);

    const clusterLayer = L.markerClusterGroup({
      maxClusterRadius: 50,
      iconCreateFunction(cluster) {
        const n = cluster.getChildCount();
        const size = n > 20 ? ["large", 44] : n > 10 ? ["medium", 40] : ["small", 36];
        return L.divIcon({
          html: `<div>${n}</div>`,
          className: `marker-cluster marker-cluster-${size[0]}`,
          iconSize: L.point(size[1], size[1]),
        });
      },
    }).addTo(map);

    loadBases(map);
    loadPins(map, clusterLayer);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
