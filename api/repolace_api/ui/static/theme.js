/* Theme: system (default) -> light -> dark. Loaded in <head> without defer so the
   stored choice is applied before first paint. localStorage can throw or come
   back empty (private windows, blocked storage), so every access is wrapped and
   the page is correct without it. No inline script: the CSP forbids it. */
(function () {
  var KEY = "repolace-theme";
  var ORDER = ["system", "light", "dark"];
  var root = document.documentElement;

  function read() {
    try {
      var v = window.localStorage.getItem(KEY);
      return ORDER.indexOf(v) === -1 ? "system" : v;
    } catch (e) {
      return "system";
    }
  }
  function write(v) {
    try {
      window.localStorage.setItem(KEY, v);
    } catch (e) {
      /* storage unavailable: the choice lasts for this page only */
    }
  }
  function apply(v) {
    if (v === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", v);
  }

  var current = read();
  apply(current);

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    function label() {
      // The visible text is the accessible name (WCAG 2.5.3): stable word first, state after it.
      btn.textContent = "Theme: " + current;
    }
    label();
    btn.hidden = false;
    btn.addEventListener("click", function () {
      current = ORDER[(ORDER.indexOf(current) + 1) % ORDER.length];
      write(current);
      apply(current);
      label();
    });
  });
})();
