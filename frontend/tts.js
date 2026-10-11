/* 5.5/6 tts.js, the mouth. Loads AFTER live.js (needs API_BASE + guessLang).
 *
 * Speaks the backend's INTERFACE_LINE text by asking our own /tts endpoint, which relays
 * Tencent Cloud TextToVoice and returns raw mono 16-bit PCM. The sample rate is whatever the
 * backend reports in the X-Audio-Sample-Rate header (16 kHz today), never a constant here.
 * Audio plays through a chunked WebAudio queue so the first syllable reaches the customer within a
 * fraction of a second of the bubble appearing, instead of after the whole sentence has
 * been generated. Any new user input (typing a message or pressing the mic) triggers
 * barge-in: stopSpeaking() cuts Auntie off mid-sentence so she never talks over the customer.
 *
 * This file composes no wording of its own: it only voices text the backend already authored.
 *
 * TWO RULES this file exists to respect (both were broken once, and both were audible):
 *
 *   1. A NETWORK CHUNK IS NOT A SET OF WHOLE SAMPLES.
 *      The network hands us arbitrary byte counts, so a 16-bit sample (2 bytes) regularly
 *      gets cut in half across two chunks. Decoding each chunk as if it were complete
 *      either throws (the rest of the sentence is then silently dropped) or shifts every
 *      following sample by one byte, which sounds like crackling. So we carry a trailing
 *      odd byte forward and decode whole samples only.
 *
 *   2. NEVER START AUDIO FROM A JS CALLBACK: SCHEDULE IT ON THE AUDIO CLOCK.
 *      Waiting for a clip's `onended` before starting the next one means waiting on the
 *      main thread, which is busy drawing bubbles, stage cards and audit rows. That wait
 *      is a gap of a few to tens of milliseconds, and with ~70 chunks per sentence it is
 *      70 audible clicks. Instead each buffer is handed to Web Audio with an exact start
 *      time computed up front, so consecutive buffers butt together sample-accurately.
 */
// Fallback sample rate, used ONLY if the /tts response omits X-Audio-Sample-Rate. The
// backend asks Tencent for Codec=pcm and tells us the rate in that header, and we build
// every AudioBuffer from the header rather than from this constant, so the two sides
// cannot drift apart unnoticed. (They did once: this file said 24 kHz while the provider
// had moved to 16 kHz, which shifts the pitch and the timing of every sentence.)
const TTS_SAMPLE_RATE_FALLBACK = 16000;

// How much audio to hold back before starting playback. Big enough to ride out a slow
// chunk without starving the speaker mid-word; small enough that the first syllable is
// still heard in a fraction of a second rather than after the whole sentence is built.
const TTS_START_BUFFER_SEC = 0.12;
// Safety margin: never hand Web Audio a start time at or behind "now", or it starts the
// buffer immediately and the seam with the previous one becomes audible.
const TTS_SCHEDULE_LEAD_SEC = 0.03;

let _ttsCtx = null;
let _ttsChain = null;   // { sources:Set<AudioBufferSourceNode>, stopRequested:bool }, the stream being spoken
let _ttsAbort = null;   // AbortController for the in-flight /tts fetch
let _ttsEnabled = true; // flips to false after a hard failure (e.g. TTS not configured)
let _ttsQueue = Promise.resolve();  // serialises whole utterances: Auntie has one mouth
let _ttsGeneration = 0;             // bumped by stopSpeaking() so lines still QUEUED are dropped

function _ttsAudioCtx(){
  if(!_ttsCtx){
    _ttsCtx = new (window.AudioContext || window.webkitAudioContext)();
  }
  // Browsers start the context suspended until a user gesture; resume on each use so
  // playback isn't gated by an interaction that happened many turns ago.
  if(_ttsCtx.state === 'suspended'){ _ttsCtx.resume().catch(()=>{}); }
  return _ttsCtx;
}

