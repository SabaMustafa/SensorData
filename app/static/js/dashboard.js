// Dashboard: polls the JSON API and updates the page without reloading.
(function () {
    const STATUS_EVERY_MS = 5000;
    const HISTORY_EVERY_MS = 60000;
    const statusUrl = `${API_BASE}/hives/${HIVE_ID}/status`;
    const historyUrl = (hours) => `${API_BASE}/hives/${HIVE_ID}/readings?hours=${hours}&max_points=400`;

    const $ = (id) => document.getElementById(id);
    let historyHours = 24;
    let chart = null;

    function fmt(value, digits = 1) {
        return value === null || value === undefined ? "--" : Number(value).toFixed(digits);
    }

    function timeAgo(isoString) {
        if (!isoString) return "never";
        const seconds = Math.round((Date.now() - new Date(isoString).getTime()) / 1000);
        if (seconds < 60) return `${Math.max(seconds, 0)} s ago`;
        if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
        if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
        return new Date(isoString).toLocaleString();
    }

    function setBadge(el, text, kind) {
        el.textContent = text;
        el.className = "badge" + (kind ? " badge-" + kind : "");
    }

    // ------------------------------------------------------------
    // Live status
    // ------------------------------------------------------------
    function renderStatus(data) {
        const latest = data.sensors.latest;

        $("temp").textContent = fmt(latest && latest.temperature_c);
        $("hum").textContent = fmt(latest && latest.humidity_pct);
        $("weight").textContent = fmt(latest && latest.weight_kg, 2);
        $("lastReading").textContent = latest ? timeAgo(latest.recorded_at) : "never";

        // Extra sensors (anything the Pi sends besides temperature/humidity/weight)
        const extra = (latest && latest.extra) || {};
        const keys = Object.keys(extra);
        $("extraSensors").innerHTML = keys.length
            ? '<ul class="status-list" style="margin-top:10px">' +
              keys.map((k) => `<li><span class="label">${k}</span><span class="value">${extra[k]}</span></li>`).join("") +
              "</ul>"
            : "";

        if (data.sensors.online) setBadge($("sensorBadge"), "Online", "green");
        else if (latest) setBadge($("sensorBadge"), "Pi offline", "red");
        else setBadge($("sensorBadge"), "No data yet", "orange");

        // Queen
        const q = data.queen;
        $("queenStatus").textContent = q.status;
        $("queenConf").textContent = q.present ? fmt(q.confidence, 2) : "--";
        $("queenLastSeen").textContent = timeAgo(q.last_seen_live);
        if (q.present) setBadge($("queenBadge"), "Queen detected", "green");
        else if (q.status === "SCANNING") setBadge($("queenBadge"), "Scanning", "blue");
        else setBadge($("queenBadge"), q.status, "");

        // Camera
        const mode = data.camera.mode;
        const modeText = {
            live: "Live Pi camera",
            demo: data.camera.live_source_configured ? "Demo video (Pi camera offline)" : "Demo video (no camera set)",
            offline: "Offline",
            starting: "Starting…",
            disabled: "Disabled",
        }[mode] || mode;
        $("cameraMode").textContent = modeText;
        const kind = { live: "green", demo: "orange", offline: "red" }[mode] || "";
        setBadge($("videoBadge"), mode === "live" ? "LIVE" : modeText, kind);

        const err = data.camera.detection_error;
        $("detectionError").style.display = err ? "block" : "none";
        $("detectionError").textContent = err ? "Detection error: " + err : "";

        renderAudio(data.audio);
    }

    // ------------------------------------------------------------
    // Audio recordings switch
    // ------------------------------------------------------------
    const settingsUrl = `${API_BASE}/hives/${HIVE_ID}/settings`;
    let savingSwitch = false;

    function fmtBytes(bytes) {
        if (bytes >= 1024 ** 3) return (bytes / 1024 ** 3).toFixed(2) + " GB";
        return (bytes / 1024 ** 2).toFixed(1) + " MB";
    }

    function renderAudio(audio) {
        if (!audio) return;
        const sw = $("recordingsSwitch");
        if (!savingSwitch) {
            sw.checked = audio.recordings_enabled;
            sw.disabled = false;
        }
        $("audioUsed").textContent = fmtBytes(audio.used_bytes) +
            (audio.limit_bytes ? " of " + fmtBytes(audio.limit_bytes) : "");
        $("audioCount").textContent = audio.recordings_count;

        if (!audio.recordings_enabled) setBadge($("audioBadge"), "Off", "");
        else if (audio.storage_full) setBadge($("audioBadge"), "Storage full", "red");
        else setBadge($("audioBadge"), "Receiving", "green");
    }

    $("recordingsSwitch").addEventListener("change", async (e) => {
        const sw = e.target;
        savingSwitch = true;
        sw.disabled = true;
        $("audioError").style.display = "none";
        try {
            const res = await fetch(settingsUrl, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ recordings_enabled: sw.checked }),
            });
            if (!res.ok) throw new Error(res.status);
            savingSwitch = false;
            renderAudio(await res.json());
        } catch (err) {
            sw.checked = !sw.checked; // put it back: the change was not saved
            $("audioError").style.display = "block";
            $("audioError").textContent = "Could not save the switch. Is the server running?";
        } finally {
            savingSwitch = false;
            sw.disabled = false;
        }
    });

    async function loadStatus() {
        try {
            const res = await fetch(statusUrl);
            if (!res.ok) throw new Error(res.status);
            renderStatus(await res.json());
        } catch (e) {
            setBadge($("sensorBadge"), "Server unreachable", "red");
        }
    }

    // ------------------------------------------------------------
    // History chart
    // ------------------------------------------------------------
    function buildChart() {
        if (typeof Chart === "undefined") return null; // CDN not reachable
        return new Chart($("historyChart"), {
            type: "line",
            data: {
                labels: [],
                datasets: [
                    { label: "Temperature (°C)", data: [], borderColor: "#dc2626", backgroundColor: "#dc2626", yAxisID: "y", tension: 0.3, pointRadius: 0, spanGaps: true },
                    { label: "Humidity (%)", data: [], borderColor: "#2563eb", backgroundColor: "#2563eb", yAxisID: "y1", tension: 0.3, pointRadius: 0, spanGaps: true },
                    { label: "Weight (kg)", data: [], borderColor: "#047857", backgroundColor: "#047857", yAxisID: "y2", tension: 0.3, pointRadius: 0, spanGaps: true, hidden: true },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                interaction: { mode: "index", intersect: false },
                scales: {
                    x: { ticks: { maxTicksLimit: 8 } },
                    y: { position: "left", title: { display: true, text: "°C" } },
                    y1: { position: "right", title: { display: true, text: "%" }, grid: { drawOnChartArea: false } },
                    y2: { display: false },
                },
            },
        });
    }

    function label(isoString) {
        const d = new Date(isoString);
        return historyHours <= 24
            ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
            : d.toLocaleDateString([], { day: "2-digit", month: "short" }) + " " +
              d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }

    async function loadHistory() {
        try {
            const res = await fetch(historyUrl(historyHours));
            if (!res.ok) throw new Error(res.status);
            const data = await res.json();

            $("emptyChart").style.display = data.points.length ? "none" : "flex";
            if (!chart) chart = buildChart();
            if (!chart) return;

            chart.data.labels = data.points.map((p) => label(p.recorded_at));
            chart.data.datasets[0].data = data.points.map((p) => p.temperature_c);
            chart.data.datasets[1].data = data.points.map((p) => p.humidity_pct);
            chart.data.datasets[2].data = data.points.map((p) => p.weight_kg);
            chart.update();
        } catch (e) {
            // keep the last chart on temporary errors
        }
    }

    $("rangeButtons").addEventListener("click", (e) => {
        const hours = e.target.dataset.hours;
        if (!hours) return;
        historyHours = Number(hours);
        document.querySelectorAll("#rangeButtons button").forEach((b) => b.classList.toggle("active", b === e.target));
        loadHistory();
    });

    loadStatus();
    loadHistory();
    setInterval(loadStatus, STATUS_EVERY_MS);
    setInterval(loadHistory, HISTORY_EVERY_MS);
})();
