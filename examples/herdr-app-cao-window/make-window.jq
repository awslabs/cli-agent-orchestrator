# Derives the CAO window's layout from a base `main` layout (task 4.4 of add-herdr-app-cao-window, reworked
# by D25, D27, D28, D36 and D37).
#   jq --argjson props "$(cat layouts/props.default.json)" [--argjson toast true] -f make-window.jq main.json > cao-window.json
# The base may be Herdr's own `main` or a layout this script derived earlier (also the earlier three-block
# one). The script
#   1. removes every block of the mod `cao-window-side`, wherever it stands, and adds the two left blocks:
#      `cao-sessions` above `cao-agents`, each of weight 1;
#   2. sets the fields of the top bar (`cao-window-tabs`), the right sidebar (`cao-window-panel`) and the base
#      `status` block on the blocks where they stand, adding the first two at their anchors (first in `work`,
#      last in `work-body`) when the base has none. It never removes and re-adds them: the `status` block is
#      Herdr's own block, which the anchor check needs and whose size and place stay;
#   3. removes only Herdr's own `spaces`, `agents` and `tabs` blocks, sets the root `toast` key only with
#      `--argjson toast true`, and stamps `props` on every CAO block;
#   4. lets the user drag the left sidebar's edge (D37): the `sidebar` column keeps its width (the base's 268 px)
#      and gets the range `min` 200 and `max` 640. What lets the edge drag is the ROOT row's `resizable` (default
#      true: the boundaries between its children drag), so the root is set to true; the sidebar column's own
#      `resizable: false` stays, because it locks only the boundaries between its own children (brand, lists,
#      footer), which are fixed blocks.
#   5. points the sidebar's `brand` block, the top-left row that Herdr draws as its logo and the text "Herdr" with its
#      `workspace-chrome` mod, at the window's own `cao-window-brand` mod, which draws the same row with "CAO" (D37 S). Only
#      that block, and only when its mod is Herdr's `workspace-chrome` or already ours: the sidebar footer (the same Herdr
#      mod with `props.footer`) stays Herdr's, a brand block of any other mod or with no content is left as it is, and a
#      base with no brand block is not given one.
# A second run on its own output changes nothing. A base layout without an anchor the script needs, or props
# that are not a JSON object, is refused: jq exits 5, names the problem and prints nothing.
def has_id($i): any(.. | objects; (.id? // "") == $i);
def need($i):
  if has_id($i) then .
  else error("make-window.jq: the base layout has no \"" + $i + "\" block or column; Herdr's main changed shape (see README, Troubleshooting)")
  end;
def cao_mod: ((.content? // null) | if type == "object" then ((.mod? // "") | tostring) else "" end);
def is_cao: cao_mod | startswith("cao-window-");
def is_side: cao_mod == "cao-window-side";
def is_herdr_own: ((.id? // "") | IN("spaces", "agents", "tabs"));
def brand_row: (.id? // "") == "brand" and (cao_mod | IN("workspace-chrome", "cao-window-brand"));
def without_children(pred):
  if type == "object" and ((.children? // null) | type) == "array"
  then .children |= map(select(pred | not))
  else . end;
def side($id; $section):
  {id: $id, type: "block", size: {weight: 1}, min: 34,
   content: {mod: "cao-window-side", props: ($props + {section: $section})}};
def top_bar:
  {id: "cao-window-tabs", type: "block", size: {px: 34}, min: 34, window_drag: true,
   content: {mod: "cao-window-tabs", props: $props}};
def right_sidebar:
  {id: "cao-window-panel", type: "block", size: {px: 368}, hidden: true, min: 280, max: 900,
   content: {mod: "cao-window-panel", props: $props}};
def at_id($i; f): if type == "object" and (.id? // "") == $i then f else . end;
def drag_range: .min = 200 | .max = 640;

(if ($props | type) == "object" then .
 else error("make-window.jq: --argjson props must be a JSON object")
 end)
| need("sidebar-lists") | need("work") | need("work-body") | need("status")
| walk(without_children(is_side))
| walk(at_id("sidebar-lists"; .children = [side("cao-sessions"; "sessions"), side("cao-agents"; "agents")] + (.children // [])))
| if has_id("cao-window-tabs") then . else walk(at_id("work"; .children = [top_bar] + (.children // []))) end
| if has_id("cao-window-panel") then . else walk(at_id("work-body"; .children = (.children // []) + [right_sidebar])) end
| walk(at_id("cao-window-tabs"; . + top_bar)
       | at_id("cao-window-panel"; . + right_sidebar)
       | at_id("status"; .content = {mod: "cao-window-status", props: $props}))
| walk(without_children(is_herdr_own))
| walk(at_id("root"; .resizable = true) | at_id("sidebar"; drag_range))
| walk(if type == "object" and brand_row then .content = {mod: "cao-window-brand"} else . end)
| walk(if type == "object" and is_cao
       then .content.props = ($props + (if ((.content.props? // null) | type) == "object" and (.content.props.section? // null) != null then {section: .content.props.section} else {} end))
       else . end)
| if ($ARGS.named.toast // false) == true then .toast = "cao-window-toast"
  elif (.toast? // null) == "cao-window-toast" then del(.toast)
  else . end