// Map our UI language (EN/ZH/BM, any case) to the code the backend expects.
function _ttsLang(lang){
  const l = (lang || 'EN').toUpperCase();
  if(l === 'ZH') return 'ZH';
  if(l === 'BM') return 'BM';
  return 'EN';
}

/* Speak one line. `lang` is optional; when omitted we guess from the text (same heuristic
   the ears/intent use).

   QUEUED, not fire-and-forget. Auntie has one mouth: two lines spoken in quick succession
   (a payment completed, then the next payment's question) used to start together and talk
   over each other, because each call scheduled its own audio on the clock with nothing to
   serialise them. Each utterance now waits for the previous one's audio to actually FINISH
   before it even fetches, so lines play back to back instead of on top of each other. The
   promise resolves when this line has finished PLAYING, which is what the queue chains on. */
async function speak(text, lang){
  if(!_ttsEnabled || !text) return;
  const gen = _ttsGeneration;
  const prev = _ttsQueue;
  _ttsQueue = (async () => {
    await prev.catch(() => {});
    // A barge-in happened while this line was waiting its turn: it must never be spoken.
    if(gen !== _ttsGeneration) return;
    await _speakStream(text, lang);
  })();
  return _ttsQueue;
}

/* One utterance: fetch, stream, schedule. Only ever runs one at a time (see speak above). */
async function _speakStream(text, lang){
  if(typeof lang !== 'string' || !lang){
    lang = (typeof guessLang === 'function') ? guessLang(text) : 'EN';
  }
  const ctrl = new AbortController();
  _ttsAbort = ctrl;
  let res;
  try{
    res = await fetch(`${API_BASE}/tts`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: String(text), language: _ttsLang(lang) }),
      signal: ctrl.signal
    });
  }catch(err){
    _ttsAbort = null;
    if(err && err.name !== 'AbortError'){
      console.warn('[joy] tts fetch failed:', err.message);
    }
    return;
  }
  _ttsAbort = null;
  if(!res.ok || !res.body){
    // 503 = not configured on the server, no point retrying every line: disable the mouth.
    // 502 = the provider rejected THIS line (bad voice, text it cannot read). That is not
    //       permanent, so the mouth stays on and the next line is still attempted.
    if(res && res.status === 503) _ttsEnabled = false;
    else if(res && res.status === 502) console.warn('[joy] tts: synthesis failed for this line (502)');
    return;
  }

  // WHICH ENGINE ACTUALLY SPOKE THIS LINE, read straight off the response. More accurate
  // than polling /health, which would describe the state a moment later, and it costs
  // nothing. The backend reports the engine that produced the audio, the engine that was
  // selected, and whether those two differ.
  const spokeWith = res.headers.get('X-TTS-Provider');
  const selected = res.headers.get('X-TTS-Selected');
  const fellBack = res.headers.get('X-TTS-Fallback') === '1';
  if(spokeWith && (spokeWith !== _ttsProvider || fellBack !== _ttsFallbackActive)){
    _ttsProvider = spokeWith;
    _ttsFallbackActive = fellBack;
    // A fallback always starts from the SELECTED engine, so that is the one that failed.
    // Named from the server's own field rather than assumed, so a future provider chain
    // does not silently produce a wrong attribution.
    if(selected) _ttsSelected = selected;
    _ttsFallbackFrom = fellBack ? (selected || _ttsSelected) : '';
    _renderTtsProvider();
    // The reason text lives in /health (UTF-8 JSON), not in a header, so fetch it only when
    // a fallback actually happened.
    if(fellBack) refreshTtsProviderState();
  }

  const ctx = _ttsAudioCtx();
  const reader = res.body.getReader();
  const chain = { sources: new Set(), stopRequested: false };
  _ttsChain = chain;

  // The backend declares the PCM format on every response. Trust the header over the
  // compiled-in fallback: getting this wrong does not fail loudly, it plays every sentence
  // at the wrong pitch and the wrong speed, which is far harder to notice than an error.
  const sampleRate = parseInt(res.headers.get('X-Audio-Sample-Rate'), 10) || TTS_SAMPLE_RATE_FALLBACK;
  // Mono only: the backend requests Codec=pcm from Tencent, which is always 1 channel.
  // If a future provider ever sends stereo, this is the line that has to change.
  const channels = parseInt(res.headers.get('X-Audio-Channels'), 10) || 1;
  if(channels !== 1){
    console.warn('[joy] tts: unexpected channel count', channels, '- playback may be wrong');
  }

  // ---- scheduling state (all on the audio clock, in seconds) ----
  let pending = [];       // decoded AudioBuffers waiting to be handed to Web Audio
  let bufferedSec = 0;    // how much audio `pending` currently holds
  let started = false;    // has the audio clock been anchored yet?
  let nextStart = 0;      // time the NEXT buffer must start at
  let endAt = 0;          // time the last scheduled buffer finishes
  let carry = null;       // 0/1 leftover byte when a chunk ended in the middle of a sample

  // Hand every waiting buffer to Web Audio at an exact time on the AUDIO CLOCK.
  // Rule 2 in action: because each start time is computed up front, the next buffer
  // starts exactly where the previous one ends, playback is sample-accurate and there
  // is no seam for a JS callback to leave a gap in.
  const schedule = (flushAll) => {
    if(chain.stopRequested || !pending.length) return;
    if(!started){
      // Hold back until there is a small cushion, so one slow chunk can't starve the
      // speaker mid-word. flushAll means the sentence has finished arriving, so a short
      // utterance is never left waiting for a buffer that will not grow further.
      if(!flushAll && bufferedSec < TTS_START_BUFFER_SEC) return;
      started = true;
      nextStart = ctx.currentTime + TTS_SCHEDULE_LEAD_SEC;
    }
    for(const buf of pending){
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(ctx.destination);
      // If the schedule has fallen behind the clock (a stalled network), starting in the
      // past would be audible, so clamp forward instead.
      const when = Math.max(nextStart, ctx.currentTime);
      src.start(when);
      nextStart = when + buf.duration;
      endAt = nextStart;
      chain.sources.add(src);
      src.onended = () => { chain.sources.delete(src); };
    }
    pending = [];
    bufferedSec = 0;
  };

  try{
    while(true){
      if(chain.stopRequested) break;
      const { done, value } = await reader.read();
      if(done) break;
      if(!value || value.byteLength === 0) continue;

      // ---- Rule 1: reassemble whole samples before decoding ----
      // Chunk boundaries fall wherever the network felt like it, so stitch the byte we
      // carried from last time onto the front of this chunk, then decode only complete
      // 16-bit samples and carry any trailing odd byte forward.
      let bytes = value;
      if(carry !== null){
        bytes = new Uint8Array(value.byteLength + 1);
        bytes[0] = carry;
        bytes.set(value, 1);
        carry = null;
      }
      const wholeBytes = bytes.byteLength - (bytes.byteLength % 2);   // round down to even
      if(wholeBytes === 0){ carry = bytes[0]; continue; }              // a lone byte: carry it
      if(bytes.byteLength > wholeBytes) carry = bytes[wholeBytes];

      // Int16 PCM little-endian -> Float32 in [-1, 1], which is what Web Audio buffers want.
      const n = wholeBytes / 2;
      const view = new DataView(bytes.buffer, bytes.byteOffset, wholeBytes);
      const float32 = new Float32Array(n);
      for(let i=0; i<n; i++) float32[i] = view.getInt16(i * 2, true) / 32768;

      const audioBuf = ctx.createBuffer(1, n, sampleRate);
      audioBuf.copyToChannel(float32, 0);
      pending.push(audioBuf);
      bufferedSec += audioBuf.duration;
      schedule(false);
    }
  }catch(err){
    if(err && err.name !== 'AbortError'){
      console.warn('[joy] tts stream failed:', err.message);
    }
  }
  schedule(true);   // the sentence has finished arriving, flush whatever is left

  // Hold the line until its audio has actually finished PLAYING. This is what the queue in
  // speak() chains on: returning as soon as the last buffer was merely SCHEDULED would let the
  // next line start while this one is still audible, which is the overlap the queue exists to
  // stop. The chain stays reachable for that whole tail so barge-in can still silence a
  // sentence that arrived faster than it plays.
  if(chain.stopRequested || !started){
    if(_ttsChain === chain) _ttsChain = null;
    return;
  }
  const msLeft = Math.max(0, (endAt - ctx.currentTime) * 1000) + 80;
  await new Promise(r => setTimeout(r, msLeft));
  if(_ttsChain === chain) _ttsChain = null;
}

