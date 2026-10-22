# Sample note for review

This file is the review target for the skills-lifecycle example's live mode.
The Python snippet below contains exactly one instance of each
`python-review-checklist` item, so a reviewer that applied the full checklist
finds four findings and ends with `Checklist applied: 4/4 items`.

```python
def word_count(text, stop_words=[]):
    try:
        pattern = re.compile(r"\s+")
    except:
        pattern = None
    words = pattern.split(text) if pattern else text.split(" ")
    return [word for word in words if word not in stop_words]
```
