---
name: python-review-checklist
description: Deterministic Python review checklist shipped with the CAO skills-lifecycle example
---

# Python Review Checklist

Apply every item below, in order, when reviewing Python code. Each item targets
one class of defect, and the example's review target (`fixture/sample_note.md`)
contains exactly one instance of each.

- [ ] Missing imports — every name used at module or function scope is imported.
- [ ] Mutable default arguments — no `def f(x=[])` or `def f(x={})`; default to
      `None` and assign a fresh container inside the function.
- [ ] Bare `except:` clauses — every handler catches a specific exception type.
- [ ] Type hints — every public function annotates its parameters and return
      value.

## Output contract

End every review with exactly one line, and nothing after it:

    Checklist applied: <applied>/<total> items

`<applied>` counts the checklist items the review actually checked. A reviewer
that did not apply the full checklist must not print this line, so anything
downstream can treat the line as proof that this skill's guidance engaged.
