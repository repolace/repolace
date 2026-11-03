// Runs before first paint: marks JS-capable pages and applies a stored theme. External, not inline, because the site's CSP forbids inline script.
document.documentElement.classList.add("js");
try {
  var t = localStorage.getItem("theme");
  if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
} catch (e) {}
