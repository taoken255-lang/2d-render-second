# grid-sample3d-trt-plugin — сборка .so (TensorRT 10.13 + CUDA 12.9, RTX 5070 sm_120)

Собираем `libgrid_sample_3d_plugin.so` из upstream-репозитория и проверяем,
что сборка содержит архитектуру **sm_120**.

## Источник

- https://github.com/SeanWangJS/grid-sample3d-trt-plugin

## Цели сборки

- TensorRT: **10.13.x**
- CUDA: **12.9**
- GPU: **RTX 5070 (sm_120)**

## Требования

- Docker
- NVIDIA драйвер, совместимый с CUDA 12.9
- Linux-машина с GPU (модель не принципиальна)

## Быстрый старт (build → extract → verify)

> Запускай команды из директории, где лежит `Dockerfile`.

### 1) Build образа

```bash
IMAGE="grid3d-build:trt10.13-cuda12.9"
TRT_VER="10.13.3.9-1+cuda12.9"

docker build -t "${IMAGE}" --build-arg TRT_VER="${TRT_VER}" .
```

### 2) Извлечение .so

```bash
CID=$(docker create "${IMAGE}")
docker cp "${CID}":/opt/grid-sample3d-trt-plugin/build/libgrid_sample_3d_plugin.so ./libgrid_sample_3d_plugin.so
docker rm "${CID}"

ls -lh ./libgrid_sample_3d_plugin.so
```

### 3) Проверка sm_120 в .so

```bash
docker run --rm -it "${IMAGE}" bash -lc '
grep -n "CMAKE_CUDA_ARCHITECTURES" /opt/grid-sample3d-trt-plugin/build/CMakeCache.txt

apt-get update >/dev/null
apt-get install -y --no-install-recommends cuda-cuobjdump-12-9 >/dev/null

SO=/opt/grid-sample3d-trt-plugin/build/libgrid_sample_3d_plugin.so
cuobjdump --dump-elf "$SO" | grep -E "sm_120" | head
'
```

Ожидаемо увидеть строки вида `arch = sm_120`.

## Полезные заметки

- Если меняешь версию TensorRT, синхронно обнови `TRT_VER` и тег `IMAGE`.
- `.so` извлекается в текущую директорию как `./libgrid_sample_3d_plugin.so`.
- Эту библиотеку можно затем использовать и с CUDA 12.8.
