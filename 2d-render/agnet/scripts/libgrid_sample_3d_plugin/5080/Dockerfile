FROM nvidia/cuda:12.9.1-devel-ubuntu22.04

ARG DEBIAN_FRONTEND=noninteractive
# Pick one from apt-cache madison tensorrt-dev (you showed these exist):
#   10.13.3.9-1+cuda12.9  (recommended, newest 10.13)
#   10.13.2.6-1+cuda12.9
ARG TRT_VER=10.13.3.9-1+cuda12.9

RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates build-essential cmake ninja-build pkg-config \
    python3 \
 && rm -rf /var/lib/apt/lists/*

# Pin TensorRT stack to EXACT TRT_VER so apt won't mix with 10.14.*
RUN set -eux; \
  cat >/etc/apt/preferences.d/tensorrt-pin <<EOF
Package: tensorrt*
Pin: version ${TRT_VER}
Pin-Priority: 1001

Package: libnvinfer*
Pin: version ${TRT_VER}
Pin-Priority: 1001

Package: libnvonnxparsers*
Pin: version ${TRT_VER}
Pin-Priority: 1001
EOF

# Install TensorRT dev/libs in the same version (allow-downgrades is important)
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends --allow-downgrades \
      "tensorrt-libs=${TRT_VER}" \
      "tensorrt-dev=${TRT_VER}" \
      "libnvinfer-dev=${TRT_VER}" \
      "libnvinfer-plugin-dev=${TRT_VER}" \
      "libnvonnxparsers-dev=${TRT_VER}" \
    ; \
    rm -rf /var/lib/apt/lists/*

ENV PATH=/usr/local/cuda/bin:${PATH}

WORKDIR /opt
RUN git clone --depth=1 https://github.com/SeanWangJS/grid-sample3d-trt-plugin.git
WORKDIR /opt/grid-sample3d-trt-plugin

# Repo hardcodes CUDA_ARCHITECTURES (you saw sm_70/80/86/89) -> force sm_120 (RTX 5070)
RUN set -eux; \
    echo "== Before patch =="; \
    grep -n "CUDA_ARCHITECTURES" CMakeLists.txt || true; \
    sed -i -E 's/(CUDA_ARCHITECTURES[[:space:]]+\")([^\"]+)(\")/\1120\3/g' CMakeLists.txt; \
    sed -i -E "s/(CUDA_ARCHITECTURES[[:space:]]+')([^']+)(')/\\1120\\3/g" CMakeLists.txt; \
    echo "== After patch =="; \
    grep -n "CUDA_ARCHITECTURES" CMakeLists.txt || true

# Build (also pass CMAKE_CUDA_ARCHITECTURES just in case)
RUN cmake -S . -B build -DTensorRT_ROOT=/usr -DCMAKE_CUDA_ARCHITECTURES=120 \
 && cmake --build build -j"$(nproc)"

# Verify: CMake saw 120 AND the .so actually contains sm_120 code
RUN set -eux; \
    grep -n "CMAKE_CUDA_ARCHITECTURES" build/CMakeCache.txt || true; \
    test -f build/libgrid_sample_3d_plugin.so; \
    apt-get update >/dev/null; \
    apt-get install -y --no-install-recommends cuda-cuobjdump-12-9 >/dev/null; \
    cuobjdump --dump-elf build/libgrid_sample_3d_plugin.so | grep -q "sm_120"; \
    echo "OK: sm_120 present in libgrid_sample_3d_plugin.so"
