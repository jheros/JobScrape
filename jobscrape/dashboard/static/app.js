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

  // --- sidebar drawer (narrow screens only — CSS keeps it always-open above
  // the 720px breakpoint, so these buttons have nothing to toggle there) ---
  var sidebar = document.getElementById("sidebar");
  var backdrop = document.getElementById("sidebar-backdrop");
  var menuToggle = document.getElementById("menu-toggle");
  var sidebarClose = document.getElementById("sidebar-close");
  function closeSidebar() {
    if (sidebar) sidebar.classList.remove("open");
    if (backdrop) backdrop.classList.remove("open");
  }
  if (menuToggle) {
    menuToggle.addEventListener("click", function () {
      if (sidebar) sidebar.classList.add("open");
      if (backdrop) backdrop.classList.add("open");
    });
  }
  if (sidebarClose) sidebarClose.addEventListener("click", closeSidebar);
  if (backdrop) backdrop.addEventListener("click", closeSidebar);

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
      var confirmMsg = btn.getAttribute("data-confirm");
      if (confirmMsg && !confirm(confirmMsg)) return;
      var stage = btn.getAttribute("data-start-stage");
      var extra = btn.getAttribute("data-extra") || "";
      var label = btn.textContent;
      btn.disabled = true;
      btn.textContent = "Starting…";
      post("/api/runs/" + stage + "/start", "extra=" + encodeURIComponent(extra))
        .then(function (res) {
          if (!res.ok) {
            alert(res.data.error || "Could not start");
            btn.disabled = false;
            btn.textContent = btn.getAttribute("data-label") || label;
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

  // --- fetch in a date range -----------------------------------------------
  var fetchRangeForm = document.getElementById("fetch-range-form");
  if (fetchRangeForm) {
    fetchRangeForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var submitBtn = fetchRangeForm.querySelector("button[type=submit]");
      var label = submitBtn.textContent;
      submitBtn.disabled = true;
      submitBtn.textContent = "Starting…";
      var body = new URLSearchParams(new FormData(fetchRangeForm)).toString();
      post("/api/runs/fetch/start", body).then(function (res) {
        if (!res.ok) {
          alert(res.data.error || "Could not start");
          submitBtn.disabled = false;
          submitBtn.textContent = label;
          return;
        }
        window.location.href = "/runs";
      });
    });

    // Presets only ever set "From date" and clear "To date" (open-ended
    // through today) — the same "bound since, leave until open" shape the
    // daily/weekly schedule already uses. Computed client-side; the server
    // never sees "12h" or "1 month", only the resulting calendar date.
    document.querySelectorAll("[data-preset-hours]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var hours = parseInt(btn.getAttribute("data-preset-hours"), 10);
        var since = new Date(Date.now() - hours * 3600 * 1000);
        var iso = since.getFullYear() + "-"
          + String(since.getMonth() + 1).padStart(2, "0") + "-"
          + String(since.getDate()).padStart(2, "0");
        document.getElementById("fetch-since").value = iso;
        document.getElementById("fetch-until").value = "";
      });
    });
  }

  // --- jobright-fetch in a date range --------------------------------------
  // A separate block rather than reusing the one above: that one hardcodes
  // #fetch-since/#fetch-until by id, so wiring this card into it would make
  // its presets silently fill the *other* card's date fields.
  var jobrightRangeForm = document.getElementById("jobright-range-form");
  if (jobrightRangeForm) {
    jobrightRangeForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var submitBtn = jobrightRangeForm.querySelector("button[type=submit]");
      var label = submitBtn.textContent;
      submitBtn.disabled = true;
      submitBtn.textContent = "Starting…";
      var body = new URLSearchParams(new FormData(jobrightRangeForm)).toString();
      post("/api/runs/jobright-fetch/start", body).then(function (res) {
        if (!res.ok) {
          alert(res.data.error || "Could not start");
          submitBtn.disabled = false;
          submitBtn.textContent = label;
          return;
        }
        window.location.href = "/runs";
      });
    });

    document.querySelectorAll("[data-jr-preset-hours]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var hours = parseInt(btn.getAttribute("data-jr-preset-hours"), 10);
        var since = new Date(Date.now() - hours * 3600 * 1000);
        var iso = since.getFullYear() + "-"
          + String(since.getMonth() + 1).padStart(2, "0") + "-"
          + String(since.getDate()).padStart(2, "0");
        document.getElementById("jobright-since").value = iso;
        document.getElementById("jobright-until").value = "";
      });
    });
  }

  // --- apify-pull in a date range -------------------------------------------
  // Own block/ids, same reasoning as the jobright one above.
  var apifyRangeForm = document.getElementById("apify-range-form");
  if (apifyRangeForm) {
    apifyRangeForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var submitBtn = apifyRangeForm.querySelector("button[type=submit]");
      var label = submitBtn.textContent;
      submitBtn.disabled = true;
      submitBtn.textContent = "Starting…";
      var body = new URLSearchParams(new FormData(apifyRangeForm)).toString();
      post("/api/runs/apify-pull/start", body).then(function (res) {
        if (!res.ok) {
          alert(res.data.error || "Could not start");
          submitBtn.disabled = false;
          submitBtn.textContent = label;
          return;
        }
        window.location.href = "/runs";
      });
    });

    document.querySelectorAll("[data-apify-preset-hours]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var hours = parseInt(btn.getAttribute("data-apify-preset-hours"), 10);
        var since = new Date(Date.now() - hours * 3600 * 1000);
        var iso = since.getFullYear() + "-"
          + String(since.getMonth() + 1).padStart(2, "0") + "-"
          + String(since.getDate()).padStart(2, "0");
        document.getElementById("apify-since").value = iso;
        document.getElementById("apify-until").value = "";
      });
    });
  }

  // --- live log via SSE ---------------------------------------------------
  // One element per run (`id="live-log-<run_id>"`), not a single shared id:
  // a composite schedule run and a manually-started, non-conflicting stage
  // can be active at the same time, and `getElementById` only ever returns
  // the first match — this used to mean only one of several concurrent logs
  // actually streamed.
  document.querySelectorAll("[id^='live-log-']").forEach(function (logEl) {
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
  });

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

  // --- settings: reveal-on-click secrets, save, restart --------------------
  document.querySelectorAll(".reveal-toggle").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var input = btn.previousElementSibling;
      input.type = input.type === "password" ? "text" : "password";
    });
  });

  var settingsForm = document.getElementById("settings-form");
  if (settingsForm) {
    settingsForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var body = new URLSearchParams(new FormData(settingsForm)).toString();
      post("/api/settings", body).then(function (res) {
        if (!res.ok) { alert(res.data.error || "Could not save"); return; }
        document.getElementById("settings-banner").style.display = "flex";
        window.scrollTo(0, 0);
      });
    });
  }
  var restartBtn = document.getElementById("settings-restart");
  if (restartBtn) {
    restartBtn.addEventListener("click", function () {
      if (!confirm("Restart the dashboard now? It only comes back automatically "
        + "if it's running under a supervisor (see docs/DEPLOYMENT.md).")) return;
      post("/api/settings/restart").then(function () {
        restartBtn.textContent = "Restarting…";
        restartBtn.disabled = true;
      });
    });
  }

  // --- schedule page --------------------------------------------------------
  var scheduleForm = document.getElementById("schedule-form");
  if (scheduleForm) {
    var submitBtn = document.getElementById("schedule-form-submit");
    var cancelBtn = document.getElementById("schedule-form-cancel");
    var title = document.getElementById("schedule-form-title");
    var editIdField = scheduleForm.elements["edit_id"];

    function resetForm() {
      scheduleForm.reset();
      editIdField.value = "";
      submitBtn.textContent = "Add schedule";
      title.textContent = "Add a schedule";
      cancelBtn.style.display = "none";
    }

    cancelBtn.addEventListener("click", resetForm);

    document.querySelectorAll("[data-schedule-edit]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        editIdField.value = btn.getAttribute("data-schedule-edit");
        scheduleForm.elements["name"].value = btn.getAttribute("data-name");
        scheduleForm.elements["stage"].value = btn.getAttribute("data-stage");
        scheduleForm.elements["extra"].value = btn.getAttribute("data-extra");
        scheduleForm.elements["cron_hour"].value = btn.getAttribute("data-hour");
        scheduleForm.elements["cron_minute"].value = btn.getAttribute("data-minute");
        scheduleForm.elements["cron_day_of_week"].value = btn.getAttribute("data-days");
        scheduleForm.elements["timezone"].value = btn.getAttribute("data-timezone");
        submitBtn.textContent = "Save changes";
        title.textContent = "Edit schedule";
        cancelBtn.style.display = "inline-flex";
        window.scrollTo(0, 0);
      });
    });

    scheduleForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var editId = editIdField.value;
      var body = new URLSearchParams(new FormData(scheduleForm)).toString();
      var url = editId ? "/api/schedule/" + editId : "/api/schedule";
      post(url, body).then(function (res) {
        if (!res.ok) { alert(res.data.error || "Could not save"); return; }
        window.location.reload();
      });
    });

    document.querySelectorAll("[data-schedule-run]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var id = btn.getAttribute("data-schedule-run");
        btn.disabled = true;
        post("/api/schedule/" + id + "/run-now").then(function (res) {
          if (!res.ok) { alert(res.data.error || "Could not start"); btn.disabled = false; return; }
          window.location.href = "/runs";
        });
      });
    });

    document.querySelectorAll("[data-schedule-toggle]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var id = btn.getAttribute("data-schedule-toggle");
        post("/api/schedule/" + id + "/toggle").then(function () { window.location.reload(); });
      });
    });

    document.querySelectorAll("[data-schedule-delete]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (!confirm("Delete this schedule?")) return;
        var id = btn.getAttribute("data-schedule-delete");
        post("/api/schedule/" + id + "/delete").then(function () { window.location.reload(); });
      });
    });
  }
})();
