import numpy as np
from .loader import *
from tqdm import tqdm
from .source2info import Source2Info
from loguru import logger
import torch
import os
import pickle
import time
import hashlib
import subprocess
from pathlib import Path
import shutil
from datetime import datetime

from service.progress.timing import TimingTracker, TimedReader


def _mean_filter(arr, k):
    n = arr.shape[0]
    half_k = k // 2
    res = []
    for i in range(n):
        s = max(0, i - half_k)
        e = min(n, i + half_k + 1)
        res.append(arr[s:e].mean(0))
    res = np.stack(res, 0)
    return res


def is_video(file_path):
    return filetype.is_video(file_path)


def load_image(image_bytes, max_dim=-1):   # <--------------------------------------------------------------------------
    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    h, w = img.shape[:2]
    if h % 2 != 0:
        img = img[:h - 1, :, :]
        h -= 1
    if w % 2 != 0:
        img = img[:, :w - 1, :]
        w -= 1
    logger.info(f"REGISTER AVATAR {w}x{h}, {len(img)}")
    new_h, new_w, rsz_flag = check_resize(h, w, max_dim)  # Ресайз под максимальный размер
    if rsz_flag:
        img = cv2.resize(img, (new_w, new_h))
    return img


def smooth_x_s_info_lst(x_s_info_list, ignore_keys=(), smo_k=13):
    keys = x_s_info_list[0].keys()
    N = len(x_s_info_list)
    smo_dict = {}
    for k in keys:
        _lst = [x_s_info_list[i][k] for i in range(N)]
        if k not in ignore_keys:
            _lst = np.stack(_lst, 0)
            _smo_lst = _mean_filter(_lst, smo_k)
        else:
            _smo_lst = _lst
        smo_dict[k] = _smo_lst

    smo_res = []
    for i in range(N):
        x_s_info = {k: smo_dict[k][i] for k in keys}
        smo_res.append(x_s_info)
    return smo_res


