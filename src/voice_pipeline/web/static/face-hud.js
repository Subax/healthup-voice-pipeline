"use strict";

// 개발 모드 얼굴 상태 패널에 넣을 문구. DOM과 네트워크는 다루지 않는다.
(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) root.FaceHud = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  function faceHudVisible(mode) {
    return mode === "dev";
  }

  function restoreFaceOnLoad(face, consented, mode) {
    if (!consented) {
      return { face: false, startCamera: false, showPreview: false, showStatus: false };
    }
    const on = !!face;
    return {
      face: on,
      startCamera: on,
      showPreview: on,
      showStatus: on && faceHudVisible(mode),
    };
  }

  function talkLayout(faceOn, mode, width) {
    const camera = !!faceOn;
    const dev = faceHudVisible(mode);
    const w = Number(width);
    const narrow = Number.isFinite(w) && w <= 700;
    return {
      showCamera: camera,
      showOverlay: camera && dev,
      showStatus: camera && dev,
      columns: camera && !narrow ? 2 : 1,
    };
  }

  function scoreText(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return "없음";
    return n.toFixed(2);
  }

  function emotionText(emotions) {
    const rows = (emotions || [])
      .map((row) => ({
        label: String((row && row.label) || "").trim(),
        score: Number(row && row.score),
      }))
      .filter((row) => row.label && Number.isFinite(row.score))
      .sort((a, b) => b.score - a.score)
      .slice(0, 3);
    if (!rows.length) return "없음";
    return rows.map((row) => row.label + " " + row.score.toFixed(2)).join(", ");
  }

  function lastSentText(at, sent) {
    if (!sent || !sent.label) return "아직 없음";
    const when = at ? String(at) + " " : "";
    const n = sent.n_frames == null ? "?" : sent.n_frames;
    const total = sent.total_frames == null ? "?" : sent.total_frames;
    return when + sent.label + ", 신뢰도 " + scoreText(sent.conf) + ", 관측 " + scoreText(sent.coverage) + ", 프레임 " + n + "/" + total;
  }

  function lines(state) {
    const src = state || {};
    const camera = src.cameraError
      ? String(src.camera || "오류") + ": " + src.cameraError
      : (src.camera || "대기");
    return {
      detected: src.detected ? "있음" : "없음",
      score: src.detected ? scoreText(src.score) : "없음",
      count: String(Math.max(0, Number(src.faceCount) || 0)),
      emotions: emotionText(src.emotions),
      fps: Number.isFinite(Number(src.fps)) ? Number(src.fps).toFixed(1) + " fps" : "없음",
      lastSent: lastSentText(src.lastSentAt, src.lastSent),
      engine: src.engine || "없음",
      camera: camera,
    };
  }

  return {
    faceHudVisible: faceHudVisible,
    restoreFaceOnLoad: restoreFaceOnLoad,
    talkLayout: talkLayout,
    lines: lines,
  };
});
