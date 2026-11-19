// One CSS string injected as <style data-cao-shelf>. Controls are the host's own App Kit components;
// this sheet only lays them out and styles the parts the kit has no component for (card body, chips,
// disclosures, section headers, the question block). Every colour is a host token read with no
// fallback, so dark mode and theme packs are whatever the host paints. Dialogs render through the
// host's portal outside .cs-root, so the classes used inside them are not scoped under it.
// Rules: status sentences are --text with a dot in the status hue (ruling 7), raw hues are for dots,
// borders and tints only, -text tokens only for the "Why" label and validation errors, and muted
// text sits only on --card or --bg (on a tint or --bg-hover the text is --text).

export const CSS = `
.cs-root{font-size:14px;line-height:1.4;position:relative}
.cs-root *,.cs-root *::before,.cs-root *::after{box-sizing:border-box}
.cs-sr{position:absolute;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.cs-muted{color:var(--muted)}
.cs-mono{font-family:var(--mono)}

/* buttons: host Btn through components/Button.tsx */
.cs-btn{justify-content:center;text-align:center}
.cs-btn[aria-expanded="true"]{background:var(--bg-hover);border-color:var(--border-strong)}
.cs-btn[aria-pressed="true"]{background:var(--accent-subtle);border-color:var(--accent);color:var(--text-strong)}
.cs-quiet{border-color:transparent}

/* tier toggles and the honest line */
.cs-tiers{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:4px;margin:0 -4px}
.cs-tier{white-space:nowrap;font-weight:600}
.cs-honest{margin:0;font-size:12px;color:var(--muted);padding:4px 0 8px}
.cs-honest b{color:var(--text)}

/* sections */
.cs-sec{margin:16px 0 8px;font-size:11.5px;font-weight:600;color:var(--muted);display:flex;align-items:center;gap:8px}
.cs-sec::after{content:"";flex:1;height:1px;background:var(--border)}
.cs-none{margin:0 0 8px;font-size:12px;color:var(--muted)}
.cs-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,300px),1fr));gap:12px;padding:0 0 8px}
.cs-dim .cs-card{opacity:.55;filter:saturate(.6)}

/* card: a host Card; no tier fill, so chips and muted text always sit on --card */
.cs-card{margin:0;padding:12px;display:flex;flex-direction:column;gap:8px;min-width:0;container-type:inline-size}
.cs-k-needs_you{box-shadow:inset 3px 0 0 var(--warn),var(--shadow-sm)}
.cs-k-needs_you.cs-err{box-shadow:inset 3px 0 0 var(--danger),var(--shadow-sm)}
.cs-k-stale{opacity:.66;filter:saturate(.5)}
.cs-card:hover,.cs-card:focus-within{opacity:1;filter:none}
.cs-head{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:4px 8px}
.cs-ttl{flex:1 1 auto;min-width:0;overflow-wrap:anywhere;background:transparent;border:0;margin:0;padding:2px 0;text-align:left;cursor:pointer;font:inherit;color:inherit;border-radius:var(--radius-md)}
.cs-nm{font-size:14px;font-weight:600;color:var(--text-strong)}
.cs-tag{flex:none}
.cs-cond{font-size:13px;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.cs-cond .cs-prof{overflow-wrap:anywhere}
.cs-dot{display:inline-block;width:9px;height:9px;border-radius:50%;border:1.8px solid var(--muted);flex:none}
.cs-dot-processing{background:var(--ok);border-color:var(--ok);animation:cs-pulse 1.2s ease-in-out infinite}
.cs-dot-waiting_user_answer{background:var(--warn);border-color:var(--warn)}
.cs-dot-completed{background:var(--info);border-color:var(--info)}
.cs-dot-error{background:var(--danger);border-color:var(--danger)}
@keyframes cs-pulse{50%{box-shadow:0 0 0 4px color-mix(in srgb,var(--ok) 25%,transparent)}}
@media (prefers-reduced-motion:reduce){.cs-dot-processing{animation:none}.cs-skel{animation:none}}

/* helper chips: outline pills that wrap inside the card */
.cs-chips{list-style:none;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:6px}
.cs-chip{display:inline-flex;flex-wrap:wrap;align-items:center;gap:2px 6px;max-width:100%;border:1px solid var(--border-strong);background:var(--card);border-radius:var(--radius-lg);padding:2px 10px 2px 8px;font:inherit;font-size:12px;color:inherit}
.cs-chip>span{min-width:0;overflow-wrap:anywhere}
.cs-chip small{color:var(--muted);font-size:12px}
.cs-chip-orphan{border:1.5px dashed var(--danger);background:var(--danger-subtle)}
.cs-chip-orphan small{color:var(--text)}
button.cs-chip{cursor:pointer}
.cs-note{margin:0;font-size:12px;color:var(--muted)}
/* status sentences are --text with a dot in the host status hue: a host -text token is only 3:1 on
   the card in some dark theme packs (design ruling 7), and a dot needs 3:1, words 4.5:1 */
.cs-note-orphan{color:var(--text)}
.cs-hof{margin:0;font-size:12px;color:var(--text);font-weight:600}
.cs-hof::before,.cs-note-orphan::before{content:"";display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:baseline}
.cs-hof::before{background:var(--ok)}
.cs-note-orphan::before{background:var(--danger)}

/* the question block */
.cs-need{padding:8px 10px;border-radius:var(--radius-md);background:var(--warn-subtle);border:1px solid var(--warn);font-size:13px;color:var(--text);display:flex;flex-direction:column;align-items:flex-start;gap:8px}
.cs-need-error{background:var(--danger-subtle);border-color:var(--danger)}
.cs-need em{font-style:normal;color:var(--text)}
.cs-need .cs-note{color:var(--text)}
.cs-need>*{max-width:100%}
.cs-need .cs-row{display:flex;gap:8px;flex-wrap:wrap}
/* on the tint a resting suggested answer keeps the card surface, so it reads as a button in dark mode too */
.cs-need .cs-row .cs-btn:not(:hover){background:var(--card)}
.cs-need .cs-form{align-self:stretch}
.cs-q{overflow-wrap:anywhere}
.cs-pre{font-family:var(--mono);font-size:12px;line-height:1.4;white-space:pre-wrap;overflow-wrap:anywhere;margin:0;padding:6px 8px;border-radius:var(--radius-md);background:var(--bg-hover);color:var(--text);border:1px solid var(--border);max-height:12em;overflow:auto;align-self:stretch}

/* relay: neutral on purpose, the only accent fills on a card are primary buttons */
.cs-relay{display:flex;flex-direction:column;gap:8px}
.cs-relay-on{padding:8px 10px;border-radius:var(--radius-md);border:1px solid var(--border-strong);background:var(--bg-hover);color:var(--text);font-size:13px;display:flex;flex-wrap:wrap;gap:2px 8px;align-items:baseline}
.cs-relay-on small{color:var(--text)}
.cs-relay-ttl{overflow-wrap:anywhere;min-width:0}
.cs-relay-on .cs-row{flex:1 1 100%;display:grid;grid-template-columns:repeat(auto-fit,minmax(8rem,1fr));gap:8px;margin-top:4px}
/* on the neutral fill a resting button keeps the card surface, so its edge shows in dark mode too; the host hover fill still applies */
.cs-relay-on .cs-btn:not(:hover){background:var(--card)}
.cs-notice{padding:8px 10px;border-radius:var(--radius-md);font-size:13px;color:var(--text)}
.cs-notice[data-tone="warn"]{background:var(--warn-subtle);border-left:3px solid var(--warn)}
.cs-notice .cs-disc summary{color:var(--warn-text);font-weight:600}

/* disclosure: native details, a chevron that turns when open */
.cs-disc{border:1px solid var(--border);border-radius:var(--radius-md);font-size:13px}
.cs-disc>summary{cursor:pointer;font-weight:600;padding:6px 10px;list-style:none;display:flex;align-items:center;gap:8px;border-radius:var(--radius-md)}
.cs-disc>summary::-webkit-details-marker{display:none}
.cs-disc>summary::before{content:"";width:6px;height:6px;border-right:2px solid currentColor;border-bottom:2px solid currentColor;transform:rotate(-45deg);flex:none}
.cs-disc[open]>summary::before{transform:rotate(45deg)}
.cs-disc[open]>summary{border-bottom:1px solid var(--border);border-radius:var(--radius-md) var(--radius-md) 0 0}
.cs-disc p{margin:8px 10px;overflow-wrap:anywhere}
.cs-disc--bare{border:0;margin-top:4px}
.cs-disc--bare>summary{padding:0;display:inline-flex}
.cs-disc--bare[open]>summary{border-bottom:0}
.cs-disc--bare p{margin:4px 0 0}
/* what remote control means: a sentence and a closed disclosure, once near the top of the page */
.cs-rc-intro{margin:4px 0 8px;font-size:13px;color:var(--text)}
.cs-rc-intro>p{margin:0;overflow-wrap:anywhere}
.cs-rc-intro .cs-disc p{max-width:72ch}

/* the action row: 2 by 2, 4 across on a wide card */
.cs-acts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:auto}
@container (min-width:460px){.cs-acts{grid-template-columns:repeat(4,auto)}}

/* details, history, forms */
.cs-det{font-size:12px;border-top:1px solid var(--border);padding-top:6px;overflow-x:auto}
.cs-det table{min-width:100%;width:max-content;border-collapse:collapse}
.cs-det th{text-align:left;color:var(--muted);font-weight:600;padding:1px 8px 1px 0;white-space:nowrap}
.cs-det td{padding:1px 8px 1px 0;font-family:var(--mono);font-size:12px;white-space:nowrap}
.cs-det p{margin:6px 0 0;color:var(--muted)}
.cs-hist{padding:8px 10px;background:var(--bg-hover);color:var(--text);border-radius:var(--radius-md);font-size:13px}
.cs-hist ul{list-style:none;margin:4px 0 0;padding:0}
.cs-hist li{margin:8px 0;display:flex;flex-wrap:wrap;align-items:center;gap:4px 8px}
.cs-hist small{color:var(--text);font-size:12px}
.cs-hist .cs-rows{flex:1 1 100%}
.cs-rows{margin:0;padding-left:8px;border-left:2px solid var(--border);font-family:var(--mono);font-size:12px;overflow-wrap:anywhere}
.cs-form{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.cs-input{width:100%;display:block}
.cs-form .cs-input{width:auto;flex:1 1 12rem}
.cs-textarea{width:100%;display:block;background:var(--bg-elevated);border:1px solid var(--border);border-radius:var(--radius-md);padding:8px 12px;color:var(--text);font:inherit;font-size:14px;resize:vertical}
.cs-foot{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:12px 0 4px}
.cs-foot>span{font-size:12px;color:var(--muted)}
.cs-box{margin:8px 0;padding:12px 16px;font-size:13px}
.cs-box-title{flex-wrap:wrap;margin:0 0 8px}
.cs-box-title small{font-weight:400;font-size:12px}
.cs-box ul{margin:4px 0 0;padding-left:18px}
.cs-box label{display:block;margin:12px 0 4px;font-weight:600}
.cs-box .cs-input{max-width:420px}
.cs-help{display:block;margin-top:4px;font-size:12px;color:var(--muted)}
.cs-switch{display:flex;gap:8px;align-items:center;margin-top:16px}
.cs-gap{margin:8px 0}
.cs-live-region{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)}
.cs-skel{height:120px;border-radius:var(--radius-lg)}

/* inside dialogs (portaled outside .cs-root) */
/* the host clips a long title to one line; the session name must stay readable */
.cs-dlg-title{white-space:normal;overflow-wrap:anywhere}
.cs-select{width:100%}
.cs-lead{margin:0 0 8px;font-size:13px;overflow-wrap:anywhere}
.cs-small{display:block;font-size:12px;color:var(--muted)}
.cs-bad{display:block;margin-top:4px;font-size:12px;color:var(--danger-text)}
.cs-field{display:block;margin:12px 0 4px;font-weight:600;font-size:13px}
.cs-checkrow{display:flex;gap:8px;align-items:center;margin:12px 0 0;font-size:13px}
.cs-check{width:16px;height:16px;margin:0;flex:none;accent-color:var(--accent)}
.cs-list{list-style:none;margin:8px 0;padding:0}
.cs-list li{margin:8px 0}
.cs-list .cs-checkrow{align-items:flex-start;margin:0}
.cs-risk{margin:8px 0}
.cs-risk>p{margin:0 0 8px;font-size:13px;overflow-wrap:anywhere}
.cs-stack{display:flex;flex-direction:column;gap:8px}
.cs-actions{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:8px;flex:1}
.cs-push{margin-right:auto}

/* narrow */
@media (max-width:640px){
  .cs-grid{grid-template-columns:minmax(0,1fr)}
  .cs-tiers{flex-wrap:nowrap;overflow-x:auto;padding:4px;-webkit-overflow-scrolling:touch}
  .cs-actions>*{flex:1 1 auto}
}
@media (max-width:640px),(pointer:coarse){
  .cs-btn,.cs-disc summary,.cs-check{min-height:44px}
  .cs-input,.cs-textarea{min-height:44px}
}
`