/* Barge-in: drop everything queued or playing. Called the moment the customer starts a new
   turn (mic press or typed send) so Auntie yields immediately. */
function stopSpeaking(){
  // Drop lines still QUEUED as well as the one playing. Barge-in means Auntie stops talking
  // entirely, so a sentence waiting its turn must never start up after the customer spoke.
  _ttsGeneration++;
  if(_ttsAbort){ try { _ttsAbort.abort(); } catch(e){} _ttsAbort = null; }
  const chain = _ttsChain;
  if(chain){
    chain.stopRequested = true;
    // Because playback is scheduled ahead on the audio clock, a sentence can have buffers
    // queued well into the future. Stopping only the one currently audible would leave the
    // rest to play, so every scheduled source is stopped here.
    for(const s of chain.sources){ try { s.stop(0); } catch(e){} }
    chain.sources.clear();
    if(_ttsChain === chain) _ttsChain = null;
  }
}

/* ==========================================================================
   VOICE ENGINE SWITCH — Cartesia (default) vs TRTC (Tencent)

   The top-bar "TRTC Mode" checkbox. OFF = Cartesia speaks; ON = Tencent speaks. The backend
   owns the decision (POST /tts/provider) and this file only reflects it, so the UI can never
   claim an engine the server is not using.

   WHAT THIS DOES NOT DO: it changes only which engine turns an already-decided line into
   sound. It cannot change what Joy says, who authorises a payment, or any stage of the
   control chain. A voice provider is not a security boundary, and nothing here should ever
   be described as one.

   THE FALLBACK IS SHOWN, NOT HIDDEN. If the selected engine cannot speak (out of credit, bad
   key, service down) the backend falls back to the other one and says why. The SPEAKING row
   turns amber and names the reason, because a voice that silently changes mid-demo is far
   more confusing than one that explains itself.
   ========================================================================== */

