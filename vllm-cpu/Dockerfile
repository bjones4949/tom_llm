FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      libtcmalloc-minimal4 libnuma1 g++ \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir vllm-cpu==0.30.0 \
      --index-url https://download.pytorch.org/whl/cpu \
      --extra-index-url https://pypi.org/simple

# vllm-cpu installs as "vllm-cpu", but vLLM looks itself up as "vllm"
# and checks the version for "cpu" to select the CPU backend.
RUN SITE=$(python -c "import site; print(site.getsitepackages()[0])") && \
    mkdir -p "$SITE/vllm-0.30.0.dist-info" && \
    printf 'Metadata-Version: 2.1\nName: vllm\nVersion: 0.30.0+cpu\n' \
      > "$SITE/vllm-0.30.0.dist-info/METADATA" && \
    python -c "import importlib.metadata as m; print('vllm version:', m.version('vllm'))"

ENV VLLM_TARGET_DEVICE=cpu
ENV LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libtcmalloc_minimal.so.4

ENTRYPOINT ["vllm", "serve"]