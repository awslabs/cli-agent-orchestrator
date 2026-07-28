# Round 1 (2026-07-28, pre-pin-correction): 14 passed, 1 failed in 133.40s

The one failure was red-by-design: case-08 (declared `/compact` on a visually
empty composer) was refused `composer-nonempty` with the detail

> "the composer's emptiness could not be proven (the input region was
> unreadable or unparseable), and a declared command is written only against
> a proven-empty composer; zero bytes were written"

because the then-pin (`_KIMI_INPUT_RULE`, a `── input ──` rule from older
in-tree fixtures) could not see the installed 0.29.2's rounded composer box.
case-07's refusal in round 1 fired through the same unproven path (zero
bytes, prefill byte-identical — fail-closed and safe, but the pin never saw
the box). The pin was corrected from this evidence (see the README's F1
section); round 2 is 15 passed in 139.69s.
