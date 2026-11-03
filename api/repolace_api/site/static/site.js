// Theme toggle and scroll reveal for the public site. Same behaviour, and the same localStorage key ("theme"),
// as the benchmark report (benchmark_site/js/core.js), so a visitor's choice carries between the two.
// External file because the site's CSP forbids inline script.
(function () {
  var root = document.documentElement;

  var btn = document.getElementById("theme");
  if (btn) {
    var isDark = function () {
      var set = root.getAttribute("data-theme");
      return (set || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")) === "dark";
    };
    btn.addEventListener("click", function () {
      var next = isDark() ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("theme", next); } catch (e) { /* private window: the toggle still works for this visit */ }
    });
  }

  var nodes = document.querySelectorAll(".rise");
  if (matchMedia("(prefers-reduced-motion: reduce)").matches || !("IntersectionObserver" in window)) {
    nodes.forEach(function (n) { n.classList.add("is-in"); });
    return;
  }
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (e.isIntersecting) { e.target.classList.add("is-in"); io.unobserve(e.target); }
    });
  }, { rootMargin: "0px 0px -8% 0px" });
  nodes.forEach(function (n) { io.observe(n); });
})();
