// Load the recorded run. The page shows only what is in data/demo.json; nothing is typed here.
export async function loadDemo() {
  const res = await fetch("data/demo.json", { cache: "no-cache" });
  if (!res.ok) throw new Error("demo.json: HTTP " + res.status);
  return res.json();
}

// The lead's hand-check note is an optional separate file; the page shows it only when it exists and has text.
export async function loadHandCheck() {
  try {
    const res = await fetch("data/hand_check.json", { cache: "no-cache" });
    if (!res.ok) return null;
    const j = await res.json();
    return typeof j.text === "string" && j.text.trim() ? j.text.trim() : null;
  } catch (e) {
    return null;
  }
}
