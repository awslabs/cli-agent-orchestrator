// The honesty table audit (cao-window-honesty-labels, "Table complete"). Pure, so the test can feed it
// broken tables and see each failure mode.
export async function auditHonesty({ expected, honesty, sources, pending, load }) {
  const problems = [];
  const reported = [];
  for (const surface of expected) {
    const entry = sources[surface];
    if (!entry) { problems.push(`${surface}: no registry entry`); continue; }
    const label = honesty[entry.label];
    if (typeof label !== "string" || label.trim() === "") { problems.push(`${surface}: empty or missing label "${entry.label}"`); continue; }
    if (Object.hasOwn(pending, surface)) { reported.push(`${surface} (section ${pending[surface]})`); continue; }
    let mod;
    try { mod = await load(entry.module); } catch (e) { problems.push(`${surface}: cannot load ${entry.module}: ${e.message}`); continue; }
    if (typeof mod[entry.fn] !== "function") { problems.push(`${surface}: ${entry.module} has no function ${entry.fn}`); continue; }
    let view;
    try { view = await mod[entry.fn](...entry.args(mod)); } catch (e) { problems.push(`${surface}: ${entry.fn} threw: ${e.message}`); continue; }
    if (!JSON.stringify(view ?? null).includes(label)) problems.push(`${surface}: the view model of ${entry.fn} lacks the label "${label}"`);
  }
  for (const surface of Object.keys(pending)) if (!expected.includes(surface)) problems.push(`${surface}: pending but not in the table`);
  return { problems, pending: reported };
}
