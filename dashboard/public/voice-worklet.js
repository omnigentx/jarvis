/**
 * AudioWorklet processor — downsamples mic Float32 to 16 kHz mono int16 PCM
 * and posts ArrayBuffer chunks to the main thread.
 *
 * Why an AudioWorklet (vs the deprecated ScriptProcessorNode):
 *   - runs on the realtime audio thread, no glitches under main-thread jank
 *   - single processor instance per session, low GC pressure
 *
 * Browsers typically give us Float32 at 48 kHz (sampleRate global is
 * injected by the worklet runtime). We decimate down to 16 kHz with a
 * simple phase accumulator: every `step = sampleRate / targetRate` input
 * samples we emit one output sample. Works for non-integer ratios too
 * (44.1 kHz → 16 kHz).
 *
 * Aliasing without a low-pass filter is acceptable for speech in this
 * range; the downstream Whisper VAD + STT both expect 16 kHz mono int16
 * and produce no useful output if the rate is wrong (which is exactly
 * what the previous "ratio inversion" bug caused — audio reached the
 * server 3x faster than VAD expected, so nothing transcribed).
 */
class PCM16Resampler extends AudioWorkletProcessor {
  constructor(options) {
    super()
    const opts = options?.processorOptions || {}
    this.targetRate = opts.targetRate || 16000
    this.frameMs = opts.frameMs || 100
    // step = how many input samples per output sample. e.g. 48000/16000 = 3.
    this.step = sampleRate / this.targetRate
    this.framesPerEmit = Math.floor((this.targetRate * this.frameMs) / 1000)
    this.buffer = new Int16Array(this.framesPerEmit)
    this.filled = 0
    // Phase accumulator: counts input samples seen toward the next output.
    // When it reaches `step`, we emit one and subtract `step` (handles
    // fractional steps correctly).
    this.phase = 0
  }

  process(inputs) {
    const ch = inputs[0]?.[0]
    if (!ch) return true
    for (let i = 0; i < ch.length; i++) {
      this.phase += 1
      if (this.phase >= this.step) {
        this.phase -= this.step
        let s = ch[i]
        if (s > 1) s = 1
        else if (s < -1) s = -1
        // Float32 [-1,1] → Int16 [-32768, 32767]
        this.buffer[this.filled++] = s < 0 ? s * 0x8000 : s * 0x7fff
        if (this.filled >= this.framesPerEmit) {
          // Post a single copy so the worklet can refill without racing
          // the main thread. slice() detaches a fresh ArrayBuffer; we
          // include it in the transferList for zero-copy ownership move.
          const out = this.buffer.slice().buffer
          this.port.postMessage(out, [out])
          this.filled = 0
        }
      }
    }
    return true
  }
}

registerProcessor('pcm16-resampler', PCM16Resampler)