class AvatarRegistrar:
    """
    source image|video -> rgb_list -> source_info
    """
    def __init__(
        self,
        insightface_det_cfg,
        landmark106_cfg,
        landmark203_cfg,
        landmark478_cfg,
        appearance_extractor_cfg,
        motion_extractor_cfg,
    ):
        self.source2info = Source2Info(
            insightface_det_cfg,
            landmark106_cfg,
            landmark203_cfg,
            landmark478_cfg,
            appearance_extractor_cfg,
            motion_extractor_cfg,
        )
        self.cache_dir = "/app/cache"
        # os.makedirs(self.cache_dir, exist_ok=True)

    def _get_cache_key(self, source_path):
        h = hashlib.md5()
        with open(source_path, "rb") as f:
            while chunk := f.read(8 * 1024 * 1024):
                h.update(chunk)
        return str(h.hexdigest()[:12])

    def _convert_video(self, source_path):
        command = [
            "ffmpeg",
            "-i", source_path,
            "-c:v", "libx264",
            "-preset", "slow",
            "-crf", "25",
            "-tune", "grain",
            "-g", "1",
            "-keyint_min", "1",
            "-sc_threshold", "0",
            "-bf", "0",
            "-pix_fmt", "yuv420p",
            f"{source_path[:-4]}_conv.mp4"
        ]
        result = subprocess.run(command, capture_output=True, text=True)

        if result.returncode == 0:
            logger.info("VIDEO CONVERTED")
        else:
            logger.exception(f"VIDEO CONVERTATION ERROR {result.stderr}")

    # def _load_cache(self, cache_path):
    #     if os.path.exists(cache_path):
    #         try:
    #             with open(cache_path, 'rb') as f:
    #                 return pickle.load(f)
    #         except:
    #             pass
    #     return None

    def _load_cache(self, cache_path, file_name):
        logger.info("LOADING CACHE")
        full_pkl_path = f"{cache_path}/{file_name}.pkl"
        full_npy_path = f"{cache_path}/{file_name}.npy"
        if os.path.exists(full_pkl_path) and os.path.exists(full_npy_path):
            try:
                with open(full_pkl_path, 'rb') as f:
                    result = pickle.load(f)
                logger.info("PKL GOT")
                f_s_np = np.load(
                    full_npy_path,
                    mmap_mode="r"  # ❗ НЕ загружает в RAM
                )
                f_s_np = [torch.from_numpy(f_s_np[i]) for i in range(len(f_s_np))]
                logger.info(f"{type(f_s_np), len(f_s_np)}")
                logger.info("NPY GOT")
                result["f_s_lst"] = f_s_np
                return result
            except:
                pass
        return None

    def _save_cache(self, cache_path, file_name, source_info):
        logger.info("SAVING CACHE")
        full_pkl_path = f"{cache_path}/{file_name}.pkl"
        full_npy_path = f"{cache_path}/{file_name}.npy"
        try:
            f_s_lst = source_info.pop("f_s_lst")
            arr = np.stack([
                t.detach().cpu().numpy().astype(np.float16, copy=False)
                for t in f_s_lst
            ])
            logger.info("TENSORS CONVERTED")
            # temp_path = f"{cache_path[:-4]}_temp_{datetime.now().strftime('%H:%M:%S')}.pkl"
            # logger.info(f"SAVING IN TEMP FILE {temp_path}")
            with open(full_pkl_path, 'wb') as f:
                pickle.dump(source_info, f)
            logger.info("PKL SAVED")
            np.save(
                full_npy_path,
                arr,
                allow_pickle=False
            )
            logger.info("NPY SAVED")
            source_info["f_s_lst"] = f_s_lst

        except Exception as e:
            print(f"Failed to save cache: {e}")

    def register(
            self,
            source_path,  # image | video
            max_dim=1920,
            n_frames=-1,
            version_name=None,
            **kwargs,
    ):
        """
        kwargs:
            crop_scale: 2.3
            crop_vx_ratio: 0
            crop_vy_ratio: -0.125
            crop_flag_do_rot: True
        """

        # Will be filled progressively while streaming frames
        source_info = {
            "x_s_info_lst": [],
            "f_s_lst": [],
            "M_c2o_lst": [],
            "eye_open_lst": [],
            "eye_ball_lst": [],
            "img_rgb_lst": [],
        }
        keys = ["x_s_info", "f_s", "M_c2o", "eye_open", "eye_ball"]
        last_lmk = None
        t_avatar = TimingTracker(enabled=True, log_each=False)
        cache_key = "-"
        cache_hit = "bypass"
        # Stream frames sequentially
        if type(source_path) == bytes:
            is_image_flag = True
            with t_avatar.measure("avatar_video_processing"):
                rgb = load_image(source_path, max_dim)
                for rgb in tqdm([rgb], desc='register avatar'):
                    info = self.source2info(rgb, last_lmk, **kwargs)
                    for k in keys:
                        source_info[f"{k}_lst"].append(info[k])
                    source_info["img_rgb_lst"].append(rgb)
                    last_lmk = info["lmk203"]

        elif is_video(source_path):
            is_image_flag = False

            # if os.path.exists(f"{source_path[:-4]}_conv.mp4"):
            #     logger.info("FOUND CONVERTED VIDEO")
            #     source_path = f"{source_path[:-4]}_conv.mp4"
            # else:
            #     logger.info("NO CONVERTED VIDEO FOUND, CREATING")
            #     self._convert_video(source_path=source_path)
            #     source_path = f"{source_path[:-4]}_conv.mp4"

            p = Path(source_path)
            avatar_name = p.stem
            hash_key = self._get_cache_key(source_path=source_path)
            if version_name:
                file_name = f"{avatar_name}_{version_name}_{hash_key}"
            else:
                file_name = f"{avatar_name}_{hash_key}"
            cache_path = self.cache_dir
            cache_key = file_name

            full_pkl_path = f"{cache_path}/{file_name}.pkl"
            full_npy_path = f"{cache_path}/{file_name}.npy"
            cached_result = None
            cache_bytes = 0
            if os.path.exists(full_pkl_path):
                try:
                    cache_bytes = os.path.getsize(full_pkl_path)
                    cache_load_start = time.perf_counter()
                    with open(full_pkl_path, "rb") as f:
                        timed_reader = TimedReader(f)
                        cached_result = pickle.Unpickler(timed_reader).load()
                    cache_load_ms = (time.perf_counter() - cache_load_start) * 1000
                    t_avatar.add_ms("avatar_cache_total", cache_load_ms)
                    t_avatar.add_ms("avatar_cache_read", timed_reader.read_ms)
                    t_avatar.add_ms("avatar_cache_deserialize", max(cache_load_ms - timed_reader.read_ms, 0.0))
                    # Load f_s_lst from separate npy file
                    if os.path.exists(full_npy_path):
                        f_s_np = np.load(full_npy_path, mmap_mode="r")
                        cached_result["f_s_lst"] = [torch.from_numpy(f_s_np[i]) for i in range(len(f_s_np))]
                        logger.info(f"NPY loaded: {type(cached_result['f_s_lst'])}, {len(cached_result['f_s_lst'])}")
                except Exception:
                    cached_result = None
            if cached_result is not None:
                logger.info("FOUND CACHE, LOADING")
                cache_hit = "hit"
                cache_mb = cache_bytes / (1024 * 1024)
                t_avatar.log_summary(label="AVATAR", include_points=False, extra={
                    "cache_hit": cache_hit,
                    "cache_key": cache_key,
                    "cache_path": full_pkl_path,
                    "cache_mb": f"{cache_mb:.1f}"
                })
                return cached_result
            cache_hit = "miss"
            logger.info("NO CACHE FOUND, CREATING")


            with t_avatar.measure("avatar_video_processing"):
                reader = imageio.get_reader(source_path, "ffmpeg")
                new_h, new_w, rsz_flag = None, None, None
                try:
                    pbar = tqdm(desc='register avatar')
                    for idx, frame_rgb in enumerate(reader):
                        if n_frames > 0 and idx >= n_frames:
                            break
                        if rsz_flag is None:
                            h, w = frame_rgb.shape[:2]
                            new_h, new_w, rsz_flag = check_resize(h, w, max_dim)
                        if rsz_flag:
                            frame_rgb = cv2.resize(frame_rgb, (new_w, new_h))

                        info = self.source2info(frame_rgb, last_lmk, **kwargs)

                        info['f_s'] = torch.from_numpy(info['f_s'].astype(np.float16))
                        for k in keys:
                            source_info[f"{k}_lst"].append(info[k])
                        last_lmk = info["lmk203"]
                        pbar.update()
                    pbar.close()
                finally:
                    try:
                        reader.close()
                    except Exception:
                        pass
        else:
            raise ValueError(f"Unsupported source type: {source_path}")

        sc_f0 = source_info['x_s_info_lst'][0]['kp'].flatten()

        source_info["sc"] = sc_f0
        source_info["is_image_flag"] = is_image_flag
        if not is_image_flag:
            logger.info("CACHE CREATED, SAVING")
            with t_avatar.measure("avatar_cache_save"):
                self._save_cache(cache_path, file_name, source_info)

        t_avatar.log_summary(label="AVATAR", include_points=False, extra={"cache_hit": cache_hit, "cache_key": cache_key})

        return source_info

    def __call__(self, *args, **kwargs):
        return self.register(*args, **kwargs)
