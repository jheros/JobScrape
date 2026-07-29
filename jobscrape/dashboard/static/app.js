/* Dashboard behaviour. Vanilla only — the page must work offline. */

(function () {
  "use strict";

  // --- theme toggle: the stamp must beat the OS setting in both directions --
  var root = document.documentElement;
  var saved = localStorage.getItem("jobscrape-theme");
  if (saved) root.setAttribute("data-theme", saved);

  var toggle = document.querySelector(".theme-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
      var current = root.getAttribute("data-theme") || (prefersDark ? "dark" : "light");
      var next = current === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      localStorage.setItem("jobscrape-theme", next);
    });
  }

  // --- run control --------------------------------------------------------
  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body || "",
    }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, data: j }; }); });
  }

  document.querySelectorAll("[data-start-stage]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var stage = btn.getAttribute("data-start-stage");
      var extra = btn.getAttribute("data-extra") || "";
      btn.disabled = true;
      btn.textContent = "Starting…";
      post("/api/runs/" + stage + "/start", "extra=" + encodeURIComponent(extra))
        .then(function (res) {
          if (!res.ok) {
            alert(res.data.error || "Could not start");
            btn.disabled = false;
            btn.textContent = btn.getAttribute("data-label") || "Start";
            return;
          }
          window.location.href = "/runs";
        });
    });
  });

  document.querySelectorAll("[data-stop-stage]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var stage = btn.getAttribute("data-stop-stage");
      if (!confirm("Stop the " + stage + " run? Progress already written is kept.")) return;
      btn.disabled = true;
      post("/api/runs/" + stage + "/stop").then(function () {
        setTimeout(function () { window.location.reload(); }, 800);
      });
    });
  });

  // --- live log via SSE ---------------------------------------------------
  var logEl = document.getElementById("live-log");
  if (logEl) {
    var runId = logEl.getAttribute("data-run-id");
    var source = new EventSource("/api/runs/" + runId + "/events");
    source.onmessage = function (event) {
      var payload = JSON.parse(event.data);
      if (payload.done) {
        var end = document.createElement("div");
        end.textContent = "— run finished (" + payload.status + ") —";
        logEl.appendChild(end);
        source.close();
        setTimeout(function () { window.location.reload(); }, 1500);
        return;
      }
      var line = document.createElement("div");
      if (payload.level === "warn") line.className = "warn";
      if (payload.level === "error") line.className = "error";
      line.textContent = payload.message;
      logEl.appendChild(line);
      // Only autoscroll when already at the bottom, so reading isn't yanked.
      var atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 60;
      if (atBottom) logEl.scrollTop = logEl.scrollHeight;
    };
    source.onerror = function () { source.close(); };
  }

  // --- per-job triage -----------------------------------------------------
  document.querySelectorAll(".status-select").forEach(function (select) {
    select.addEventListener("change", function () {
      var source = select.getAttribute("data-source");
      var jobId = select.getAttribute("data-job-id");
      var row = select.closest("tr");
      post(
        "/api/jobs/" + encodeURIComponent(source) + "/" + encodeURIComponent(jobId) + "/status",
        "status=" + encodeURIComponent(select.value)
      ).then(function (res) {
        if (!res.ok) { alert(res.data.error || "Could not save"); return; }
        if (row) {
          row.style.transition = "opacity .25s";
          row.style.opacity = ".45";
        }
      });
    });
  });

  // --- bulk triage --------------------------------------------------------
  var bulkBar = document.getElementById("bulk-bar");
  function selectedIds() {
    return Array.prototype.slice
      .call(document.querySelectorAll(".row-check:checked"))
      .map(function (c) { return c.getAttribute("data-token"); });
  }
  function refreshBulk() {
    if (!bulkBar) return;
    var n = selectedIds().length;
    bulkBar.style.display = n ? "flex" : "none";
    var label = document.getElementById("bulk-count");
    if (label) label.textContent = n + " selected";
  }
  document.querySelectorAll(".row-check").forEach(function (box) {
    box.addEventListener("change", refreshBulk);
  });
  var selectAll = document.getElementById("check-all");
  if (selectAll) {
    selectAll.addEventListener("change", function () {
      document.querySelectorAll(".row-check").forEach(function (box) {
        box.checked = selectAll.checked;
      });
      refreshBulk();
    });
  }
  document.querySelectorAll("[data-bulk-status]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var ids = selectedIds();
      if (!ids.length) return;
      post("/api/jobs/bulk",
        "ids=" + encodeURIComponent(ids.join(",")) +
        "&status=" + encodeURIComponent(btn.getAttribute("data-bulk-status"))
      ).then(function () { window.location.reload(); });
    });
  });

  // --- keep the active-run pill honest ------------------------------------
  if (document.querySelector(".pill.running")) {
    setInterval(function () {
      fetch("/api/runs/active")
        .then(function (r) { return r.json(); })
        .then(function (data) { if (!data.runs.length) window.location.reload(); })
        .catch(function () {});
    }, 5000);
  }
})();
