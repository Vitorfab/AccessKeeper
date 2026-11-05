(function () {
  "use strict";

  var box = document.querySelector("[data-duration]");
  if (!box) return;

  var end = document.getElementById(box.dataset.end);
  var startInput = box.dataset.startInput ? document.getElementById(box.dataset.startInput) : null;
  var warning = box.querySelector("[data-warning]");
  var levelWarning = box.querySelector("[data-level-warning]");
  var levelInput = box.dataset.levelInput ? document.getElementById(box.dataset.levelInput) : null;
  var limit = Number(box.dataset.warnDays || 365) * 86400000;
  if (!end) return;

  function level() {
    return levelInput ? levelInput.value : box.dataset.level || "";
  }

  function levelDays() {
    if (level() === "critical") return Number(box.dataset.criticalDays);
    if (level() === "high") return Number(box.dataset.highDays);
    return 0;
  }

  function parse(value) {
    var m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(value || "");
    return m ? Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]) : null;
  }

  function pad(n) {
    return String(n).padStart(2, "0");
  }

  function format(ms) {
    var d = new Date(ms);
    return d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate()) +
      "T" + pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes());
  }

  function startMs() {
    return parse(startInput ? startInput.value : box.dataset.start);
  }

  function baseMs() {
    return parse(box.dataset.base || (startInput && startInput.value));
  }

  function check() {
    var s = startMs();
    var e = parse(end.value);
    var length = s !== null && e !== null ? e - s : null;
    warning.hidden = !(length !== null && length > limit);
    var days = levelDays();
    var show = length !== null && days > 0 && length > days * 86400000;
    if (levelWarning) {
      levelWarning.hidden = !show;
      if (show) {
        levelWarning.textContent = "This " + (level() === "critical" ? "Critical" : "High") +
          " access lasts more than " + days + " days. Consider a shorter duration or an earlier review.";
      }
    }
  }

  box.addEventListener("click", function (event) {
    var button = event.target.closest("button[data-hours]");
    if (!button) return;
    var base = baseMs();
    if (base === null) {
      (startInput || end).focus();
      return;
    }
    end.value = format(base + Number(button.dataset.hours) * 3600000);
    check();
  });

  end.addEventListener("input", check);
  if (levelInput) levelInput.addEventListener("change", check);
  if (startInput) startInput.addEventListener("input", check);
  check();
})();
