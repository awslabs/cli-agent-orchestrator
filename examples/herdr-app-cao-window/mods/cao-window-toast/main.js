// The window's toast mod: the layout's root `toast` mod, installed only with TOAST_MOD=1. Herdr opens one
// copy of it per notification (props: title, body, level, action, overlay, and timeout when given), so a
// copy lives only while its toast shows, and it draws and dismisses the toast itself.
//
// Spike 1.5 was not run, so the shipped behavior is pass-through: every notification is drawn as it
// arrives, a click runs its action, and the Dock badge (cao-window-status) carries the asking count.
// toastStep's pass-through branch is the code path. Holding (the queue, the release on a hand-off, Later)
// would need a copy that outlives the toast to feed focus events and ticks to toastStep, and is not
// built: those branches are exercised by lib/logic.test.mjs only.
import { View, div, image } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { run, closeOverlay, theme, props, checking } from "herdr";
import { shippedToastState, toastStep } from "./logic.js";

const DEFAULT_MS = 6000;
const LEVEL_COLORS = { info: "accent", success: "done", warning: "blocked", error: "ansi1" };

export default class CaoToast extends View {
  init(_initial, cx) {
    const [, effects] = toastStep(shippedToastState({}), { type: "notify", notification: props() }, Date.now());
    this.toast = effects.find((e) => e.type === "show")?.notification ?? null;
    if (checking()) return;                         // a check has no overlay to close and no toast to time
    if (!this.toast) { this.dismiss(); return; }    // a held notification shows nothing
    cx.timer.after(typeof this.toast.timeout === "number" ? this.toast.timeout : DEFAULT_MS, () => this.dismiss());
  }

  dismiss() {
    const overlay = props().overlay;
    if (typeof overlay === "string") closeOverlay(overlay);
  }

  open() {
    const action = this.toast?.action;
    if (action && typeof action.name === "string") run(action.name, action.args ?? {});
    this.dismiss();
  }

  // The notification's icon: a file in the app home (svg, png ...) is an image, anything else is text.
  iconView(t, icon) {
    const box = div().flex_none().w(16).h(16).flex().items_center().justify_center();   // fixed: the icon, one glyph or one image
    return /\.(svg|png|jpe?g|gif|webp)$/i.test(icon) ? box.child(image(icon).size(16)) : box.text_size(13).text_color(t.foreground).child(String(icon));
  }

  render() {
    const t = theme();
    const n = this.toast;
    if (!n) return div();
    return v_flex().id("cao-toast").size_full().p(10).gap(2).rounded(8).bg(t.popover).text_color(t.foreground)
      .border_1().border_color(t[LEVEL_COLORS[n.level] ?? "accent"])
      .when(Boolean(n.action), (e) => e.cursor_pointer().on_click(() => this.open()))
      .child(h_flex().gap(8).items_center()
        .when(Boolean(n.icon), (e) => e.child(this.iconView(t, n.icon)))
        .child(div().flex_1().min_w(0).font_semibold().text_size(13).child(String(n.title ?? "")))
        .child(h_flex().id("cao-toast-close").w(18).h(18).flex_none().items_center().justify_center().rounded(4)   // fixed: a control with one character
          .text_color(t.dim).cursor_pointer().hover((s) => s.bg(t.hover))
          .on_click((_e, cx) => { cx.stop_propagation(); this.dismiss(); }).child("x")))
      .when(Boolean(n.body), (e) => e.child(div().text_size(12).text_color(t.muted).child(String(n.body))));
  }
}
