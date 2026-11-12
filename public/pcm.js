class PcmCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Int16Array(960);
    this.offset = 0;
    this.energy = 0;
  }
  process(inputs) {
    const samples = inputs[0]?.[0];
    if (!samples) return true;
    for (const value of samples) {
      this.energy += value * value;
      this.buffer[this.offset++] = Math.round(
        Math.max(-1, Math.min(1, value)) * 32767,
      );
      if (this.offset === this.buffer.length) {
        const pcm = this.buffer.buffer;
        this.port.postMessage(
          { pcm, rms: Math.sqrt(this.energy / this.offset) },
          [pcm],
        );
        this.buffer = new Int16Array(960);
        this.offset = 0;
        this.energy = 0;
      }
    }
    return true;
  }
}
registerProcessor('pcm-capture', PcmCapture);
