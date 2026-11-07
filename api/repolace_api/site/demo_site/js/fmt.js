// Number formats. Every value shown comes from demo.json; these only choose how it is written.
export const usd = (x, d = 4) => "$" + Number(x).toFixed(d);
export const int = (x) => Number(x).toLocaleString("en-US");
export const secs = (x) => (x < 10 ? Number(x).toFixed(1) : Math.round(x).toString()) + " s";
export function clock(total) {
  const m = Math.floor(total / 60);
  const s = Math.round(total - m * 60);
  return m > 0 ? `${m} min ${String(s).padStart(2, "0")} s` : `${s} s`;
}
export const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`;
