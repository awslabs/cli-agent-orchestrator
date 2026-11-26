// Two starting points for New workflow: a Python script and a sequential YAML spec, two steps each.
// test/examples/test_herdr_app_cao_window_templates.py posts each to the real POST /workflows/validate
// route, so a template cannot drift into something the server refuses. The profile names are
// placeholders: New workflow shows a note (TEMPLATE_NOTE) asking the user to replace them with installed
// profiles; nothing picks them. The YAML template's name: line is rewritten to the form's name when the
// template is applied (lib/workflows-view.js templateFor).
export const TEMPLATES = [
  {
    id: "python",
    label: "Python script, two steps",
    tier: "python",
    name: "hello-review",
    source: `from cao_workflow import run_step, emit_output

note = run_step("claude_code", "developer", "Write a one-line hello note. Return it only.")
review = run_step("claude_code", "reviewer", f"Critique this note in one line: {note.output}")
emit_output({"note": note.output, "review": review.output})
`,
  },
  {
    id: "yaml",
    label: "Sequential YAML spec, two steps",
    tier: "yaml",
    name: "hello-review-yaml",
    source: `name: hello-review-yaml
description: Write a one-line note, then review it.
mode: sequential
steps:
  - id: write
    provider: claude_code
    agent: developer
    prompt: Write a one-line hello note. Return it only.
  - id: review
    provider: claude_code
    agent: reviewer
    prompt: "Critique this note in one line: {{steps.write.output}}"
`,
  },
];
