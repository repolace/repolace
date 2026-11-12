// The demo video: plays muted and looped while it is on screen, with one Pause/Play button.
// No native controls (the page wants a clean frame), so the button is the way to stop moving content.
// Visitors who ask for reduced motion get the poster and a Play button instead of autoplay.

export function initWalkthrough() {
  const video = document.querySelector("#walkthrough-video");
  const button = document.querySelector("#walkthrough-toggle");
  if (!video || !button) return;

  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let pausedByVisitor = reduceMotion;

  const label = () => {
    button.textContent = video.paused ? "Play video" : "Pause video";
  };
  const play = () => video.play().then(label, label);

  button.addEventListener("click", () => {
    if (video.paused) {
      pausedByVisitor = false;
      play();
    } else {
      pausedByVisitor = true;
      video.pause();
      label();
    }
  });
  video.addEventListener("click", () => button.click());
  video.addEventListener("play", label);
  video.addEventListener("pause", label);

  // Start when it scrolls into view; pause when it leaves, so it neither downloads nor runs off screen.
  if ("IntersectionObserver" in window) {
    new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            if (!pausedByVisitor) play();
          } else if (!video.paused) {
            video.pause();
          }
        }
      },
      { threshold: 0.4 },
    ).observe(video);
  } else if (!pausedByVisitor) {
    play();
  }
  label();
}
