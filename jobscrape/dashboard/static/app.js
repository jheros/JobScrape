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

  // --- stat tile count-up: 0 -> the server-rendered value on load ----------
  // The final text is already correct HTML (comma-formatted, "%" suffix
  // preserved) — this only re-renders it progressively, never computes it,
  // so a JS-disabled load still shows the real number immediately.
  if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    document.querySelectorAll(".tile .value").forEach(function (el) {
      var raw = el.textContent.trim();
      var match = raw.match(/^([\d,]+)(.*)$/);
      if (!match) return;
      var target = parseInt(match[1].replace(/,/g, ""), 10);
      if (isNaN(target)) return;
      var suffix = match[2] || "";
      var hasComma = match[1].indexOf(",") !== -1;
      var duration = 700;
      var start = null;
      function frame(ts) {
        if (start === null) start = ts;
        var progress = Math.min((ts - start) / duration, 1);
        var eased = 1 - Math.pow(1 - progress, 3);
        var current = Math.round(target * eased);
        el.textContent = (hasComma ? current.toLocaleString("en-US") : String(current)) + suffix;
        if (progress < 1) requestAnimationFrame(frame);
        else el.textContent = raw;
      }
      requestAnimationFrame(frame);
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
      var confirmMsg = btn.getAttribute("data-confirm");
      if (confirmMsg && !confirm(confirmMsg)) return;
      var stage = btn.getAttribute("data-start-stage");
      var extra = btn.getAttribute("data-extra") || "";
      var confirmToken = btn.getAttribute("data-confirm-token") || "";
      var label = btn.textContent;
      btn.disabled = true;
      btn.textContent = "Starting…";
      var body = "extra=" + encodeURIComponent(extra);
      if (confirmToken) body += "&confirm=" + encodeURIComponent(confirmToken);
      post("/api/runs/" + stage + "/start", body)
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

  // --- hiringcafe-fetch in a date range --------------------------------------
  // Own block/ids, same reasoning as the jobright one above.
  var hiringcafeRangeForm = document.getElementById("hiringcafe-range-form");
  if (hiringcafeRangeForm) {
    hiringcafeRangeForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var submitBtn = hiringcafeRangeForm.querySelector("button[type=submit]");
      var label = submitBtn.textContent;
      submitBtn.disabled = true;
      submitBtn.textContent = "Starting…";
      var body = new URLSearchParams(new FormData(hiringcafeRangeForm)).toString();
      post("/api/runs/hiringcafe-fetch/start", body).then(function (res) {
        if (!res.ok) {
          alert(res.data.error || "Could not start");
          submitBtn.disabled = false;
          submitBtn.textContent = label;
          return;
        }
        window.location.href = "/runs";
      });
    });

    document.querySelectorAll("[data-hc-preset-hours]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var hours = parseInt(btn.getAttribute("data-hc-preset-hours"), 10);
        var since = new Date(Date.now() - hours * 3600 * 1000);
        var iso = since.getFullYear() + "-"
          + String(since.getMonth() + 1).padStart(2, "0") + "-"
          + String(since.getDate()).padStart(2, "0");
        document.getElementById("hiringcafe-since").value = iso;
        document.getElementById("hiringcafe-until").value = "";
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
    var cursor = 0;
    var finished = false;
    var reconnectDelay = 1000;
    var source = null;

    function connect() {
      source = new EventSource("/api/runs/" + runId + "/events?after=" + cursor);
      source.onopen = function () { reconnectDelay = 1000; };
      source.onmessage = function (event) {
        var payload = JSON.parse(event.data);
        if (payload.done) {
          finished = true;
          var end = document.createElement("div");
          end.textContent = "— run finished (" + payload.status + ") —";
          logEl.appendChild(end);
          source.close();
          setTimeout(function () { window.location.reload(); }, 1500);
          return;
        }
        cursor = payload.id;
        var line = document.createElement("div");
        if (payload.level === "warn") line.className = "warn";
        if (payload.level === "error") line.className = "error";
        line.textContent = payload.message;
        logEl.appendChild(line);
        // Only autoscroll when already at the bottom, so reading isn't yanked.
        var atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 60;
        if (atBottom) logEl.scrollTop = logEl.scrollHeight;
      };
      // A dropped connection used to just give up here (`source.close()`
      // and nothing else) -- a brief network blip, or the server's own
      // ~20-minute silent-stream timeout on a genuinely still-running but
      // quiet stage (Jobright's Selenium session can run 20-45+ minutes),
      // permanently stalled the live log with no sign anything was wrong.
      // Reconnecting with `?after=<cursor>` (the endpoint already supports
      // resuming from a cursor -- see app.py's `stream_events`) picks up
      // exactly where it left off instead of re-showing the whole log from
      // the start.
      source.onerror = function () {
        source.close();
        if (finished) return;
        setTimeout(connect, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 2, 15000);
      };
    }
    connect();
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
        document.getElementById("settings-banner").classList.remove("hidden");
        window.scrollTo(0, 0);
      });
    });
  }
  // --- settings: blocked-companies chip list --------------------------------
  // Auto-saves on every add/remove, independently of the page's big "Save
  // settings" button -- a chip visibly appearing/disappearing already reads
  // as a completed action, so it should actually be one.
  var chipList = document.getElementById("blocked-companies-chips");
  var chipHidden = document.getElementById("blocked-companies-hidden");
  var chipAddInput = document.getElementById("blocked-company-add");
  var chipAddBtn = document.getElementById("blocked-company-add-btn");
  var chipSaveStatus = document.getElementById("blocked-companies-save-status");
  if (chipList && chipHidden) {
    function syncBlockedCompaniesHidden() {
      var names = Array.prototype.slice
        .call(chipList.querySelectorAll(".chip"))
        .map(function (c) { return c.getAttribute("data-company"); });
      chipHidden.value = names.join(",");
    }
    var chipSaveClearTimer = null;
    function saveBlockedCompanies() {
      if (!chipSaveStatus) return;
      clearTimeout(chipSaveClearTimer);
      chipSaveStatus.textContent = "Saving…";
      chipSaveStatus.classList.remove("error");
      post("/api/settings",
        "JOBSCRAPE_BLOCKED_COMPANIES=" + encodeURIComponent(chipHidden.value)
      ).then(function (res) {
        if (!res.ok) {
          chipSaveStatus.textContent = res.data.error || "Could not save";
          chipSaveStatus.classList.add("error");
          return;
        }
        chipSaveStatus.textContent = "Saved";
        chipSaveClearTimer = setTimeout(function () {
          chipSaveStatus.textContent = "";
        }, 2000);
      });
    }
    function addChip(name) {
      name = name.trim();
      if (!name) return false;
      var existing = Array.prototype.slice.call(chipList.querySelectorAll(".chip"))
        .map(function (c) { return c.getAttribute("data-company").toLowerCase(); });
      if (existing.indexOf(name.toLowerCase()) !== -1) return false;
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
        saveBlockedCompanies();
      });
      chip.appendChild(removeBtn);
      chipList.appendChild(chip);
      syncBlockedCompaniesHidden();
      return true;
    }
    // "Amazon, Microsoft, ..." in one go, not just one name per Add click --
    // one save after the whole batch, not one per name, and none at all if
    // every piece was blank or already blocked.
    function addChipsFromInput(value) {
      var added = value.split(",").map(addChip).some(Boolean);
      if (added) saveBlockedCompanies();
    }
    chipList.querySelectorAll(".chip-remove").forEach(function (btn) {
      btn.addEventListener("click", function () {
        btn.closest(".chip").remove();
        syncBlockedCompaniesHidden();
        saveBlockedCompanies();
      });
    });
    if (chipAddBtn) {
      chipAddBtn.addEventListener("click", function () {
        addChipsFromInput(chipAddInput.value);
        chipAddInput.value = "";
        chipAddInput.focus();
      });
    }
    if (chipAddInput) {
      chipAddInput.addEventListener("keydown", function (event) {
        if (event.key === "Enter") {
          event.preventDefault();
          addChipsFromInput(chipAddInput.value);
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
      cancelBtn.classList.add("hidden");
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
        cancelBtn.classList.remove("hidden");
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
  // --- endpoints page -------------------------------------------------------
  var endpointForm = document.getElementById("endpoint-form");
  if (endpointForm) {
    var epSubmit = document.getElementById("endpoint-form-submit");
    var epCancel = document.getElementById("endpoint-form-cancel");
    var epTitle = document.getElementById("endpoint-form-title");
    var epTokenHint = document.getElementById("endpoint-token-hint");
    var epEditId = endpointForm.elements["edit_id"];
    var epTokenDefaultHint = epTokenHint.innerHTML;

    function resetEndpointForm() {
      endpointForm.reset();
      epEditId.value = "";
      epSubmit.textContent = "Add endpoint";
      epTitle.textContent = "Add an endpoint";
      epTokenHint.innerHTML = epTokenDefaultHint;
      epCancel.classList.add("hidden");
    }
    epCancel.addEventListener("click", resetEndpointForm);

    document.querySelectorAll("[data-endpoint-edit]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        epEditId.value = btn.getAttribute("data-endpoint-edit");
        endpointForm.elements["name"].value = btn.getAttribute("data-name");
        endpointForm.elements["url"].value = btn.getAttribute("data-url");
        endpointForm.elements["token"].value = "";
        epTokenHint.textContent = "Token set — leave blank to keep it, or type a new one to replace it";
        epSubmit.textContent = "Save endpoint";
        epTitle.textContent = "Edit endpoint";
        epCancel.classList.remove("hidden");
        window.scrollTo(0, 0);
      });
    });

    endpointForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var editId = epEditId.value;
      var body = new URLSearchParams(new FormData(endpointForm)).toString();
      post(editId ? "/api/endpoints/" + editId : "/api/endpoints", body).then(function (res) {
        if (!res.ok) { alert(res.data.error || "Could not save"); return; }
        window.location.reload();
      });
    });

    document.querySelectorAll("[data-endpoint-toggle]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        post("/api/endpoints/" + btn.getAttribute("data-endpoint-toggle") + "/toggle")
          .then(function () { window.location.reload(); });
      });
    });

    document.querySelectorAll("[data-endpoint-test]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var label = btn.innerHTML;
        btn.disabled = true;
        btn.textContent = "Testing…";
        post("/api/endpoints/" + btn.getAttribute("data-endpoint-test") + "/test").then(function (res) {
          btn.disabled = false;
          btn.innerHTML = label;
          if (!res.ok) { alert(res.data.error || "Could not test"); return; }
          alert((res.data.ok ? "Connected: " : "Failed: ") + res.data.message);
        });
      });
    });

    function sendNow(btn) {
      btn.disabled = true;
      return post("/api/runs/endpoint-sync/start").then(function (res) {
        if (!res.ok) { alert(res.data.error || "Could not start"); btn.disabled = false; return; }
        window.location.href = "/runs";
      });
    }

    document.querySelectorAll("[data-endpoint-send]").forEach(function (btn) {
      btn.addEventListener("click", function () { sendNow(btn); });
    });

    document.querySelectorAll("[data-endpoint-resend]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var name = btn.getAttribute("data-name");
        if (!confirm("Resend everything to " + name + "?\n\nThis re-sends ALL open jobs to " +
                     name + ", starting now. It can be a large amount of traffic.")) return;
        post("/api/endpoints/" + btn.getAttribute("data-endpoint-resend") + "/reset").then(function (res) {
          if (!res.ok) { alert(res.data.error || "Could not reset"); return; }
          sendNow(btn);
        });
      });
    });

    document.querySelectorAll("[data-endpoint-delete]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (!confirm("Delete the endpoint " + btn.getAttribute("data-name") + "? It will stop receiving jobs.")) return;
        post("/api/endpoints/" + btn.getAttribute("data-endpoint-delete") + "/delete")
          .then(function () { window.location.reload(); });
      });
    });
  }
})();
