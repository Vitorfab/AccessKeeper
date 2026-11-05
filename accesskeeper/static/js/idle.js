(function () {
  "use strict";

  var body = document.body;
  var idleMs = Number(body.dataset.idleSeconds) * 1000;
  if (!idleMs) return;

  var warnMs = Math.min(30000, Math.floor(idleMs / 2));
  var pingEvery = Math.min(15000, Math.floor(idleMs / 4));
  var meta = document.querySelector('meta[name="csrf-token"]');
  var csrf = meta ? meta.content : "";
  var banner = document.getElementById("idle-warning");
  var counter = document.getElementById("idle-count");
  var KEY = "ak_last_activity";
  var last = Date.now();
  var lastPing = last;
  var lastWrite = 0;
  var leaving = false;

  function shared() {
    try {
      return Number(localStorage.getItem(KEY)) || 0;
    } catch (error) {
      return 0;
    }
  }

  function share(time) {
    try {
      localStorage.setItem(KEY, String(time));
    } catch (error) {}
  }

  function lastActivity() {
    return Math.max(last, shared());
  }

  function post(url) {
    return fetch(url, {
      method: "POST",
      headers: { "X-CSRFToken": csrf },
      credentials: "same-origin",
      keepalive: true
    });
  }

  function leave() {
    if (leaving) return;
    leaving = true;
    var go = function () {
      window.location.replace(body.dataset.loginUrl);
    };
    setTimeout(go, 3000);
    post(body.dataset.expireUrl).then(go, go);
  }

  function ping() {
    lastPing = Date.now();
    post(body.dataset.pingUrl).then(function (response) {
      if (response.status === 401 || response.status === 400) leave();
    }, function () {});
  }

  function activity() {
    if (leaving) return;
    var now = Date.now();
    if (now - lastActivity() >= idleMs) return;
    last = now;
    if (now - lastWrite > 1000) {
      lastWrite = now;
      share(now);
    }
    if (!banner.hidden) {
      banner.hidden = true;
      ping();
    } else if (now - lastPing >= pingEvery) {
      ping();
    }
  }

  ["mousemove", "mousedown", "keydown", "wheel", "scroll", "touchstart", "touchmove"].forEach(
    function (name) {
      document.addEventListener(name, activity, { passive: true, capture: true });
    }
  );

  setInterval(function () {
    if (leaving) return;
    var idle = Date.now() - lastActivity();
    if (idle >= idleMs) {
      banner.hidden = true;
      leave();
    } else if (idle >= idleMs - warnMs) {
      counter.textContent = String(Math.ceil((idleMs - idle) / 1000));
      banner.hidden = false;
    } else if (!banner.hidden) {
      banner.hidden = true;
    }
  }, 1000);
})();
