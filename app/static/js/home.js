// Home page: solid navigation bar after scrolling, and a live reading from the hive.
(function () {
    const nav = document.getElementById("nav");
    const onScroll = () => nav.classList.toggle("solid", window.scrollY > 40);
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();

    const $ = (id) => document.getElementById(id);

    function timeAgo(isoString) {
        const seconds = Math.round((Date.now() - new Date(isoString).getTime()) / 1000);
        if (seconds < 60) return `${Math.max(seconds, 0)} s ago`;
        if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
        return `${Math.round(seconds / 3600)} h ago`;
    }

    // Shown only while the Pi is online, so the page never shows stale numbers as live
    async function loadLive() {
        if (!window.HIVE_STATUS_URL) return;
        try {
            const res = await fetch(window.HIVE_STATUS_URL);
            if (!res.ok) return;
            const data = await res.json();
            const latest = data.sensors.latest;
            if (!data.sensors.online || !latest) {
                $("live").hidden = true;
                return;
            }
            const parts = [];
            $("liveTemp").textContent = latest.temperature_c != null ? `${latest.temperature_c.toFixed(1)} °C` : "";
            $("liveHum").textContent = latest.humidity_pct != null ? `${latest.humidity_pct.toFixed(0)} % humidity` : "";
            const pressure = latest.extra && latest.extra.pressure_hpa;
            if (pressure != null) parts.push(`${Number(pressure).toFixed(0)} hPa`);
            $("liveExtra").textContent = parts.join(" · ");
            $("liveAgo").textContent = `updated ${timeAgo(latest.recorded_at)}`;
            $("live").hidden = false;
        } catch (e) {
            // Server not reachable: keep the section hidden
        }
    }

    loadLive();
    setInterval(loadLive, 30000);
})();
