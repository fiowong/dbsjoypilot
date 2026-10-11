/* 3/5 personalization.js, load 3rd. CUSTOMER_NAME/PERSONALIZE_MODE state, personalizeActive()/tt(), syncPersonalization(), reportFieldSignal(), and the Personalisation settings-button wiring. Calls popup()/iface()/agent()/audit() from ui.js (already loaded) and postJSON()/auditClassFor() from live.js/scenarios.js, safe because those are only referenced inside functions that run after every file below has finished loading, never during this file's own top-level evaluation. */
// ==========================================================================
// PERSONALIZATION, adapts to observed difficulty in THIS conversation, not
// to any guess about who the customer is. The decision itself is made server-side
// by the Personalization Agent (backend/app/agents.py), which reasons only
// over structured facts about this session (a clarification point that had to be
// raised twice, a field rejected twice, two turns in a row that didn't resolve),
// never the customer's words. This file just reflects that decision and lets the
// customer override it from the "Personalisation" button.
// ==========================================================================
let CUSTOMER_NAME = 'Auntie';    // updated from the trusted session.customer_preferred_name once
                                  // a live turn responds, never taken from anything the customer typed.
                                  // It fills the {name} placeholder in ui.js's fill(); the live path
                                  // arrives already addressed by the backend and needs no filling.
let CUSTOMER_NAME_KNOWN = false; // false until the trusted session actually tells us the name.
                                  // The intro card is drawn BEFORE the first turn, so it must not
                                  // invent one, refreshNameSlots() leaves the greeting nameless
                                  // until this flips, rather than addressing the customer as
                                  // "Auntie", which reads like the assistant talking to itself.
let PERSONALIZE_MODE = 'auto';   // 'auto' | 'on' | 'off', 'auto' follows AUTO_PERSONALIZE
let AUTO_PERSONALIZE = false;    // mirrors the backend's session.personalize_auto
let LAST_PERSONALIZATION_DECISION = null;   // the Personalization Agent's own reason_code, for the settings popup
function personalizeActive(){
  return PERSONALIZE_MODE==='on' || (PERSONALIZE_MODE==='auto' && AUTO_PERSONALIZE);
}
// Picks the wording set to speak with: the plain/simple variant once active,
// falling back to the standard field for anything simple{} doesn't override.
function tt(lang){
  const base = T[lang]||T.EN;
  return personalizeActive() ? Object.assign({}, base, base.simple||{}) : base;
}
function updatePersonalizeBadge(){
  const b=$('personalizeBadge'); if(!b) return;
  b.textContent = personalizeActive() ? 'Personalisation: Adapted' : 'Personalisation: Standard';
  b.classList.toggle('active', personalizeActive());
}
// Reads the Personalization Agent's latest decision off a /turn, /confirm or
// /field_signal response and announces the change once, the moment it actually
// flips, never retroactively, never silently.
function syncPersonalization(session){
  if(!session) return;
  if(session.customer_preferred_name){
    CUSTOMER_NAME = session.customer_preferred_name;
    CUSTOMER_NAME_KNOWN = true;
    // The intro card is drawn before the first turn, when the name is still unknown, so it
    // renders an empty {name} slot rather than a frozen string. Fill it the moment it arrives.
    if(typeof refreshNameSlots === 'function') refreshNameSlots();
  }
  if(typeof session.personalize_auto !== 'boolean') return;
  const before = personalizeActive();
  AUTO_PERSONALIZE = session.personalize_auto;
  if(session.personalization_decision) LAST_PERSONALIZATION_DECISION = session.personalization_decision;
  if(!before && personalizeActive()){
    iface('I\'ll take a little more time from here, and check each detail with you twice, just to be safe.');
  }
  updatePersonalizeBadge();
  // Feature 3: surface Joy adapting to Mei Ling in the inspector, the preferred name plus the
  // wording mode, so the assistant is visibly "learning" across turns, not just obeying.
  const pe = $('stPers');
  if(pe){
    const nm = CUSTOMER_NAME_KNOWN ? CUSTOMER_NAME : 'Mei Ling';
    pe.textContent = personalizeActive() ? (nm + ' · Adapted') : (nm + ' · Standard');
  }
}
// Reports a field-review answer to the backend the instant it happens, so a
// second "no" on the same field is a fact the Personalization Agent can see,
// not something the client only claims after the fact.
async function reportFieldSignal(field, result){
  if(!FREE_SESSION_ID) return;
  try{
    const resp = await postJSON('/field_signal', {session_id: FREE_SESSION_ID, field, result});
    syncPersonalization(resp.session);
    // Rendered through the same stepsForEvent() mapping every other backend response uses, so
    // the backend's own FIELD_EDIT_REQUESTED sentence reaches the chat (instead of the client
    // assembling its own), and a live PERSONALIZATION AGENT card still appears the instant a
    // repeat signal crosses the threshold rather than on the next turn.
    const seenStages=new Set();
    const steps=[];
    for(const ev of (resp.events||[])) steps.push(...stepsForEvent(ev, seenStages));
    await play(steps, RUN);
  }catch(err){ /* best-effort, a failed signal just means slower adaptation, never a broken flow */ }
}

// Picks what Auntie should actually hear back for a live /turn or /confirm response.
// DELETED: composeReply() used to hunt through the event payloads for a clarification_question
// or a message_key and render the sentence here, using the MSG[] map. That made the browser a
// second author of the interface copy. The backend now renders the finished sentence from its
// messages.py catalogue and ships it as an INTERFACE_LINE event, which eventsToSteps() plays
// directly, so there is nothing left for the client to compose.

// Personalisation settings button: shows honestly what's active and why, and
// always lets the customer override it, automatic detection never has the
// final word.
const personalizeBadge = $('personalizeBadge');
if(personalizeBadge){
  personalizeBadge.onclick = async () => {
    const why = {
      REPEAT_REJECTION: "the same detail got corrected twice during review",
      REPEAT_CLARIFICATION: "the same question had to be asked twice",
      STALLED_INTENT: "the conversation wasn't moving forward",
      COMBINED: "a couple of things kept needing to be repeated",
    }[LAST_PERSONALIZATION_DECISION?.reason_code] || "this conversation needed a bit more back-and-forth";
    const statusLine = PERSONALIZE_MODE==='on' ? "You've turned this on yourself."
      : PERSONALIZE_MODE==='off' ? "You've turned this off. Joy won't switch it on itself."
      : personalizeActive() ? `Joy noticed ${why}, so it's using simpler wording and double-checking each detail.`
      : "Joy is using its standard wording. It'll offer to simplify if a conversation needs a bit more back-and-forth.";
    const choice = await popup({
      kicker:'PERSONALISATION', title:'How Joy is adapting',
      body:`${statusLine}<br><br>This only reacts to difficulty in <i>this</i> conversation, never to how someone talks, types, or who they might be.`,
      acts:[
        {id:'on', label:'Keep it simple', kind: PERSONALIZE_MODE==='on'?'yes':''},
        {id:'off', label:'Use standard wording', kind: PERSONALIZE_MODE==='off'?'yes':''},
        {id:'auto', label:'Let Joy decide', kind: PERSONALIZE_MODE==='auto'?'yes':''}
      ],
      note:'You can change this anytime. It never overrides what you choose here.'
    });
    if(choice==='on'||choice==='off'||choice==='auto'){ PERSONALIZE_MODE=choice; updatePersonalizeBadge(); }
  };
}
