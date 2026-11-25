// The drawing helpers the panel's two hosts share (the sidebar and the dialog): a heading, a hint, a problem line,
// a labelled field, a text field, a button, a chip, and the check box and radio the form draws itself. Each takes
// the theme `t` and returns an element. They hold no state.
//
// Unlike the pure modules this one imports host modules, so it cannot run under node and has no node test; the
// mods are checked with `herdr-app check`. The imports carry wg-prefixed names: bundle mode (D8) puts every shared
// module and the mod's own code in one scope, where a second binding of the mod's own `div` would be a syntax error.
import { div as wgDiv } from "gpui-kit";
import { v_flex as wgCol, h_flex as wgRow, Input as wgInput, Textarea as wgTextarea } from "gpui-base";

export const heading = (t, text, flag) => wgRow().gap(6).items_center().flex_wrap()
  .child(wgDiv().text_size(12).font_medium().child(text))
  .when(Boolean(flag), (e) => e.child(wgDiv().flex_none().text_size(10.5).text_color(t.dim).child(flag)));
export const hintLine = (t, text) => wgDiv().text_size(11).text_color(t.dim).child(text);
export const problemLine = (t, text) => wgDiv().text_size(11.5).text_color(t.blocked).child(`\u2715 ${text}`);
// A labelled field: heading with its CLI flag, the control, an optional hint and the problem at the field.
export const fieldBox = (t, head, flag, body, { hint, error } = {}) => wgCol().gap(4)
  .child(heading(t, head, flag)).child(body)
  .when(Boolean(hint), (e) => e.child(hintLine(t, hint)))
  .when(Boolean(error), (e) => e.child(problemLine(t, error)));
// A text field: the retained InputState inside a bordered box (the cao-tab comment box does the same).
export const inputBox = (t, id, state, invalid = false) => wgDiv().id(id).h(28).px(8).flex().items_center().rounded(6).border_1()   // fixed: a one-line text field; its value scrolls and it holds no label
  .border_color(invalid ? t.blocked : t.border).bg(t.input).cursor_text()
  .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
  .child(wgInput.new(state).h_full().text_size(12).text_color(t.foreground));
export const textareaBox = (t, id, state, rows) => wgDiv().id(id).h(rows * 18 + 12).px(8).py(4).rounded(6).border_1().border_color(t.border).bg(t.input).cursor_text()
  .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
  .child(wgTextarea.new(state).size_full().text_size(12).text_color(t.foreground));
// A button. kind: plain | primary | danger. A disabled one has no handler; a dim one looks disabled and still answers a click.
export const pushButton = (t, id, text, { kind = "plain", disabled = false, dim = false, on } = {}) => wgRow().id(id).min_h(28).px(12).py(4).items_center().justify_center().rounded(6).text_size(12)
  .when(kind === "primary", (e) => e.bg(t.solid).text_color(t.solid_foreground))
  .when(kind === "danger", (e) => e.border_1().border_color(t.blocked).text_color(t.blocked))
  .when(kind === "plain", (e) => e.border_1().border_color(t.border).text_color(t.foreground))
  .when(disabled || dim, (e) => e.opacity(0.5))
  .when(!disabled && Boolean(on), (e) => e.cursor_pointer().hover((s) => s.bg(t.hover)).on_click((_ev, cx) => { cx.stop_propagation(); on(cx); }))
  .child(text);
export const chip = (t, id, text, selected, on) => wgRow().id(id).min_h(22).px(8).py(2).items_center().rounded(11).border_1().text_size(11.5)
  .border_color(selected ? t.accent : t.border).text_color(selected ? t.foreground : t.muted).when(selected, (e) => e.bg(t.selected))
  .cursor_pointer().hover((s) => s.bg(t.hover)).on_click((_e, cx) => { cx.stop_propagation(); on(cx); }).child(text);
// A check box or radio the form draws itself (the base components are unstyled).
export const markBox = (t, round, on) => wgDiv().size(14).flex_none().rounded(round ? 7 : 3).border_1().border_color(on ? t.accent : t.border).flex().items_center().justify_center()
  .when(on, (e) => e.child(wgDiv().size(round ? 6 : 8).rounded(round ? 3 : 1).bg(t.accent)));
export const choiceRow = (t, id, round, on, text, note, onClick, danger = false) => wgRow().id(id).gap(8).items_start().cursor_pointer().on_click((_e, cx) => { cx.stop_propagation(); onClick(cx); })
  .child(wgDiv().pt(1).child(markBox(t, round, on)))
  .child(wgCol().gap(1).flex_1().min_w(0).child(wgDiv().text_size(12).text_color(danger ? t.blocked : t.foreground).child(text))
    .when(Boolean(note), (e) => e.child(wgDiv().text_size(11).text_color(t.dim).child(note))));
