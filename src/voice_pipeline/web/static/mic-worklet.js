// 마이크 입력을 16 kHz Int16 모노 20 ms(320샘플) 프레임으로 바꿔 메인 스레드로 보낸다.
class MicProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000; // 입력 샘플 수 / 출력 샘플
    this.acc = 0; this.n = 0; this.pos = 0;
    this.out = new Int16Array(320); this.k = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      this.acc += ch[i]; this.n++; this.pos += 1;
      if (this.pos >= this.ratio) { // 구간 평균 = 간단한 저역 통과 + 데시메이션
        this.pos -= this.ratio;
        const v = Math.max(-1, Math.min(1, this.acc / this.n));
        this.acc = 0; this.n = 0;
        this.out[this.k++] = v < 0 ? v * 32768 : v * 32767;
        if (this.k === 320) {
          const buf = this.out.buffer;
          this.port.postMessage(buf, [buf]);
          this.out = new Int16Array(320); this.k = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor("mic-processor", MicProcessor);