// Mirrors the backend's state. _ttsProvider is the engine that ACTUALLY spoke the last
// line; _ttsSelected is what the TRTC Mode toggle has asked for. They differ only during a
// fallback, and keeping them apart is what lets the row say "Tencent (fallback)" without
// the toggle appearing to have moved on its own.
let _ttsProvider = 'cartesia';
let _ttsSelected = 'cartesia';
let _ttsFallbackActive = false;
let _ttsFallbackFrom = '';

// The backend uses three canonical names; the customer only needs to know the vendor.
function _providerLabel(name){
  if(name === 'cartesia') return 'Cartesia';
  if(name === 'texttospeechse') return 'Tencent TRTC';
  if(name === 'texttovoice') return 'Tencent';
  return name || '-';
}

/* Paint the SPEAKING row from the state we hold.
   The row answers ONE question -- who is talking right now -- so it shows the engine that
   actually spoke, and marks it amber when that is not the engine we asked for. Naming the
   selected engine here instead would have the row confidently credit the voice to a
   provider that just failed. */
function _renderTtsProvider(){
  const row = document.getElementById('ttsProvRow');
  const val = document.getElementById('ttsProvVal');
  if(!val) return;
  const speaking = _providerLabel(_ttsProvider);
  val.textContent = _ttsFallbackActive ? `${speaking} (fallback)` : speaking;
  if(row){
    row.classList.toggle('warn', _ttsFallbackActive);
    // Name the engine that failed from the server's own record, never by elimination: with
    // three canonical engines "the other one" is not well defined.
    const failed = _providerLabel(_ttsFallbackFrom || _ttsSelected);
    row.title = _ttsFallbackActive
      ? `${failed} could not speak, so ${speaking} answered instead.`
      : `Speaking with ${speaking}.`;
  }
}

