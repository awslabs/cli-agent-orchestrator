# A2UI metric preview

Renders one allow-listed `emit_ui` component, `metric`, as an A2UI v0.9.1 surface
carried on `ACTIVITY_SNAPSHOT`, and refuses an off-list component. See
[Post-3.0 previews](../../../docs/agui.md#post-30-previews).

```sh
uv run python examples/ag-ui/a2ui-metric-preview/render_metric.py
```

The script needs no server and no flag. The server route that does the same work,
`POST /agui/v1/preview/a2ui`, is off unless `CAO_AGUI_POST3_PREVIEW` is set.

The renderer here is a small reference one for the `Card`, `Column` and `Text`
components of the A2UI Basic Catalog. It is not the A2UI project's own renderer.
