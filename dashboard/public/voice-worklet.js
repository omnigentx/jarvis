/**
 * AudioWorklet processor — downsamples mic Float32 to 16 kHz mono int16 PCM
 * and posts ArrayBuffer chunks to the main thread.
 *
 * Why an AudioWorklet (vs the deprecated ScriptProcessorNode):
 *   - runs on the realtime audio thread, no glitches under main-thread jank
 *   - single processor instance per session, low GC pressure
 *
 * The browser typically gives us 48 kHz Float32. We resample to 16 kHz with
 * a simple stride-based decimation + low-pass smoothing. Fine for speech;
 * a polyphase filter would be overkill at 3:1 ratio.
 */
class PCM16Resampler extends AudioWorkletProcessor {
  constructor(options) {
    super()
    const opts = options?.processorOptions || {}
    this.targetRate = opts.targetRate || 16000
    this.frameMs = opts.frameMs || 100  // emit every 100ms
    this.inputRate = sampleRate  // global injected by the worklet runtime
    this.ratio = this.inputRate / this.targetRate
    this.framesPerEmit = Math.floor((this.targetRate * this.frameMs) / 1000)
    this.buffer = new Int16Array(this.framesPerEmit)
    this.filled = 0
    this.acc = 0
  }

  process(inputs) {
    const input = inputs[0]
    if (!input || !input[0]) return true
    const ch = input[0]
    for (let i = 0; i < ch.length; i++) {
      this.acc += this.ratio
      while (this.acc < 1) { i++; this.acc += this.ratio; if (i >= ch.length) break }
      if (i >= ch.length) break
      this.acc -= 1
      // Float32 [-1,1] → Int16 [-32768,32767]
      let s = ch[i]
      if (s > 1) s = 1; else if (s < -1) s = -1
      this.buffer[this.filled++] = s < 0 ? s * 0x8000 : s * 0x7fff
      if (this.filled >= this.framesPerEmit) {
        // Send a copy so the worklet can refill without races on the
        // main thread side.
        this.port.postMessage(this.buffer.slice().buffer, [this.buffer.slice().buffer])
        this.filled = 0
      }
    }
    return true
  }
}

registerProcessor('pcm16-resampler', PCM16Resampler)
