/* 1.5/6 icons.js — load right after translations.js and BEFORE ui.js.
   A single stroke-based SVG icon set for the DBS app surface.

   Why this file exists: the dashboard tiles and the mic button used OS emoji for primary
   banking actions. Emoji render differently on every platform and read as an unverified
   draft next to real banking chrome. These are plain inline SVG paths, drawn on a 24x24
   grid with a 1.75px stroke, so they inherit `currentColor` and stay crisp at any size.

   Usage:  icon('mic')                 -> <svg class="ic">…</svg>
           icon('mic','ic-lg')         -> extra class for sizing
   Never hard-code an emoji into banking chrome again — add a path here instead. */

const ICONS = {
  /* ---- actions ---- */
  mic:      '<rect x="9" y="2" width="6" height="13" rx="3"/><path d="M12 19v3"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/>',
  send:     '<path d="M21 3 10.5 13.5"/><path d="M21 3 14.5 21l-4-7.5L3 9.5 21 3Z"/>',
  transfer: '<path d="M8 7h12m0 0-4-4m4 4-4 4"/><path d="M16 17H4m0 0 4 4m-4-4 4-4"/>',
  paynow:   '<rect x="6" y="2.5" width="12" height="19" rx="3"/><path d="M12 16.5V8.5"/><path d="m9.2 11.3 2.8-2.8 2.8 2.8"/>',
  scan:     '<path d="M3 8V5a2 2 0 0 1 2-2h3"/><path d="M16 3h3a2 2 0 0 1 2 2v3"/><path d="M21 16v3a2 2 0 0 1-2 2h-3"/><path d="M8 21H5a2 2 0 0 1-2-2v-3"/><rect x="9" y="9" width="6" height="6" rx="1"/>',
  card:     '<rect x="2.5" y="5" width="19" height="14" rx="2.5"/><path d="M2.5 10h19"/><path d="M6.5 14.5h3"/>',
  user:     '<path d="M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z"/><path d="M4.5 20a7.5 7.5 0 0 1 15 0"/>',
  plus:     '<circle cx="12" cy="12" r="9"/><path d="M12 8v8"/><path d="M8 12h8"/>',
  receipt:  '<path d="M6 3h12v18l-3-2-3 2-3-2-3 2V3Z"/><path d="M9.5 8h5"/><path d="M9.5 12h5"/>',

  /* ---- navigation ---- */
  chevronRight: '<path d="m9 6 6 6-6 6"/>',
  arrowLeft:    '<path d="M19 12H5"/><path d="m11 18-6-6 6-6"/>',

  /* ---- trust / security ---- */
  shieldCheck: '<path d="M12 3 4.5 6v6c0 4.4 3.1 8.2 7.5 9.2 4.4-1 7.5-4.8 7.5-9.2V6L12 3Z"/><path d="m9 12 2 2 4-4"/>',
  lock:        '<rect x="4.5" y="10.5" width="15" height="10" rx="2.5"/><path d="M8 10.5V7a4 4 0 0 1 8 0v3.5"/>',
  faceid:      '<path d="M3 8V5.5A2.5 2.5 0 0 1 5.5 3H8"/><path d="M16 3h2.5A2.5 2.5 0 0 1 21 5.5V8"/><path d="M21 16v2.5a2.5 2.5 0 0 1-2.5 2.5H16"/><path d="M8 21H5.5A2.5 2.5 0 0 1 3 18.5V16"/><path d="M9 9.5v2"/><path d="M15 9.5v2"/><path d="M9.5 15.5a3.5 3.5 0 0 0 5 0"/>',

  /* ---- status ---- */
  check:       '<path d="m5 13 4.5 4.5L19 7"/>',
  checkCircle: '<circle cx="12" cy="12" r="9"/><path d="m8.5 12.5 2.5 2.5 4.5-5"/>',
  alert:       '<path d="M12 3.5 2.5 20h19L12 3.5Z"/><path d="M12 10v4"/><path d="M12 17.2h.01"/>',
  sparkle:     '<path d="M12 3.5 13.7 9l5.5 1.7-5.5 1.7L12 18l-1.7-5.6L4.8 10.7 10.3 9 12 3.5Z"/>',
  globe:       '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a14 14 0 0 1 0 18 14 14 0 0 1 0-18Z"/>',
};

/* Renders one icon. `cls` is optional extra class names (sizing/colour live in styles.css,
   so an icon never needs an inline style). aria-hidden because every icon in this app sits
   beside a real text label — the label is what a screen reader should read. */
function icon(name, cls){
  const body = ICONS[name] || '';
  return `<svg class="ic${cls ? ' ' + cls : ''}" viewBox="0 0 24 24" fill="none" stroke="currentColor"`
       + ` stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">`
       + body + `</svg>`;
}
