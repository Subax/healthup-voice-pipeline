#!/usr/bin/env bash
# ==============================================================================
# scripts/fetch_face_models.sh
# HealthUp F1 표정 모델 및 런타임 파일 다운로드 스크립트 (webapp_plan.md §4.3, §5 F1)
#
# 공식 배포처 출처 URL, 파일 크기 및 SHA256 체크섬 기록:
# ------------------------------------------------------------------------------
# [후보 1: MediaPipe Face Landmarker (Google)]
# 1) face_landmarker.task (3.58 MB / 3,758,596 bytes)
#    - URL: https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task
#    - SHA256: 64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff
# 2) vision_bundle.mjs (133.7 KB / 136,870 bytes)
#    - URL: https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/vision_bundle.mjs
#    - SHA256: e77f281f9619150d937023c355bae170e9120e3b9e43f1e23a2a7bee07197669
# 3) wasm/vision_wasm_internal.js (204.9 KB / 209,826 bytes)
#    - URL: https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/wasm/vision_wasm_internal.js
#    - SHA256: 9440cf0cc0cea21800e31581ec32aeedcc5fbf9df4509796bbc7d3f99e52ab9c
# 4) wasm/vision_wasm_internal.wasm (8.99 MB / 9,423,986 bytes)
#    - URL: https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/wasm/vision_wasm_internal.wasm
#    - SHA256: f82a8e6c05e08a44cc9f9e7ec5f845935bcbb1b1500ebe8c2f4812fb4e2917dc
# 5) wasm/vision_wasm_nosimd_internal.js (204.8 KB / 209,735 bytes)
#    - URL: https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/wasm/vision_wasm_nosimd_internal.js
#    - SHA256: abe9b6fbeaf86fcb53a5edce3926c82ccb0619e18fed4d9d9ce561ee7f55e054
# 6) wasm/vision_wasm_nosimd_internal.wasm (8.86 MB / 9,294,247 bytes)
#    - URL: https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/wasm/vision_wasm_nosimd_internal.wasm
#    - SHA256: 38b61feab2fd7934e05cbe9f68baa308978a5e3b7f85c1913bb8ae89b8ef8b97
#
# [후보 2: RFB-320 Face Detector + FER+ ONNX (ONNX Model Zoo)]
# 7) version-RFB-320.onnx (1.21 MB / 1,270,727 bytes)
#    - URL: https://github.com/onnx/models/raw/main/validated/vision/body_analysis/ultraface/models/version-RFB-320.onnx
#    - SHA256: 34cd7e60aeff28744c657de7a3dc64e872d506741de66987f3426f2b79f88017
# 8) emotion-ferplus-8.onnx (33.42 MB / 35,040,571 bytes)
#    - URL: https://github.com/onnx/models/raw/main/validated/vision/body_analysis/emotion_ferplus/model/emotion-ferplus-8.onnx
#    - SHA256: a2a2ba6a335a3b29c21acb6272f962bd3d47f84952aaffa03b60986e04efa61c
# 9) ort.min.js (529.9 KB / 542,655 bytes)
#    - URL: https://cdn.jsdelivr.net/npm/onnxruntime-web@1.17.3/dist/ort.min.js
#    - SHA256: 88490a46ffcbdb497d7c11402f6b6d39a97f00185f641f0d0a946556ce931752
# 10) ort-wasm-simd.wasm (10.06 MB / 10,551,547 bytes)
#    - URL: https://cdn.jsdelivr.net/npm/onnxruntime-web@1.17.3/dist/ort-wasm-simd.wasm
#    - SHA256: 6783fcd6647ce1b426a527c31412a9c40894552faa609e9c931b3239a3438071
# 11) ort-wasm.wasm (9.28 MB / 9,726,745 bytes)
#    - URL: https://cdn.jsdelivr.net/npm/onnxruntime-web@1.17.3/dist/ort-wasm.wasm
#    - SHA256: 969afd00c79afead28a90b6acd01bd64abe4b5ac8bc9650c3dee3aefc6e0cea6
#
# [후보 3: face-api.js (TinyFaceDetector + FaceExpressionNet)]
# 12) tiny_face_detector_model-weights_manifest.json (2.88 KB / 2,953 bytes)
#     - URL: https://raw.githubusercontent.com/justadudewhohacks/face-api.js/master/weights/tiny_face_detector_model-weights_manifest.json
#     - SHA256: 14c60659a31b6b7b1320077171b8f8adcb24ef0e62dde62ce603bcb49a1b49b5
# 13) tiny_face_detector_model-shard1 (188.8 KB / 193,321 bytes)
#     - URL: https://raw.githubusercontent.com/justadudewhohacks/face-api.js/master/weights/tiny_face_detector_model-shard1
#     - SHA256: b7503ce7df31039b1c43316a9b865cab6a70dd748cc602d3fa28b551503c3871
# 14) face_expression_model-weights_manifest.json (6.23 KB / 6,384 bytes)
#     - URL: https://raw.githubusercontent.com/justadudewhohacks/face-api.js/master/weights/face_expression_model-weights_manifest.json
#     - SHA256: 960cbe959fe0328965dd18662d839f197df6e27607da4f726cd0c826e47fc936
# 15) face_expression_model-shard1 (321.7 KB / 329,468 bytes)
#     - URL: https://raw.githubusercontent.com/justadudewhohacks/face-api.js/master/weights/face_expression_model-shard1
#     - SHA256: 9a9840f2cf1f4c7eab95f197512569345c00d2426754d4608b92af30e0300f3d
# 16) face_landmark_68_tiny_model-weights_manifest.json (4,441 bytes)
#     - URL: https://raw.githubusercontent.com/justadudewhohacks/face-api.js/master/weights/face_landmark_68_tiny_model-weights_manifest.json
#     - SHA256: 3c63b8984302c187b218d9ef5aa149ed8c2c7fa3fe54db078614692bc48d153c
# 17) face_landmark_68_tiny_model-shard1 (77,224 bytes)
#     - URL: https://raw.githubusercontent.com/justadudewhohacks/face-api.js/master/weights/face_landmark_68_tiny_model-shard1
#     - SHA256: b98e9f2f7da76f8a6dda9741a36ed485b224b889d552de2b2c1bb16217f67bfc
# 18) face-api.js (1.27 MB / 1,333,943 bytes)
#     - URL: https://cdn.jsdelivr.net/npm/@vladmandic/face-api/dist/face-api.js
#     - SHA256: 0160f7af3a8c78cece45c7ecc765383bad74becfd438bb787cdd627b2d6f2cf6
# ==============================================================================

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATIC_DIR="${ROOT_DIR}/src/voice_pipeline/web/static"

