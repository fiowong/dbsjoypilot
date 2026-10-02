/* 5.5/6 stt.js, the ears. Loads AFTER scenarios.js (which defines API_BASE) and BEFORE
   live.js (which drives it).

   Captures Auntie's microphone as 16 kHz mono 16-bit PCM, streams it to the backend over a
   WebSocket, and hands back whatever the ASR engine returns: an interim guess, then a final
   reading with a confidence.

   Two things this file deliberately does NOT do:
     - It composes no customer-facing wording. It returns text; the orchestrator decides
       what Joy says.
     - It never invents a confidence. If the engine reported none, the confidence comes back
       null and the reason is in `confidence_source`, so the caller can say so honestly.
*/

const PCM_WORKLET_SRC = `
class PcmCapture extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) this.port.postMessage(ch);
    return true;
  }
}
registerProcessor('pcm-capture', PcmCapture);
`;

function floatToPcm16(f32) {
  const out = new Int16Array(f32.length);
  for (let i = 0; i < f32.length; i++) {
    const s = Math.max(-1, Math.min(1, f32[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out.buffer;
}

function sttWsUrl() {
  // API_BASE (scenarios.js) is the single source of truth for the backend address; the
  // literal below is only a last-resort fallback if that file somehow didn't load.
  const base = (typeof API_BASE !== 'undefined' && API_BASE) || 'http://127.0.0.1:8078';
  return base.replace(/^http/, 'ws') + '/ws/stt';
}

/* One object = one listening session. Create, start(), then stop() or cancel(). */
class SttClient {
  constructor(opts) {
    opts = opts || {};
    this.onInterim = opts.onInterim || function () {};
    this.onFinal = opts.onFinal || function () {};
    this.onError = opts.onError || function () {};
    this.onState = opts.onState || function () {};

    this.ws = null;
    this.ctx = null;
    this.stream = null;
    this.node = null;
    this.running = false;
    this._resolveFinal = null;
  }

  static supported() {
    return !!(window.WebSocket && navigator.mediaDevices && navigator.mediaDevices.getUserMedia
              && (window.AudioContext || window.webkitAudioContext));
  }

  /* Open the ASR session and begin streaming the mic. Resolves once the engine is ready. */
  async start(language) {
    if (this.running) return;
    this.onState('connecting');
    await this._openSocket();
    await this._startMic();

    this.ws.send(JSON.stringify({ type: 'start', language: language || 'en' }));
    await this._waitFor('started');
    this.running = true;
    this.onState('listening');
  }

  /* Stop listening and let the engine finish the utterance. Resolves with the final result. */
  async stop() {
    if (!this.running) return null;
    this.running = false;
    this.onState('finishing');
    this._stopMic();
    if (!this.ws) return null;          // never connected, nothing to wait for
    // Send the trailing partial chunk (<200 ms) so the engine hears the full utterance
    // before we tell it the stream is over.
    if (typeof this._flushAudio === 'function') this._flushAudio();
    try { this.ws.send(JSON.stringify({ type: 'end' })); } catch (e) { /* socket gone */ }
    const result = await this._waitFor('final');
    this._closeSocket();
    this.onState('idle');
    return result;
  }

  /* Abandon the utterance, no transcript wanted. */
  cancel() {
    this.running = false;
    this._stopMic();
    this._closeSocket();
    this.onState('idle');
  }

  /* ---------- internals ---------- */
  _openSocket() {
    return new Promise((resolve, reject) => {
      let ws;
      try { ws = new WebSocket(sttWsUrl()); }
      catch (e) { this.onError('could not open the speech socket'); reject(e); return; }
      ws.binaryType = 'arraybuffer';
      this.ws = ws;

      ws.onopen = () => resolve();
      ws.onerror = () => {
        this.onError('speech socket error');
        // Unblock anyone awaiting a handshake, or start() would hang forever.
        this._fire('started', { error: true });
        reject(new Error('ws error'));
      };
      ws.onclose = () => {
        if (this.running) this.onError('speech socket closed early');
        this._fire('started', { error: true });
        this._fire('final', null);
      };
      ws.onmessage = ev => this._onMessage(ev.data);
    });
  }

  _onMessage(raw) {
    let m;
    try { m = JSON.parse(raw); } catch (e) { return; }

    if (m.type === 'ready') {
      if (!m.live) {
        this.onError('ASR is not configured on the server');
        this._fire('started', { error: true });   // otherwise start() waits forever
        this._closeSocket();
      }
    } else if (m.type === 'started') {
      this._fire('started', m);
    } else if (m.type === 'interim') {
      if (m.text) this.onInterim(m.text);
    } else if (m.type === 'final') {
      // confidence is null when the engine reported nothing, pass that through untouched.
      this._fire('final', m);
      this.onFinal({ text: m.text || '', confidence: m.confidence,
                     confidenceSource: m.confidence_source, engine: m.engine });
    } else if (m.type === 'error') {
      this.onError(m.message || 'speech recognition failed');
      this._fire('started', { error: true });
      this._fire('final', null);
    }
  }

  /* Resolve a pending wait for `kind`. */
  _fire(kind, payload) {
    if (this._waiting && this._waiting.kind === kind) {
      const w = this._waiting; this._waiting = null;
      w.resolve(payload);
    }
  }

  _waitFor(kind) {
    return new Promise(resolve => { this._waiting = { kind: kind, resolve: resolve }; });
  }

  async _startMic() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error('no microphone API');
    }
    this.stream = await navigator.mediaDevices.getUserMedia({
      // Echo cancellation + noise suppression are tuned for VoIP (so the other person
      // doesn't hear themselves), not for ASR. They clip soft consonants and fricatives
      // and noticeably hurt recognition accuracy, especially on quieter / elderly
      // voices. Disable both; we still want mono, and the AudioContext below forces 16 kHz.
      audio: { echoCancellation: false, noiseSuppression: false, channelCount: 1 }
    });
    const AC = window.AudioContext || window.webkitAudioContext;
    this.ctx = new AC({ sampleRate: 16000 });   // Tencent wants 16 kHz
    const src = this.ctx.createMediaStreamSource(this.stream);

    await this._attachNode(src);
  }

  async _attachNode(src) {
    // Batch audio into ~200 ms chunks before forwarding. The worklet hands us 8 ms
    // quanta (~125 messages/sec) which fragments the signal and noticeably hurts
    // Tencent's recognition accuracy on quiet / elderly voices. The provider's own
    // docs recommend ~200 ms frames sent 1:1 realtime.
    const TARGET_SAMPLES = 3200;                 // 200 ms × 16 kHz
    let pendingChunks = [];
    let pendingSamples = 0;
    const flush = () => {
      if (!pendingSamples) return;
      const merged = new Float32Array(pendingSamples);
      let off = 0;
      for (const a of pendingChunks) { merged.set(a, off); off += a.length; }
      pendingChunks = []; pendingSamples = 0;
      if (!this.running) return;
      if (this.ws && this.ws.readyState === 1) this.ws.send(floatToPcm16(merged));
    };
    // Exposed so stop() can flush the trailing partial chunk (<200 ms) so the engine
    // hears the full utterance before we tell it "end".
    this._flushAudio = flush;

    try {
      const url = URL.createObjectURL(new Blob([PCM_WORKLET_SRC],
                                               { type: 'application/javascript' }));
      await this.ctx.audioWorklet.addModule(url);
      URL.revokeObjectURL(url);
      this.node = new AudioWorkletNode(this.ctx, 'pcm-capture');
      this.node.port.onmessage = e => {
        const f = e.data;
        pendingChunks.push(f);
        pendingSamples += f.length;
        if (pendingSamples >= TARGET_SAMPLES) flush();
      };
      // AudioWorklet only runs when it is connected to a destination.
      const sink = this.ctx.createGain(); sink.gain.value = 0;
      src.connect(this.node); this.node.connect(sink); sink.connect(this.ctx.destination);
      return;
    } catch (e) {
      /* ScriptProcessor is deprecated but universally available, a fine fallback. */
      const node = this.ctx.createScriptProcessor(2048, 1, 1);
      node.onaudioprocess = e => {
        const f = e.inputBuffer.getChannelData(0);
        pendingChunks.push(f);
        pendingSamples += f.length;
        if (pendingSamples >= TARGET_SAMPLES) flush();
      };
      const sink = this.ctx.createGain(); sink.gain.value = 0;
      src.connect(node); node.connect(sink); sink.connect(this.ctx.destination);
      this.node = node;
    }
  }

  _stopMic() {
    if (this.node) { try { this.node.disconnect(); } catch (e) {} this.node = null; }
    if (this.stream) { this.stream.getTracks().forEach(t => t.stop()); this.stream = null; }
    if (this.ctx) { try { this.ctx.close(); } catch (e) {} this.ctx = null; }
  }

  _closeSocket() {
    if (this.ws) {
      try { this.ws.close(); } catch (e) { /* already closed */ }
      this.ws = null;
    }
  }
}
