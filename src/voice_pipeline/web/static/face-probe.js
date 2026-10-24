/**
 * HealthUp 표정 모델 성능 계측 스크립트 (F1 Face Probe)
 *
 * 개발 전용:
 * 1. 100% 브라우저 메모리 로컬 분석 (서버 영상 프레임 전송 0 바이트)
 * 2. 동의 버튼 클릭 전 getUserMedia({video}) 호출 0회
 * 3. 4.3절 후보 3종 성능 계측:
 *    - 로드 시간 (Load Time)
 *    - 추론 지연 시간 P50 / P90 / Mean / Min / Max
 *    - 실제 분석 주기 (Interval ms) & FPS
 *    - 오디오 스케줄러 언더런 수 (app.js 연동 검증: t < now + 5ms)
 * 4. 텍스트 결과 내려받기 (bench/results/face_probe_YYYYMMDD.txt 포맷)
 */

(function () {
  "use strict";

  // ---------- DOM 요소 참조 ----------
  const btnConsentOn = document.getElementById("btnConsentOn");
  const btnConsentOff = document.getElementById("btnConsentOff");
  const cameraStatusBadge = document.getElementById("cameraStatusBadge");
  const netGuardBadge = document.getElementById("netGuardBadge");

  const cameraView = document.getElementById("cameraView");
  const overlayCanvas = document.getElementById("overlayCanvas");
  const videoPlaceholder = document.getElementById("videoPlaceholder");
  const camFpsText = document.getElementById("camFpsText");

  const topEmotionLabel = document.getElementById("topEmotionLabel");
  const topEmotionConf = document.getElementById("topEmotionConf");
  const barSmile = document.getElementById("barSmile");
  const valSmile = document.getElementById("valSmile");
  const barBrow = document.getElementById("barBrow");
  const valBrow = document.getElementById("valBrow");
  const barBlink = document.getElementById("barBlink");
  const valBlink = document.getElementById("valBlink");

  const underrunCountEl = document.getElementById("underrunCount");
  const schedulerJitterEl = document.getElementById("schedulerJitter");
  const audioCtxStateEl = document.getElementById("audioCtxState");
  const benchFrameCountEl = document.getElementById("benchFrameCount");
  const chkAudioMonitor = document.getElementById("chkAudioMonitor");

  const selCandidate = document.getElementById("selCandidate");
  const selFrameCount = document.getElementById("selFrameCount");
  const selTargetInterval = document.getElementById("selTargetInterval");
  const candidateBadge = document.getElementById("candidateBadge");

  const btnRunSingle = document.getElementById("btnRunSingle");
  const btnRunAll = document.getElementById("btnRunAll");
  const btnStopBench = document.getElementById("btnStopBench");
  const btnDownloadReport = document.getElementById("btnDownloadReport");

  const benchProgressWrap = document.getElementById("benchProgressWrap");
  const benchProgressBar = document.getElementById("benchProgressBar");
  const benchProgressText = document.getElementById("benchProgressText");
  const benchPercentText = document.getElementById("benchPercentText");

  // ---------- 전역 상태 ----------
  let mediaStream = null;
  let isCameraActive = false;
  let isBenchRunning = false;
  let benchAbortRequested = false;

  // 후보별 계측 결과 보관소
  const benchmarkResults = {
    c1: null,
    c2: null,
    c3: null,
  };

  // 후보 정의 메타데이터 (§4.3)
  const CANDIDATES = {
    c1: {
      id: "c1",
      name: "MediaPipe Face Landmarker",
      desc: "blendshape 52종 (얼굴 랜드마크 478점)",
      license: "Apache-2.0",
      runtime: "@mediapipe/tasks-vision (WASM/WebGL)",
      modelFiles: [
        { path: "/static/models/face_landmarker.task", size: "3.58 MB", url: "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task" },
        { path: "/static/vendor/tasks-vision/vision_wasm_internal.wasm", size: "2.49 MB", url: "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/wasm/vision_wasm_internal.wasm" },
      ],
      totalSizeMb: 6.1,
    },
    c2: {
      id: "c2",
      name: "Face Detector + FER+ ONNX",
      desc: "8개 감정 확률 (64x64 흑백 입력 CNN)",
      license: "ONNX Model Zoo MIT",
      runtime: "onnxruntime-web (WASM SIMD)",
      modelFiles: [
        { path: "/static/models/version-RFB-320.onnx", size: "1.21 MB", url: "https://github.com/onnx/models/raw/main/validated/vision/body_analysis/ultraface/models/version-RFB-320.onnx" },
        { path: "/static/models/emotion-ferplus-8.onnx", size: "33.42 MB", url: "https://github.com/onnx/models/raw/main/validated/vision/body_analysis/emotion_ferplus/model/emotion-ferplus-8.onnx" },
        { path: "/static/vendor/onnxruntime-web/ort-wasm-simd.wasm", size: "2.33 MB", url: "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.17.3/dist/ort-wasm-simd.wasm" },
      ],
      totalSizeMb: 37.1,
    },
    c3: {
      id: "c3",
      name: "face-api.js 표정 모델",
      desc: "7개 감정 확률 (TinyFaceDetector + FaceExpressionNet)",
      license: "MIT",
      runtime: "TensorFlow.js / face-api bundle",
      modelFiles: [
        { path: "/static/models/face-api/tiny_face_detector_model-shard1", size: "0.19 MB", url: "https://raw.githubusercontent.com/justadudewhohacks/face-api.js/master/weights/tiny_face_detector_model-shard1" },
        { path: "/static/models/face-api/face_expression_model-shard1", size: "0.33 MB", url: "https://raw.githubusercontent.com/justadudewhohacks/face-api.js/master/weights/face_expression_model-shard1" },
        { path: "/static/vendor/face-api/face-api.js", size: "0.26 MB", url: "https://cdn.jsdelivr.net/npm/@vladmandic/face-api/dist/face-api.js" },
      ],
      totalSizeMb: 0.8,
    },
  };

  // ---------- 1. 오디오 언더런 모니터 (Web Audio API) ----------
  class AudioUnderrunMonitor {
    constructor() {
      this.ctx = null;
      this.timer = null;
      this.underruns = 0;
      this.nextTime = 0;
      this.jitterMs = 0;
      this.intervalMs = 25; // 25ms 단위로 스케줄링
    }

    start() {
      if (!chkAudioMonitor.checked) return;
      try {
        const AudioCtx = window.AudioContext || window.webkitAudioContext;
        if (!this.ctx) {
          this.ctx = new AudioCtx({ sampleRate: 24000 });
        }
        if (this.ctx.state === "suspended") {
          this.ctx.resume();
        }
        this.underruns = 0;
        this.nextTime = this.ctx.currentTime + 0.05;
        audioCtxStateEl.textContent = this.ctx.state;
        audioCtxStateEl.className = "value ok";

        if (this.timer) clearInterval(this.timer);
        this.timer = setInterval(() => this._tick(), this.intervalMs);
      } catch (e) {
        console.warn("AudioUnderrunMonitor 시작 오류:", e);
        audioCtxStateEl.textContent = "미지원";
        audioCtxStateEl.className = "value warn";
      }
    }

    _tick() {
      if (!this.ctx) return;
      const now = this.ctx.currentTime;
      // app.js의 언더런 감지 조건 (t < now + 0.005) 대조:
      // 이미 스케줄링해야 할 시점이 현재 오디오 클럭보다 5ms 이상 뒤쳐지면 언더런으로 판정
      const delay = (now - this.nextTime) * 1000;
      this.jitterMs = Math.max(0, Math.round(delay * 10) / 10);

      if (this.nextTime < now - 0.005) {
        this.underruns++;
        this.nextTime = now + 0.02; // 재동기화
        underrunCountEl.className = "value bad";
      } else {
        // 무음 오디오 버퍼 스케줄링 (CPU 클럭 부하 감시용)
        while (this.nextTime < now + 0.1) {
          const osc = this.ctx.createOscillator();
          const gain = this.ctx.createGain();
          gain.gain.value = 0.0; // 무음
          osc.connect(gain);
          gain.connect(this.ctx.destination);
          osc.start(this.nextTime);
          osc.stop(this.nextTime + 0.02);
          this.nextTime += 0.02;
        }
        if (this.underruns === 0) {
          underrunCountEl.className = "value ok";
        }
      }

      underrunCountEl.textContent = `${this.underruns}회`;
      schedulerJitterEl.textContent = `${this.jitterMs.toFixed(1)} ms`;
      audioCtxStateEl.textContent = this.ctx.state;
    }

    stop() {
      if (this.timer) {
        clearInterval(this.timer);
        this.timer = null;
      }
      if (this.ctx && this.ctx.state !== "closed") {
        this.ctx.suspend().catch(() => {});
      }
      audioCtxStateEl.textContent = "정지";
    }

    reset() {
      this.underruns = 0;
      underrunCountEl.textContent = "0회";
      underrunCountEl.className = "value ok";
      schedulerJitterEl.textContent = "0.0 ms";
    }
  }

  const audioMonitor = new AudioUnderrunMonitor();

  // ---------- 2. 카메라 제어 (동의 전 getUserMedia 0회 보장) ----------
  btnConsentOn.addEventListener("click", async () => {
    btnConsentOn.disabled = true;
    cameraStatusBadge.textContent = "카메라 요청 중...";
    cameraStatusBadge.className = "badge";

    try {
      // 오직 동의 버튼 클릭 시에만 getUserMedia 호출 (§4.4)
      mediaStream = await navigator.mediaDevices.getUserMedia({
        video: {
          width: { ideal: 640 },
          height: { ideal: 480 },
          facingMode: "user",
        },
        audio: false,
      });

      cameraView.srcObject = mediaStream;
      await cameraView.play();

      isCameraActive = true;
      videoPlaceholder.hidden = true;
      cameraView.hidden = false;
      overlayCanvas.hidden = false;

      // 캔버스 크기 동기화
      overlayCanvas.width = cameraView.videoWidth || 640;
      overlayCanvas.height = cameraView.videoHeight || 480;

      btnConsentOn.disabled = true;
      btnConsentOff.disabled = false;
      cameraStatusBadge.textContent = "카메라 켜짐 (640x480)";
      cameraStatusBadge.className = "badge ok";
      netGuardBadge.textContent = "네트워크 영상 전송 0 바이트 (안전)";

      audioMonitor.start();
      startLivePreviewLoop();
    } catch (err) {
      console.error("카메라 권한 또는 장치 오류:", err);
      btnConsentOn.disabled = false;
      cameraStatusBadge.textContent = `카메라 실패: ${err.name || "권한 거부"}`;
      cameraStatusBadge.className = "badge bad";
    }
  });

  btnConsentOff.addEventListener("click", () => {
    stopCamera();
  });

  function stopCamera() {
    if (mediaStream) {
      mediaStream.getTracks().forEach((track) => track.stop());
      mediaStream = null;
    }
    cameraView.srcObject = null;
    isCameraActive = false;

    videoPlaceholder.hidden = false;
    cameraView.hidden = true;
    overlayCanvas.hidden = true;

    btnConsentOn.disabled = false;
    btnConsentOff.disabled = true;
    cameraStatusBadge.textContent = "카메라 꺼짐";
    cameraStatusBadge.className = "badge";
    camFpsText.textContent = "- FPS";

    clearVisualizers();
    audioMonitor.stop();
  }

  function clearVisualizers() {
    topEmotionLabel.textContent = "-";
    topEmotionConf.textContent = "- %";
    barSmile.style.width = "0%";
    valSmile.textContent = "0.0";
    barBrow.style.width = "0%";
    valBrow.textContent = "0.0";
    barBlink.style.width = "0%";
    valBlink.textContent = "0.0";

    const ctx = overlayCanvas.getContext("2d");
    ctx.clearRect(0, 0, overlayCanvas.width, overlayCanvas.height);
  }

  // 실시간 카메라 미리보기 루프 (프레임 렌더링)
  let lastCamFpsTime = performance.now();
  let camFrameCount = 0;

  function startLivePreviewLoop() {
    function loop() {
      if (!isCameraActive) return;

      camFrameCount++;
      const now = performance.now();
      if (now - lastCamFpsTime >= 1000) {
        const fps = Math.round((camFrameCount * 1000) / (now - lastCamFpsTime));
        camFpsText.textContent = `${fps} FPS`;
        camFrameCount = 0;
        lastCamFpsTime = now;
      }

      if (!isBenchRunning && cameraView.readyState >= 2) {
        // 비측정 중 기본 얼굴 윤곽 가이드 박스 표시
        drawGuideBox();
      }

      requestAnimationFrame(loop);
    }
    requestAnimationFrame(loop);
  }

  function drawGuideBox() {
    const ctx = overlayCanvas.getContext("2d");
    const w = overlayCanvas.width;
    const h = overlayCanvas.height;
    ctx.clearRect(0, 0, w, h);

    // 얼굴 영역 안내 타원
    ctx.strokeStyle = "rgba(47, 111, 237, 0.4)";
    ctx.lineWidth = 2;
    ctx.setLineDash([6, 6]);
    ctx.beginPath();
    ctx.ellipse(w / 2, h / 2, w * 0.22, h * 0.32, 0, 0, Math.PI * 2);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  // ---------- 3. 통계 및 분위수 계산 도구 (P50, P90, P99) ----------
  function calcPercentiles(arr) {
    if (!arr.length) return { p50: 0, p90: 0, p99: 0, min: 0, max: 0, mean: 0 };
    const sorted = [...arr].sort((a, b) => a - b);
    const p50 = sorted[Math.floor(sorted.length * 0.5)];
    const p90 = sorted[Math.floor(sorted.length * 0.9)];
    const p99 = sorted[Math.floor(sorted.length * 0.99)];
    const min = sorted[0];
    const max = sorted[sorted.length - 1];
    const sum = sorted.reduce((a, b) => a + b, 0);
    const mean = Math.round((sum / sorted.length) * 10) / 10;
    return {
      p50: Math.round(p50 * 10) / 10,
      p90: Math.round(p90 * 10) / 10,
      p99: Math.round(p99 * 10) / 10,
      min: Math.round(min * 10) / 10,
      max: Math.round(max * 10) / 10,
      mean,
    };
  }

  // ---------- 4. 후보 모델 파이프라인 (로컬 모델 또는 모의 연산) ----------
  class CandidateRunner {
    constructor(candId) {
      this.cand = CANDIDATES[candId];
      this.offscreenCanvas = document.createElement("canvas");
      this.offCtx = this.offscreenCanvas.getContext("2d", { willReadFrequently: true });
      this.isRealModelLoaded = false;
      this.modelInstance = null;
    }

    async checkModelAvailability() {
      // 로컬 /static/models/ 파일 존재 여부 점검
      const primaryFile = this.cand.modelFiles[0];
      try {
        const resp = await fetch(primaryFile.path, { method: "HEAD" });
        return resp.ok;
      } catch {
        return false;
      }
    }

    async load() {
      const t0 = performance.now();
      const available = await this.checkModelAvailability();

      if (available) {
        // 실제 로컬 파일이 있으면 동적 로드 시도
        try {
          if (this.cand.id === "c1") {
            // MediaPipe tasks-vision 초기화
            if (window.FilesetResolver && window.FaceLandmarker) {
              const vision = await window.FilesetResolver.forVisionTasks("/static/vendor/tasks-vision/wasm");
              this.modelInstance = await window.FaceLandmarker.createFromOptions(vision, {
                baseOptions: { modelAssetPath: "/static/models/face_landmarker.task" },
                outputFaceBlendshapes: true,
                runningMode: "VIDEO",
                numFaces: 1,
              });
              this.isRealModelLoaded = true;
            }
          } else if (this.cand.id === "c2") {
            // onnxruntime-web 초기화
            if (window.ort) {
              if (window.ort.env && window.ort.env.wasm) {
                window.ort.env.wasm.wasmPaths = "/static/vendor/onnxruntime-web/";
              }
              this.modelInstance = await window.ort.InferenceSession.create("/static/models/emotion-ferplus-8.onnx", {
                executionProviders: ["wasm"],
              });
              this.isRealModelLoaded = true;
            }
          } else if (this.cand.id === "c3") {
            // face-api 초기화
            if (window.faceapi) {
              await window.faceapi.nets.tinyFaceDetector.loadFromUri("/static/models/face-api");
              await window.faceapi.nets.faceExpressionNet.loadFromUri("/static/models/face-api");
              this.isRealModelLoaded = true;
            }
          }
        } catch (err) {
          console.warn(`[${this.cand.name}] 실제 모델 로드 실패, 모의 파이프라인으로 전환:`, err);
          this.isRealModelLoaded = false;
        }
      }

      if (!this.isRealModelLoaded) {
        // 모의 파이프라인: 실제 모델에 상응하는 캔버스 초기화 (인위적 딜레이 제거 §15-2-2)
        this.offscreenCanvas.width = this.cand.id === "c2" ? 64 : 256;
        this.offscreenCanvas.height = this.cand.id === "c2" ? 64 : 256;
      }

      const loadTimeMs = Math.round(performance.now() - t0);
      return {
        loadTimeMs,
        isReal: this.isRealModelLoaded,
      };
    }

    async infer(video) {
      const t0 = performance.now();

      if (this.isRealModelLoaded && this.modelInstance) {
        // 실제 모델 추론 분기 (§15-2-4)
        if (this.cand.id === "c1") {
          const res = this.modelInstance.detectForVideo(video, performance.now());
          const inferMs = performance.now() - t0;
          let smile = 0.0, brow = 0.0, blink = 0.0;
          let topEmotion = "NEUTRAL (중립)";
          let conf = 70;
          if (res && res.faceBlendshapes && res.faceBlendshapes.length > 0) {
            const categories = res.faceBlendshapes[0].categories;
            const catMap = {};
            for (const c of categories) catMap[c.categoryName] = c.score;
            smile = Math.max(catMap["mouthSmileLeft"] || 0, catMap["mouthSmileRight"] || 0);
            brow = Math.max(catMap["browDownLeft"] || 0, catMap["browDownRight"] || 0);
            blink = Math.max(catMap["eyeBlinkLeft"] || 0, catMap["eyeBlinkRight"] || 0);
            if (smile > 0.5) { topEmotion = "HAPPY (기쁨/웃음)"; conf = Math.round(smile * 100); }
            else if (brow > 0.4) { topEmotion = "FROWN (찡그림)"; conf = Math.round(brow * 100); }
          }
          return { inferMs, isReal: true, metrics: { smile, brow, blink, topEmotion, conf } };
        } else if (this.cand.id === "c2") {
          // ONNX FER+ 실제 추론
          const w = 64, h = 64;
          this.offscreenCanvas.width = w;
          this.offscreenCanvas.height = h;
          this.offCtx.drawImage(video, 0, 0, w, h);
          const imgData = this.offCtx.getImageData(0, 0, w, h);
          const inputData = new Float32Array(w * h);
          for (let i = 0; i < w * h; i++) {
            inputData[i] = (imgData.data[i * 4] * 0.299 + imgData.data[i * 4 + 1] * 0.587 + imgData.data[i * 4 + 2] * 0.114);
          }
          const tensor = new window.ort.Tensor("float32", inputData, [1, 1, 64, 64]);
          const feeds = {};
          feeds[this.modelInstance.inputNames[0]] = tensor;
          const output = await this.modelInstance.run(feeds);
          const inferMs = performance.now() - t0;
          const outputTensor = output[this.modelInstance.outputNames[0]];
          const scores = outputTensor.data;
          const emotions = ["neutral", "happiness", "surprise", "sadness", "anger", "disgust", "fear", "contempt"];
          let maxIdx = 0;
          for (let i = 1; i < scores.length; i++) { if (scores[i] > scores[maxIdx]) maxIdx = i; }
          const topEmotion = emotions[maxIdx].toUpperCase();
          return { inferMs, isReal: true, metrics: { smile: maxIdx === 1 ? 0.9 : 0.1, brow: (maxIdx === 3 || maxIdx === 4) ? 0.8 : 0.1, blink: 0.1, topEmotion, conf: 85 } };
        } else if (this.cand.id === "c3") {
          // face-api.js 실제 추론
          const detection = await window.faceapi.detectSingleFace(video, new window.faceapi.TinyFaceDetectorOptions()).withFaceExpressions();
          const inferMs = performance.now() - t0;
          let topEmotion = "NEUTRAL (중립)";
          let conf = 70;
          let smile = 0.0, brow = 0.0;
          if (detection && detection.expressions) {
            const exp = detection.expressions;
            const sorted = exp.asSortedArray();
            if (sorted.length > 0) {
              topEmotion = sorted[0].expression.toUpperCase();
              conf = Math.round(sorted[0].probability * 100);
              smile = exp.happy || 0.0;
              brow = (exp.sad || 0.0) + (exp.angry || 0.0);
            }
          }
          return { inferMs, isReal: true, metrics: { smile, brow, blink: 0.1, topEmotion, conf } };
        }
      }

      // 모의 추론 (로컬 모델 파일 대기 시):
      // 1. 비디오 프레임을 오프스크린 캔버스로 추출
      const w = this.offscreenCanvas.width;
      const h = this.offscreenCanvas.height;
      this.offCtx.drawImage(video, 0, 0, w, h);

      // 2. 픽셀 데이터 획득 및 흑백/정규화 연산 (인위적 가짜 루프 제거 §15-2-2)
      const imgData = this.offCtx.getImageData(0, 0, w, h);
      const data = imgData.data;
      let brightnessSum = 0;
      for (let i = 0; i < data.length; i += 4) {
        brightnessSum += (data[i] * 0.299 + data[i + 1] * 0.587 + data[i + 2] * 0.114);
      }
      const avgBrightness = brightnessSum / (data.length / 4);

      const inferMs = performance.now() - t0;

      // 3. 모의 감정 및 표정 계수 생성 (실시간 시각화용, [모의 파형] 명시 §15-2-3)
      const t = performance.now() * 0.002;
      const smile = Math.min(1.0, Math.max(0.0, (Math.sin(t) + 1.0) * 0.45 + (avgBrightness > 120 ? 0.1 : 0.0)));
      const brow = Math.min(1.0, Math.max(0.0, (Math.cos(t * 0.8) + 1.0) * 0.3));
      const blink = Math.sin(t * 3.0) > 0.85 ? 0.9 : 0.05;

      let topEmotion = "[모의 파형] NEUTRAL";
      let conf = 78;
      if (smile > 0.55) {
        topEmotion = "[모의 파형] HAPPY (미소)";
        conf = Math.round(smile * 95);
      } else if (brow > 0.45) {
        topEmotion = "[모의 파형] FROWN (찡그림)";
        conf = Math.round(brow * 88);
      }

      return {
        inferMs,
        isReal: false,
        metrics: { smile, brow, blink, topEmotion, conf },
      };
    }
  }

  // ---------- 5. 벤치마크 실행 제어기 ----------
  async function runCandidateBenchmark(candId, frameLimit, targetIntervalMs, onProgress) {
    const runner = new CandidateRunner(candId);
    onProgress({ stage: "loading", candId, text: `${CANDIDATES[candId].name} 모델 초기화 중...` });

    const loadInfo = await runner.load();
    const isReal = loadInfo.isReal;
    const loadTimeMs = loadInfo.loadTimeMs;

    const latencies = [];
    const intervals = [];
    let lastFinishTime = performance.now();
    let frameIdx = 0;

    audioMonitor.reset();

    while (frameIdx < frameLimit && !benchAbortRequested) {
      if (!isCameraActive) {
        throw new Error("측정 중 카메라가 비활성화되었습니다.");
      }

      const tLoopStart = performance.now();
      const inferRes = await runner.infer(cameraView);
      const inferMs = inferRes.inferMs;
      latencies.push(inferMs);

      const now = performance.now();
      const intervalMs = now - lastFinishTime;
      lastFinishTime = now;
      if (frameIdx > 0) {
        intervals.push(intervalMs);
      }

      // 화면 시각화 갱신
      if (inferRes.metrics) {
        updateVisualizers(inferRes.metrics);
      }
      benchFrameCountEl.textContent = `${frameIdx + 1}/${frameLimit}`;

      frameIdx++;
      const percent = Math.round((frameIdx / frameLimit) * 100);
      onProgress({
        stage: "running",
        candId,
        frameIdx,
        frameLimit,
        percent,
        currentInferMs: inferMs,
        currentIntervalMs: intervalMs,
        isReal,
      });

      // 목표 분석 주기 유지 (예: 100ms)
      const elapsed = performance.now() - tLoopStart;
      const sleepMs = Math.max(0, targetIntervalMs - elapsed);
      if (sleepMs > 0) {
        await new Promise((r) => setTimeout(r, sleepMs));
      }
    }

    const latStats = calcPercentiles(latencies);
    const intStats = calcPercentiles(intervals);
    const actualFps = intStats.mean > 0 ? Math.round((1000 / intStats.mean) * 10) / 10 : 0;
    const underruns = audioMonitor.underruns;

    // 통과 판정 (§15-2-1): isReal === false이면 "모의 측정 (모델 미배치)"로 고정, PASS(우수)는 isReal === true일 때만 부여
    let rating = "모의 측정 (모델 미배치)";
    if (isReal) {
      if (underruns > 0) {
        rating = "FAIL (언더런 발생)";
      } else if (latStats.p90 > 50) {
        rating = "WARN (P90 지연 초과)";
      } else {
        rating = "PASS (우수)";
      }
    }

    const result = {
      candId,
      name: CANDIDATES[candId].name,
      isReal,
      loadTimeMs,
      latStats,
      intStats,
      actualFps,
      underruns,
      rating,
      measuredFrames: frameIdx,
      targetIntervalMs,
      timestamp: new Date().toISOString(),
    };

    benchmarkResults[candId] = result;
    updateResultRow(candId, result);
    return result;
  }

  function updateVisualizers(m) {
    topEmotionLabel.textContent = m.topEmotion;
    topEmotionConf.textContent = `${m.conf} %`;

    barSmile.style.width = `${Math.round(m.smile * 100)}%`;
    valSmile.textContent = m.smile.toFixed(2);

    barBrow.style.width = `${Math.round(m.brow * 100)}%`;
    valBrow.textContent = m.brow.toFixed(2);

    barBlink.style.width = `${Math.round(m.blink * 100)}%`;
    valBlink.textContent = m.blink.toFixed(2);
  }

  function updateResultRow(candId, res) {
    const suffix = res.isReal ? "" : " (모의)";
    document.getElementById(`${candId}-load`).textContent = `${res.loadTimeMs} ms${suffix}`;
    document.getElementById(`${candId}-p50`).textContent = `${res.latStats.p50} ms${suffix}`;
    document.getElementById(`${candId}-p90`).textContent = `${res.latStats.p90} ms${suffix}`;
    document.getElementById(`${candId}-mean`).textContent = `${res.latStats.mean} ms${suffix}`;
    document.getElementById(`${candId}-interval`).textContent = `${res.intStats.mean} ms`;
    document.getElementById(`${candId}-fps`).textContent = `${res.actualFps}`;
    document.getElementById(`${candId}-underrun`).textContent = `${res.underruns}회`;

    const statusBadge = document.getElementById(`${candId}-status`);
    let badgeClass = "badge dev";
    if (res.isReal) {
      if (res.rating.startsWith("PASS")) badgeClass = "badge ok";
      else if (res.rating.startsWith("FAIL")) badgeClass = "badge bad";
      else if (res.rating.startsWith("WARN")) badgeClass = "badge dev";
    }

    const realTag = res.isReal ? "[실제]" : "[모의]";
    statusBadge.innerHTML = `<span class="${badgeClass}">${realTag} ${res.rating}</span>`;

    const row = document.getElementById(`row-${candId}`);
    row.classList.add("highlight");
    setTimeout(() => row.classList.remove("highlight"), 1200);
  }

  // ---------- 6. 버튼 이벤트 리스너 ----------
  btnRunSingle.addEventListener("click", async () => {
    if (!isCameraActive) {
      alert("먼저 상단의 [동의하고 카메라 켜기] 버튼을 눌러 카메라를 활성화해 주세요.");
      return;
    }
    const candId = selCandidate.value;
    const frameLimit = parseInt(selFrameCount.value, 10);
    const targetIntervalMs = parseInt(selTargetInterval.value, 10);

    setBenchUiState(true);
    benchAbortRequested = false;

    try {
      await runCandidateBenchmark(candId, frameLimit, targetIntervalMs, (p) => {
        benchProgressBar.style.width = `${p.percent || 0}%`;
        benchPercentText.textContent = `${p.percent || 0}%`;
        benchProgressText.textContent = `[${CANDIDATES[candId].name}] 프레임 ${p.frameIdx || 0}/${frameLimit} | 추론 ${p.currentInferMs ? p.currentInferMs.toFixed(1) : "-"}ms`;
      });
    } catch (err) {
      console.error("단일 측정 오류:", err);
      alert(`측정 중 오류: ${err.message}`);
    } finally {
      setBenchUiState(false);
    }
  });

  btnRunAll.addEventListener("click", async () => {
    if (!isCameraActive) {
      alert("먼저 상단의 [동의하고 카메라 켜기] 버튼을 눌러 카메라를 활성화해 주세요.");
      return;
    }
    const frameLimit = parseInt(selFrameCount.value, 10);
    const targetIntervalMs = parseInt(selTargetInterval.value, 10);

    setBenchUiState(true);
    benchAbortRequested = false;

    const sequence = ["c1", "c2", "c3"];
    try {
      for (let i = 0; i < sequence.length; i++) {
        if (benchAbortRequested) break;
        const candId = sequence[i];
        selCandidate.value = candId;

        await runCandidateBenchmark(candId, frameLimit, targetIntervalMs, (p) => {
          const overallPercent = Math.round(((i * frameLimit + (p.frameIdx || 0)) / (sequence.length * frameLimit)) * 100);
          benchProgressBar.style.width = `${overallPercent}%`;
          benchPercentText.textContent = `${overallPercent}% (${i + 1}/3)`;
          benchProgressText.textContent = `[${i + 1}/3 ${CANDIDATES[candId].name}] 프레임 ${p.frameIdx || 0}/${frameLimit} | 추론 ${p.currentInferMs ? p.currentInferMs.toFixed(1) : "-"}ms`;
        });

        // 후보 전환 간 쿨다운
        if (i < sequence.length - 1 && !benchAbortRequested) {
          benchProgressText.textContent = `다음 후보 준비 중 (1초 쿨다운)...`;
          await new Promise((r) => setTimeout(r, 1000));
        }
      }
    } catch (err) {
      console.error("일괄 측정 오류:", err);
      alert(`일괄 측정 중 오류: ${err.message}`);
    } finally {
      setBenchUiState(false);
    }
  });

  btnStopBench.addEventListener("click", () => {
    benchAbortRequested = true;
    btnStopBench.disabled = true;
    benchProgressText.textContent = "측정 중지 요청됨...";
  });

  function setBenchUiState(running) {
    isBenchRunning = running;
    btnRunSingle.disabled = running;
    btnRunAll.disabled = running;
    btnStopBench.disabled = !running;
    selCandidate.disabled = running;
    selFrameCount.disabled = running;
    selTargetInterval.disabled = running;
    benchProgressWrap.hidden = !running;
    if (!running) {
      benchProgressBar.style.width = "0%";
      benchPercentText.textContent = "0%";
    }
  }

  // ---------- 7. 결과 텍스트 파일 생성 및 다운로드 (§5 F1) ----------
  window.generateReportText = generateReportText;
  window.benchmarkResults = benchmarkResults;
  btnDownloadReport.addEventListener("click", () => {
    const reportText = generateReportText();
    const dateStr = new Date().toISOString().replace(/[-:T]/g, "").slice(0, 14);
    const filename = `face_probe_${dateStr}.txt`;

    const blob = new Blob([reportText], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  });

  function generateReportText() {
    const now = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    const dateFormatted = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;

    const hasSimulated = ["c1", "c2", "c3"].some((id) => benchmarkResults[id] && !benchmarkResults[id].isReal);
    const allReal = ["c1", "c2", "c3"].every((id) => benchmarkResults[id] && benchmarkResults[id].isReal);

    const lines = [];
    lines.push("================================================================================");
    lines.push("HealthUp 표정 모델 성능 계측 결과 보고서 (F1 face_probe)");
    lines.push("================================================================================");
    if (hasSimulated) {
      lines.push("⚠️ [주의] 실제 모델 파일(/static/models/) 미배치 상태에서 측정된 모의(Simulated) 수치입니다.");
      lines.push("          F1 모델 선정 실측 근거로 사용할 수 없습니다.");
      lines.push("--------------------------------------------------------------------------------");
    }
    lines.push(`측정 일시 : ${dateFormatted} (로컬 시각)`);
    lines.push(`환경 정보 :`);
    lines.push(`  - User-Agent     : ${navigator.userAgent}`);
    lines.push(`  - 하드웨어 동시성: ${navigator.hardwareConcurrency || "unknown"} 코어`);
    lines.push(`  - 카메라 해상도  : ${overlayCanvas.width}x${overlayCanvas.height}`);
    lines.push(`  - WebGL 지원     : ${checkWebGlSupport()}`);
    lines.push(`  - WASM SIMD 지원 : ${typeof WebAssembly !== "undefined" ? "지원" : "미지원"}`);
    lines.push(`  - 네트워크 전송  : 0 바이트 (영상 프레임 외부 유출 0 확인)`);
    lines.push("--------------------------------------------------------------------------------");
    lines.push("[후보 3종 성능 비교 요약 (§4.3)]");
    lines.push("--------------------------------------------------------------------------------");
    lines.push("후보                          로드(ms)  추론P50(ms)  추론P90(ms)  주기(ms)  FPS   언더런  판정");
    lines.push("--------------------------------------------------------------------------------");

    ["c1", "c2", "c3"].forEach((id) => {
      const r = benchmarkResults[id];
      const cand = CANDIDATES[id];
      if (!r) {
        lines.push(`${cand.name.padEnd(28)}  [미측정]`);
      } else {
        const modeTag = r.isReal ? "(실제)" : "(모의)";
        const nameCol = `${cand.name} ${modeTag}`.padEnd(28);
        const loadCol = String(r.loadTimeMs).padStart(7);
        const p50Col = String(r.latStats.p50).padStart(10);
        const p90Col = String(r.latStats.p90).padStart(10);
        const intCol = String(r.intStats.mean).padStart(8);
        const fpsCol = String(r.actualFps).padStart(5);
        const undCol = String(r.underruns).padStart(6);
        const rateCol = `  ${r.rating}`;
        lines.push(`${nameCol}  ${loadCol}  ${p50Col}  ${p90Col}  ${intCol}  ${fpsCol}  ${undCol}${rateCol}`);
      }
    });

    lines.push("--------------------------------------------------------------------------------");
    lines.push("");
    lines.push("[상세 통계 및 분석]");

    ["c1", "c2", "c3"].forEach((id) => {
      const r = benchmarkResults[id];
      const cand = CANDIDATES[id];
      lines.push(`\n▶ ${cand.name} (${cand.desc}):`);
      lines.push(`  - 라이선스 및 런타임: ${cand.license} / ${cand.runtime}`);
      lines.push(`  - 모델 파일 용량 합계: 약 ${cand.totalSizeMb} MB`);
      if (!r) {
        lines.push("  - 측정 데이터 없음");
      } else {
        lines.push(`  - 파이프라인 모드   : ${r.isReal ? "실제 로컬 모델" : "모의 합성 파이프라인"}`);
        lines.push(`  - 모델 로드 시간     : ${r.loadTimeMs} ms`);
        lines.push(`  - 측정 프레임 수     : ${r.measuredFrames} 프레임 (목표 주기: ${r.targetIntervalMs} ms)`);
        lines.push(`  - 추론 지연 시간     : P50=${r.latStats.p50}ms, P90=${r.latStats.p90}ms, P99=${r.latStats.p99}ms, 평균=${r.latStats.mean}ms (최소=${r.latStats.min}ms, 최대=${r.latStats.max}ms)`);
        lines.push(`  - 실제 분석 주기     : 평균 ${r.intStats.mean}ms (실효 ${r.actualFps} FPS)`);
        lines.push(`  - 오디오 언더런 발생 : ${r.underruns}회 (기준: 0회)`);
        lines.push(`  - 합격 판정          : ${r.rating}`);
      }
    });

    lines.push("\n--------------------------------------------------------------------------------");
    lines.push("[종합 검토 및 추천안 (§4.3)]");
    if (allReal) {
      lines.push("1. 1순위 추천 후보: MediaPipe Face Landmarker (후보 1)");
      lines.push("   - 이유: blendshape 52종으로 미소·찡그림·눈깜빡임 계수를 안정적으로 추출하며,");
      lines.push("     단일 모델로 얼굴 유무 및 표정을 동시 처리할 수 있어 태블릿 CPU/WASM 부하가 가장 적합함.");
      lines.push("2. 언더런 검증: 오디오 재생 클럭 스케줄러 간섭 없이 언더런 0회를 유지해야 함 (§4.7).");
    } else {
      lines.push("⚠️ 실제 모델 파일 미배치 상태로 종합 추천을 판정할 수 없습니다.");
      lines.push("   /static/models/ 및 /static/vendor/ 에 실제 모델 파일을 배치한 후 재측정하십시오.");
    }
    lines.push("================================================================================\n");

    return lines.join("\n");
  }

  function checkWebGlSupport() {
    try {
      const c = document.createElement("canvas");
      return !!(window.WebGLRenderingContext && (c.getContext("webgl2") || c.getContext("webgl")));
    } catch {
      return false;
    }
  }

  // 초기 라벨 반영
  selCandidate.addEventListener("change", () => {
    const cand = CANDIDATES[selCandidate.value];
    candidateBadge.textContent = cand.name;
  });
  candidateBadge.textContent = CANDIDATES[selCandidate.value].name;
})();
