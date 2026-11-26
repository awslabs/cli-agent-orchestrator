// The top-left row of the CAO window's left sidebar: Herdr's own row with the CAO mark and "CAO" where Herdr draws
// its logo and "Herdr", so the window is clearly the CAO window. Herdr draws this row with its shipped `workspace-chrome` mod in the layout's
// `brand` block; make-window.jq points that block here, and the sidebar's footer stays on `workspace-chrome`. Each part
// below names the lines of that mod (mods/workspace-chrome/main.js of Herdr 0.1.17) that it mirrors, so a Herdr that
// changes the row is found by reading them again. The two icons are files every Herdr home holds; the mark is the
// project's own, two drawings in this folder. It does nothing but draw the row and run the two buttons.
import { View, div, image, svg } from "gpui-kit";
import { h_flex } from "gpui-base";
import { theme, run, window, shared, setShared, layout } from "herdr";
import { BRAND_TEXT, BRAND_MARK_SIZE, BRAND_MARK_RADIUS, brandMark, sidebarFold } from "./shell.js";

// workspace-chrome/main.js:18-24 is Herdr's fold: to a 76 px rail, or back to the width it had. This window's sidebar has no rail (its
// lists have no rail form, so its minimum is 200 px, which clamped the 76 px and left Herdr's rail chrome over full lists), so the button
// hides the sidebar, as Cmd-B does, and the top bar's button shows it again; a sidebar an earlier fold left at the rail width gets its
// width back (sidebarFold, lib/shell.js).
function brandToggle() {
  const step = sidebarFold(layout(), shared("sidebar.width"));
  if (step.command === "layout.resize") setShared("sidebar.collapsed", false);   // the flag that mods older than the width rule still read
  run(step.command, step.args);
}

export default class CaoBrand extends View {
  // workspace-chrome/main.js:27-31, a 26 px button that does not start a window drag.
  button(id, icon, action) {
    const t = theme();
    return div().id(id).size(26).flex_none().flex().items_center().justify_center().rounded(6).text_color(t.muted).hover(s => s.bg(t.hover).text_color(t.foreground))
      .on_mouse_down("left", (_e, cx) => cx.stop_propagation()).on_click(action).child(icon);
  }

  // workspace-chrome/main.js:48-53, the row: padding, room for the macOS traffic lights, the mark and the text, the search
  // button and the fold button. The text takes the weight of the person's font_weight setting like the rows of the window; Herdr's own
  // wordmark (:50) says font_semibold. Herdr's row also has a rail form (no mark, no text, no search, and on macOS no fold button); this
  // sidebar is never narrower than 200 px, so the row is always drawn whole.
  // The mark is a full-colour `image()` (`svg()` tints one colour), the light or the dark drawing by the theme's brightness (brandMark).
  render() {
    const t = theme();
    return h_flex().size_full().bg(t.chrome).pl(14).pr(10).gap(4).items_center().text_size(13.5)
      .when(window()?.platform === "macos" && !window()?.fullscreen, el => el.pl(76))
      .child(h_flex().flex_1().gap(8).items_center().child(image(brandMark(t)).size(BRAND_MARK_SIZE).rounded(BRAND_MARK_RADIUS)).child(div().text_color(t.foreground).child(BRAND_TEXT)))
      .child(this.button("search", svg("mods/workspace-chrome/search.svg").size(15), () => run("mod.open", { name: "picker" })))
      .child(this.button("sidebar-toggle", svg("mods/workspace-chrome/sidebar.svg").size(15), brandToggle));
  }
}
