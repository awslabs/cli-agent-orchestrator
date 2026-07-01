// Pure mapping from a tile's state to its poll interval in ms.
// 0 means "do not poll". A focused console always keeps polling (even when the
// last poll errored) so its live view self-heals when the node comes back.
export function intervalFor({ focused, status }) {
  if (focused) return 800;
  if (status === "offline") return 0;
  if (status === "working") return 1000;
  return 3000; // idle / online-but-waiting
}
