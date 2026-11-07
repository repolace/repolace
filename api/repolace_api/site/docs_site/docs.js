// Theme toggle for the documentation pages: the same "theme" key and behaviour as the benchmark report's core.js.
(function () {
  var btn = document.getElementById("theme");
  if (!btn) return;
  var root = document.documentElement;
  function isDark() {
    var set = root.getAttribute("data-theme");
    return (set || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")) === "dark";
  }
  btn.addEventListener("click", function () {
    var next = isDark() ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("theme", next); } catch (e) { /* private window: the toggle still works for this visit */ }
  });
})();
