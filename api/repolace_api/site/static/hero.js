// The hero loop on the landing page: plays muted and looped while it is on screen, with one Pause/Play
// button. The same behaviour as the demo page's video (demo_site/js/walkthrough.js): no native controls,
// so the button is the way to stop moving content, and visitors who ask for reduced motion get the
// poster and a Play button instead of autoplay. A file of its own because site.js returns early for
// those visitors. External because the site's CSP forbids inline script.
(function () {
  var video = document.getElementById("hero-video");
  var button = document.getElementById("hero-toggle");
  if (!video || !button) return;

  var pausedByVisitor = matchMedia("(prefers-reduced-motion: reduce)").matches;
  var label = function () { button.textContent = video.paused ? "Play video" : "Pause video"; };
  var play = function () { video.play().then(label, label); };

  button.addEventListener("click", function () {
    if (video.paused) { pausedByVisitor = false; play(); }
    else { pausedByVisitor = true; video.pause(); label(); }
  });
  video.addEventListener("click", function () { button.click(); });
  video.addEventListener("play", label);
  video.addEventListener("pause", label);

  // Start when it is on screen and stop when it leaves, so it neither downloads nor runs unseen.
  if ("IntersectionObserver" in window) {
    new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) { if (!pausedByVisitor) play(); }
        else if (!video.paused) { video.pause(); }
      });
    }, { threshold: 0.4 }).observe(video);
  } else if (!pausedByVisitor) {
    play();
  }
  label();
})();
