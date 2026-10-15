/* Accessibility glue for the htmx-polled regions. Loaded with `defer`; no inline script (CSP).
   htmx itself runs with allowEval off, so conditional polling cannot be written as an
   `[expression]` trigger filter: pause-while-hidden is done here instead. */
(function () {
  "use strict";

  var announcer = null;
  var errorBox = null;
  var seen = new Map(); // task id -> "status|stage" last announced
  var pendingLoadMore = null; // id of the last task row before a "Load more" request

  function state(el) {
    return (el.getAttribute("data-task-status") || "") + "|" + (el.getAttribute("data-task-stage") || "");
  }

  function announce(el) {
    var id = el.getAttribute("data-task-id");
    if (!id) return;
    var now = state(el);
    var before = seen.get(id);
    seen.set(id, now);
    if (before === undefined || before === now || !announcer) return;
    var status = el.getAttribute("data-task-status");
    var stage = el.getAttribute("data-task-stage");
    var text = "Task #" + el.getAttribute("data-issue") + " is now " + status;
    if (stage) text += ", stage " + stage;
    // Only the changed text is written, once, into one stable live region.
    announcer.textContent = text;
  }

  function showError(message) {
    if (!errorBox) return;
    errorBox.textContent = message;
    errorBox.hidden = false;
  }

  document.addEventListener("DOMContentLoaded", function () {
    announcer = document.getElementById("announcer");
    errorBox = document.getElementById("htmx-error");
    document.querySelectorAll("[data-task-id]").forEach(function (el) {
      seen.set(el.getAttribute("data-task-id"), state(el));
    });
    // A server-rendered form error: move focus to it (autofocus on a non-control is unreliable).
    var target = document.querySelector("[data-focus-on-load]");
    if (target) target.focus();
  });

  // New or replaced content: announce a changed status or stage; move focus after "Load more".
  document.addEventListener("htmx:load", function (evt) {
    var el = evt.target;
    if (!(el instanceof Element)) return;
    if (el.hasAttribute("data-task-id")) announce(el);
    if (pendingLoadMore && el.matches('tr[id^="task-"]')) {
      var link = el.querySelector("a.rowlink");
      pendingLoadMore = null;
      if (link) link.focus();
    }
  });

  document.addEventListener("htmx:beforeRequest", function (evt) {
    var elt = evt.detail && evt.detail.elt;
    if (!elt) return;
    var trigger = elt.getAttribute && elt.getAttribute("hx-trigger");
    // Polling pauses while the tab is hidden: the request is cancelled and the next tick tries again.
    if (trigger && trigger.indexOf("every") === 0 && document.visibilityState === "hidden") {
      evt.preventDefault();
      return;
    }
    if (elt.closest && elt.closest(".loadmore")) {
      var rows = document.querySelectorAll('tr[id^="task-"]');
      pendingLoadMore = rows.length ? rows[rows.length - 1].id : null;
    }
  });

  document.addEventListener("htmx:afterRequest", function (evt) {
    if (evt.detail && evt.detail.successful && errorBox) errorBox.hidden = true;
  });
  document.addEventListener("htmx:responseError", function (evt) {
    var status = evt.detail && evt.detail.xhr ? evt.detail.xhr.status : "";
    showError("The page could not refresh (server answered " + status + "). It will keep trying; reload if this stays.");
  });
  document.addEventListener("htmx:sendError", function () {
    showError("The page could not reach the server. It will keep trying; reload if this stays.");
  });
})();