MODELS_DIR="${STATIC_DIR}/models"
VENDOR_DIR="${STATIC_DIR}/vendor"

mkdir -p "${MODELS_DIR}/face-api"
mkdir -p "${VENDOR_DIR}/tasks-vision/wasm"
mkdir -p "${VENDOR_DIR}/onnxruntime-web"
mkdir -p "${VENDOR_DIR}/face-api"

fetch_file() {
    local url="$1"
    local dest="$2"
    if [ -f "${dest}" ]; then
        echo "  [건너뜀] 이미 존재함: ${dest}"
    else
        echo "  [다운로드] $(basename "${dest}") <- ${url}"
        curl -sSL --retry 3 "${url}" -o "${dest}"
    fi
}

echo "=== HealthUp F1 표정 모델 및 벤더 런타임 파일 다운로드 시작 ==="

# 1. MediaPipe Face Landmarker
echo "1) MediaPipe Face Landmarker 파일 수신 중..."
fetch_file "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task" \
           "${MODELS_DIR}/face_landmarker.task"
fetch_file "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/vision_bundle.mjs" \
           "${VENDOR_DIR}/tasks-vision/vision_bundle.mjs"
fetch_file "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/wasm/vision_wasm_internal.js" \
           "${VENDOR_DIR}/tasks-vision/wasm/vision_wasm_internal.js"
fetch_file "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/wasm/vision_wasm_internal.wasm" \
           "${VENDOR_DIR}/tasks-vision/wasm/vision_wasm_internal.wasm"
fetch_file "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/wasm/vision_wasm_nosimd_internal.js" \
           "${VENDOR_DIR}/tasks-vision/wasm/vision_wasm_nosimd_internal.js"
fetch_file "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/wasm/vision_wasm_nosimd_internal.wasm" \
           "${VENDOR_DIR}/tasks-vision/wasm/vision_wasm_nosimd_internal.wasm"

# 2. ONNX Face Detector + FER+
echo "2) ONNX FER+ 및 onnxruntime-web 파일 수신 중..."
fetch_file "https://github.com/onnx/models/raw/main/validated/vision/body_analysis/ultraface/models/version-RFB-320.onnx" \
           "${MODELS_DIR}/version-RFB-320.onnx"
fetch_file "https://github.com/onnx/models/raw/main/validated/vision/body_analysis/emotion_ferplus/model/emotion-ferplus-8.onnx" \
           "${MODELS_DIR}/emotion-ferplus-8.onnx"
fetch_file "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.17.3/dist/ort.min.js" \
           "${VENDOR_DIR}/onnxruntime-web/ort.min.js"
fetch_file "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.17.3/dist/ort-wasm-simd.wasm" \
           "${VENDOR_DIR}/onnxruntime-web/ort-wasm-simd.wasm"
fetch_file "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.17.3/dist/ort-wasm.wasm" \
           "${VENDOR_DIR}/onnxruntime-web/ort-wasm.wasm"

# 3. face-api.js
echo "3) face-api.js 및 모델 가중치 파일 수신 중..."
fetch_file "https://raw.githubusercontent.com/justadudewhohacks/face-api.js/master/weights/tiny_face_detector_model-weights_manifest.json" \
           "${MODELS_DIR}/face-api/tiny_face_detector_model-weights_manifest.json"
fetch_file "https://raw.githubusercontent.com/justadudewhohacks/face-api.js/master/weights/tiny_face_detector_model-shard1" \
           "${MODELS_DIR}/face-api/tiny_face_detector_model-shard1"
fetch_file "https://raw.githubusercontent.com/justadudewhohacks/face-api.js/master/weights/face_expression_model-weights_manifest.json" \
           "${MODELS_DIR}/face-api/face_expression_model-weights_manifest.json"
fetch_file "https://raw.githubusercontent.com/justadudewhohacks/face-api.js/master/weights/face_expression_model-shard1" \
           "${MODELS_DIR}/face-api/face_expression_model-shard1"
fetch_file "https://raw.githubusercontent.com/justadudewhohacks/face-api.js/master/weights/face_landmark_68_tiny_model-weights_manifest.json" \
           "${MODELS_DIR}/face-api/face_landmark_68_tiny_model-weights_manifest.json"
fetch_file "https://raw.githubusercontent.com/justadudewhohacks/face-api.js/master/weights/face_landmark_68_tiny_model-shard1" \
           "${MODELS_DIR}/face-api/face_landmark_68_tiny_model-shard1"
fetch_file "https://cdn.jsdelivr.net/npm/@vladmandic/face-api/dist/face-api.js" \
           "${VENDOR_DIR}/face-api/face-api.js"

echo "=== 모든 파일 수신 및 확인 완료 ==="
ls -lh "${MODELS_DIR}"
ls -lh "${VENDOR_DIR}"
