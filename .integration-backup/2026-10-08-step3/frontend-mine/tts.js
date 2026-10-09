/* 5.5/6 tts.js, the mouth. Loads AFTER live.js (needs API_BASE + guessLang).
 *
 * Speaks the backend's INTERFACE_LINE text by asking our own /tts endpoint, which proxies
 * Cartesia and returns raw mono 16-bit PCM at 24 kHz as it's synthesised. Audio plays
 * through a chunked WebAudio queue so the first syllable reaches the customer within a
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
// Cartesia sonic-latest always emits mono 16-bit signed little-endian PCM at 24 kHz,
// matching the format we request in tts.py. The browser needs to know this to build
// AudioBuffers without first asking the server, keeping it as a constant makes it
// impossible for the two sides to drift apart unnoticed.
const TTS_SAMPLE_RATE = 24000;

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
    // 503 = not configured on the server, no point retrying every line.
    if(res && res.status === 503) _ttsEnabled = false;
    return;
  }

  const ctx = _ttsAudioCtx();
  const reader = res.body.getReader();
  const chain = { sources: new Set(), stopRequested: false };
  _ttsChain = chain;

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

      const audioBuf = ctx.createBuffer(1, n, TTS_SAMPLE_RATE);
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
