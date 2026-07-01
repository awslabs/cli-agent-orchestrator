// Pure decision: after a re-render, should the screen pin to the bottom?
//   "always" → yes. Used by cropped, non-interactive wall tiles that should
//              always show the latest (bottom) of the mirrored screen.
//   "smart"  → only if the user is already at/near the bottom. Used by the
//              focused console so a manual scroll-up to read isn't yanked back
//              down by the next poll.
// `geom` is anything with scrollHeight/scrollTop/clientHeight (e.g. a DOM el).
export function computeFollow(mode, geom) {
  if (mode === "always") return true;
  if (mode === "smart") {
    return geom.scrollHeight - geom.scrollTop - geom.clientHeight < 40;
  }
  return false;
}
