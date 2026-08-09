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

  // --- keep the active-run pill honest ------------------------------------
  if (document.querySelector(".pill.running")) {
    setInterval(function () {
      fetch("/api/runs/active")
        .then(function (r) { return r.json(); })
        .then(function (data) { if (!data.runs.length) window.location.reload(); })
        .catch(function () {});
    }, 5000);
  }

  // --- live Duration on Runs/History's still-running rows ------------------
  // Mirrors analytics.py's _duration() bucketing exactly, so the live-ticked
  // text and the eventual server-rendered final value never visibly jump.
  function formatDuration(seconds) {
    seconds = Math.max(0, Math.floor(seconds));
    if (seconds < 60) return seconds + "s";
    if (seconds < 3600) return Math.floor(seconds / 60) + "m " + (seconds % 60) + "s";
    return Math.floor(seconds / 3600) + "h " + (Math.floor(seconds % 3600 / 60)) + "m";
  }
  var runningRows = document.querySelectorAll("tr.is-running[data-started-at]");
  if (runningRows.length) {
    setInterval(function () {
      runningRows.forEach(function (row) {
        var started = new Date(row.getAttribute("data-started-at")).getTime();
        var el = row.querySelector(".duration-live");
        if (el) el.textContent = formatDuration((Date.now() - started) / 1000);
      });
    }, 1000);
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
  // --- settings: blocked-companies chip list --------------------------------
  var chipList = document.getElementById("blocked-companies-chips");
  var chipHidden = document.getElementById("blocked-companies-hidden");
  var chipAddInput = document.getElementById("blocked-company-add");
  var chipAddBtn = document.getElementById("blocked-company-add-btn");
  if (chipList && chipHidden) {
    function syncBlockedCompaniesHidden() {
      var names = Array.prototype.slice
        .call(chipList.querySelectorAll(".chip"))
        .map(function (c) { return c.getAttribute("data-company"); });
      chipHidden.value = names.join(",");
    }
    function addChip(name) {
      name = name.trim();
      if (!name) return;
      var existing = Array.prototype.slice.call(chipList.querySelectorAll(".chip"))
        .map(function (c) { return c.getAttribute("data-company").toLowerCase(); });
      if (existing.indexOf(name.toLowerCase()) !== -1) return;
      var chip = document.createElement("span");
      chip.className = "chip removable";
      chip.setAttribute("data-company", name);
      chip.textContent = name + " ";
      var removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.className = "chip-remove";
      removeBtn.setAttribute("aria-label", "Remove");
      removeBtn.textContent = "×";
      removeBtn.addEventListener("click", function () {
        chip.remove();
        syncBlockedCompaniesHidden();
      });
      chip.appendChild(removeBtn);
      chipList.appendChild(chip);
      syncBlockedCompaniesHidden();
    }
    chipList.querySelectorAll(".chip-remove").forEach(function (btn) {
      btn.addEventListener("click", function () {
        btn.closest(".chip").remove();
        syncBlockedCompaniesHidden();
      });
    });
    if (chipAddBtn) {
      chipAddBtn.addEventListener("click", function () {
        addChip(chipAddInput.value);
        chipAddInput.value = "";
        chipAddInput.focus();
      });
    }
    if (chipAddInput) {
      chipAddInput.addEventListener("keydown", function (event) {
        if (event.key === "Enter") {
          event.preventDefault();
          addChip(chipAddInput.value);
          chipAddInput.value = "";
        }
      });
    }
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
    var frequencyMode = scheduleForm.elements["frequency_mode"];
    var minuteIntervalField = scheduleForm.elements["minute_interval"];
    var intervalDaysField = scheduleForm.elements["interval_days"];
    var stageField = scheduleForm.elements["stage"];
    var jobrightHint = document.getElementById("jobright-hint");
    var fieldMinuteInterval = document.getElementById("field-minute-interval");
    var fieldIntervalDays = document.getElementById("field-interval-days");
    var fieldHour = document.getElementById("field-hour");
    var fieldMinute = document.getElementById("field-minute");
    var fieldDays = document.getElementById("field-days");

    // "cron" — daily/weekly at a fixed HH:MM, optionally on specific days.
    // "minutes" — every N minutes, all day (cron_minute becomes "*/N",
    // cron_hour/day_of_week become "*").
    // "days" — every N days at a fixed HH:MM, via IntervalTrigger
    // (interval_days set; cron_day_of_week is irrelevant to it).
    function applyFrequencyMode(mode) {
      fieldMinuteInterval.classList.toggle("hidden", mode !== "minutes");
      fieldIntervalDays.classList.toggle("hidden", mode !== "days");
      fieldHour.classList.toggle("hidden", mode === "minutes");
      fieldMinute.classList.toggle("hidden", mode === "minutes");
      fieldDays.classList.toggle("hidden", mode !== "cron");
    }
    frequencyMode.addEventListener("change", function () {
      applyFrequencyMode(frequencyMode.value);
    });
    applyFrequencyMode(frequencyMode.value);

    function updateJobrightHint() {
      jobrightHint.classList.toggle("hidden", stageField.value !== "jobright-fetch");
    }
    stageField.addEventListener("change", updateJobrightHint);
    updateJobrightHint();

    function resetForm() {
      scheduleForm.reset();
      editIdField.value = "";
      submitBtn.textContent = "Add schedule";
      title.textContent = "Add a schedule";
      cancelBtn.style.display = "none";
      frequencyMode.value = "cron";
      applyFrequencyMode("cron");
      updateJobrightHint();
    }

    cancelBtn.addEventListener("click", resetForm);

    document.querySelectorAll("[data-schedule-edit]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        editIdField.value = btn.getAttribute("data-schedule-edit");
        scheduleForm.elements["name"].value = btn.getAttribute("data-name");
        scheduleForm.elements["stage"].value = btn.getAttribute("data-stage");
        scheduleForm.elements["extra"].value = btn.getAttribute("data-extra");
        scheduleForm.elements["timezone"].value = btn.getAttribute("data-timezone");
        var hour = btn.getAttribute("data-hour");
        var minute = btn.getAttribute("data-minute");
        var days = btn.getAttribute("data-days");
        var intervalDays = btn.getAttribute("data-interval-days");
        var minuteStep = /^\*\/(\d+)$/.exec(minute || "");
        var mode = "cron";
        if (intervalDays) {
          mode = "days";
          intervalDaysField.value = intervalDays;
          scheduleForm.elements["cron_hour"].value = hour;
          scheduleForm.elements["cron_minute"].value = minute;
        } else if (minuteStep && hour === "*") {
          mode = "minutes";
          minuteIntervalField.value = minuteStep[1];
        } else {
          scheduleForm.elements["cron_hour"].value = hour;
          scheduleForm.elements["cron_minute"].value = minute;
          scheduleForm.elements["cron_day_of_week"].value = days;
        }
        frequencyMode.value = mode;
        applyFrequencyMode(mode);
        updateJobrightHint();
        submitBtn.textContent = "Save changes";
        title.textContent = "Edit schedule";
        cancelBtn.style.display = "inline-flex";
        window.scrollTo(0, 0);
      });
    });

    scheduleForm.addEventListener("submit", function (event) {
      event.preventDefault();
      // Translate the friendly frequency-mode UI into the raw cron_minute/
      // cron_hour/cron_day_of_week/interval_days fields the backend actually
      // stores, right before serializing — the visible "Every N minutes"/
      // "Every N days" controls aren't real schedule columns themselves.
      var mode = frequencyMode.value;
      if (mode === "minutes") {
        var n = parseInt(minuteIntervalField.value, 10) || 1;
        scheduleForm.elements["cron_minute"].value = "*/" + n;
        scheduleForm.elements["cron_hour"].value = "*";
        scheduleForm.elements["cron_day_of_week"].value = "*";
        intervalDaysField.value = "";
      } else if (mode === "days") {
        scheduleForm.elements["cron_day_of_week"].value = "*";
      } else {
        intervalDaysField.value = "";
      }
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
