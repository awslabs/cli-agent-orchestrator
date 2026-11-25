// The top-left row of the CAO window's left sidebar: Herdr's own row with the CAO mark and "CAO" where Herdr draws
// its logo and "Herdr", so the window is clearly the CAO window. Herdr draws this row with its shipped `workspace-chrome` mod in the layout's
// `brand` block; make-window.jq points that block here, and the sidebar's footer stays on `workspace-chrome`. Each part
// below names the lines of that mod (mods/workspace-chrome/main.js of Herdr 0.1.17) that it mirrors, so a Herdr that
// changes the row is found by reading them again. The two icons are files every Herdr home holds; the mark is the
// project's own, two drawings in this folder. It does nothing but draw the row and run the two buttons.
import { View, div, image, svg } from "gpui-kit";
import { h_flex } from "gpui-base";
import { theme, run, window, shared, setShared, layout } from "herdr";
import { BRAND_TEXT, BRAND_MARK_SIZE, BRAND_MARK_RADIUS, brandMark } from "./shell.js";

// workspace-chrome/main.js:5-12, a block of the layout by its id.
function brandFind(node, id) {
  if (node?.id === id) return node;
  for (const child of node?.children ?? []) {
    const found = brandFind(child, id);
    if (found) return found;
  }
  return null;
}

// workspace-chrome/main.js:15, the rail goes by the sidebar's width, not a flag: dragging its edge narrows it too.
const brandNarrow = () => (brandFind(layout(), "sidebar")?.size?.px ?? Infinity) < 160;

// workspace-chrome/main.js:18-24, to the rail (on macOS wide enough for the traffic lights), or back to the width it had.
// The sidebar's range (min 200) clamps the 76 px: the fold button leaves it at 200 px.
function brandToggle() {
  const width = brandFind(layout(), "sidebar")?.size?.px;
  const rail = brandNarrow();
  if (!rail) setShared("sidebar.width", width);
  setShared("sidebar.collapsed", !rail);   // the flag that mods older than the width rule still read
  run("layout.resize", { id: "sidebar", px: rail ? (shared("sidebar.width") ?? 268) : window()?.platform === "macos" ? 76 : 44 });
}

export default class CaoBrand extends View {
  // workspace-chrome/main.js:27-31, a 26 px button that does not start a window drag.
  button(id, icon, action) {
    const t = theme();
    return div().id(id).size(26).flex_none().flex().items_center().justify_center().rounded(6).text_color(t.muted).hover(s => s.bg(t.hover).text_color(t.foreground))
      .on_mouse_down("left", (_e, cx) => cx.stop_propagation()).on_click(action).child(icon);
  }

  // workspace-chrome/main.js:48-53, the row: padding, room for the macOS traffic lights, the mark and the text, the search
  // button and the fold button. On the macOS rail the traffic lights fill the row, and the fold button is not drawn. The text takes the weight of
  // the person's font_weight setting like the rows of the window; Herdr's own wordmark (:50) says font_semibold.
  // The mark is a full-colour `image()` (`svg()` tints one colour), the light or the dark drawing by the theme's brightness (brandMark).
  render() {
    const t = theme();
    const collapsed = brandNarrow();
    return h_flex().size_full().bg(t.chrome).pl(collapsed ? 6 : 14).pr(10).gap(4).items_center().text_size(13.5)
      .when(window()?.platform === "macos" && !window()?.fullscreen && !collapsed, el => el.pl(76))
      .when(!collapsed, el => el.child(h_flex().flex_1().gap(8).items_center().child(image(brandMark(t)).size(BRAND_MARK_SIZE).rounded(BRAND_MARK_RADIUS)).child(div().text_color(t.foreground).child(BRAND_TEXT))))
      .when(!collapsed, el => el.child(this.button("search", svg("mods/workspace-chrome/search.svg").size(15), () => run("mod.open", { name: "picker" }))))
      .when(!(collapsed && window()?.platform === "macos"), el => el.child(this.button("sidebar-toggle", svg("mods/workspace-chrome/sidebar.svg").size(15), brandToggle)));
  }
}
