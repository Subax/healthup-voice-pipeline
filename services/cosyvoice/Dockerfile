# CosyVoice3 서비스 이미지. 베이스의 torch(GB10 CUDA 13)를 건드리지 않는다.
# 빌드 전에 CosyVoice 소스가 필요하다: ./build.sh 가 clone 후 빌드한다.
FROM nvcr.io/nvidia/vllm:26.07-py3

ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1
# torch 계열·numpy가 바뀌지 않게 고정
RUN python - <<'PY'
import importlib.metadata as m
pins = [f"{p}=={m.version(p)}" for p in ("torch", "torchvision", "numpy") ]
open("/tmp/constraints.txt", "w").write("\n".join(pins) + "\n")
PY

# 추론에 필요한 의존성만 (deepspeed·tensorrt·gradio·학습용 제외, ttsfrd 대신 wetext)
RUN pip install -c /tmp/constraints.txt \
      onnxruntime conformer diffusers hyperpyyaml inflect modelscope omegaconf \
      x-transformers wetext lightning hydra-core tiktoken numba more-itertools \
      pyarrow matplotlib wget gdown regex scipy pyworld \
 && pip install --no-deps -c /tmp/constraints.txt openai-whisper

# CosyVoice 는 transformers==4.51.3 을 전제로 한다. 5.x 에서는 Qwen2 KV-cache 동작이 달라 음성이 깨진다(CER≈1.0).
# huggingface_hub 는 diffusers 때문에 1.x 를 유지하므로 transformers 의 버전 검사만 끈다. 이 컨테이너는 vLLM 을 쓰지 않는다.
RUN pip install --no-deps -c /tmp/constraints.txt transformers==4.51.3 "tokenizers>=0.21,<0.22" \
 && printf 'def dep_version_check(pkg, hint=None):\n    pass\n' \
      > /usr/local/lib/python3.12/dist-packages/transformers/dependency_versions_check.py

# torchaudio: PyPI 휠은 torch 2.11용이라 CUDA 확장 버전 검사가 실패한다.
# 필요한 것은 순수 파이썬 부분(kaldi.fbank, transforms)뿐이라 검사만 끈다. torch는 건드리지 않는다.
RUN pip install --no-deps torchaudio==2.11.0 \
 && sed -i 's/^    _check_cuda_version()/    pass/' /usr/local/lib/python3.12/dist-packages/torchaudio/_extension/__init__.py

# 베이스 이미지의 libcufft.so.12 는 13KB 스텁이라 torch.stft/fft 가 실패한다. 실제 라이브러리를 앞에 둔다
RUN pip install --no-deps nvidia-cufft \
 && mkdir -p /opt/cufft && ln -s /usr/local/lib/python3.12/dist-packages/nvidia/cu13/lib/libcufft.so.12 /opt/cufft/libcufft.so.12
ENV LD_LIBRARY_PATH=/opt/cufft:${LD_LIBRARY_PATH}

COPY CosyVoice /opt/CosyVoice
# torchaudio 2.11 에는 load(backend='soundfile')가 없어 soundfile 로 바꾼다
RUN python - <<'PY'
p = "/opt/CosyVoice/cosyvoice/utils/file_utils.py"
s = open(p).read()
old = "speech, sample_rate = torchaudio.load(wav, backend='soundfile')"
new = ("import soundfile as sf; _d, sample_rate = sf.read(wav, dtype='float32', always_2d=True); "
       "speech = torch.from_numpy(_d.T.copy())")
assert old in s
open(p, "w").write(s.replace(old, new))
PY
ENV PYTHONPATH=/opt/CosyVoice:/opt/CosyVoice/third_party/Matcha-TTS
WORKDIR /opt/CosyVoice
COPY server.py /opt/server.py
ENTRYPOINT []
CMD ["python", "/opt/server.py"]