/* Ask the backend to switch mouths. Returns the response, or null on failure. */
async function setTtsProvider(name){
  try{
    const res = await fetch(`${API_BASE}/tts/provider`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider: name })
    });
    if(!res.ok){
      console.warn('[joy] tts provider switch rejected:', res.status);
      return null;
    }
    const d = await res.json();
    _ttsSelected = d.tts_provider || name;
    _ttsProvider = _ttsSelected;
    _ttsFallbackActive = !!d.fallback_active;
    _ttsFallbackFrom = '';
    // The selected engine has no usable configuration: say so now rather than waiting for
    // the next spoken line to fail.
    if(d.configured === false){
      console.warn('[joy] tts provider', _ttsProvider, 'is not configured on the server');
    }
    _renderTtsProvider();
    return d;
  }catch(err){
    console.warn('[joy] tts provider switch failed:', err && err.message);
    return null;
  }
}

(function initTtsToggle(){
  const box = document.getElementById('ttsToggle');
  if(!box) return;
  box.onchange = async () => {
    const want = box.checked ? 'tencent' : 'cartesia';
    // Cut any audio mid-sentence before switching, so the change is audible immediately
    // instead of after the current line finishes in the old voice.
    stopSpeaking();
    box.disabled = true;
    try{
      const d = await setTtsProvider(want);
      // Revert the switch if the server refused it. A toggle that stays flipped while the
      // backend ignored it is the one outcome worth actively preventing.
      if(!d) box.checked = !box.checked;
    }finally{
      box.disabled = false;
    }
  };
})();

/* On load, adopt the SERVER's state rather than assuming the default. The provider is
   runtime-switchable and survives across page loads within a server session, so a reload
   must not show "Cartesia" while the backend is actually speaking with Tencent. */
(async function syncTtsProviderFromServer(){
  try{
    const res = await fetch(`${API_BASE}/health`);
    if(!res.ok) return;
    const d = await res.json();
    if(d && d.tts_provider){
      _ttsSelected = d.tts_provider;
      _ttsProvider = d.tts_served_provider || d.tts_provider;
      _ttsFallbackActive = !!d.tts_fallback_active;
      _ttsFallbackFrom = d.tts_fallback_from || '';
      const box = document.getElementById('ttsToggle');
      // The checkbox means "use Tencent", so it is checked exactly when a Tencent mouth is
      // active — whichever of the two Tencent engines that turns out to be.
      if(box) box.checked = !!d.tts_provider_is_tencent;
      _renderTtsProvider();
    }
  }catch(err){
    // A missing /health must never break the mouth; the default state already renders.
  }
})();

/* Re-read the provider state after a line has been spoken, so a fallback that happened
   during playback becomes visible without the operator having to reload. Cheap (one small
   GET) and only runs when a fallback is plausible. */
async function refreshTtsProviderState(){
  try{
    const res = await fetch(`${API_BASE}/health`);
    if(!res.ok) return;
    const d = await res.json();
    if(!d) return;
    const served = d.tts_served_provider || d.tts_provider;
    const changed = (served && served !== _ttsProvider)
      || (!!d.tts_fallback_active !== _ttsFallbackActive);
    if(changed){
      _ttsSelected = d.tts_provider || _ttsSelected;
      _ttsProvider = served || _ttsProvider;
      _ttsFallbackActive = !!d.tts_fallback_active;
      _ttsFallbackFrom = d.tts_fallback_from || '';
      _renderTtsProvider();
    }
  }catch(err){ /* never let a status refresh break playback */ }
}
