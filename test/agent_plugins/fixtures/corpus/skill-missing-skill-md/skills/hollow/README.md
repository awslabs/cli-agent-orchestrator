This directory is the fixture for the "skill directory with no SKILL.md" row of
the failure-isolation table: a folder that looks like a skill but has no
`SKILL.md`, which the validator must skip with a `skill.missing_skill_md`
finding while its sibling `alpha` still loads.

The file you are reading exists only so git tracks the directory. Git stores no
empty directories, so without it this fixture vanishes on a fresh clone and the
corpus case passes vacuously in CI while passing honestly on the machine that
created it. Its name is deliberately not `SKILL.md`.
