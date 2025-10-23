import numpy as np
from .loader import *
from tqdm import tqdm
from .source2info import Source2Info
from loguru import logger
import torch
import os
import pickle
import hashlib
from datetime import datetime
import glob


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
        self.cache_dir = "cache/avatar_registrar"
        os.makedirs(self.cache_dir, exist_ok=True)

    def register(
            self,
            source_path,  # image | video
            max_dim=1920,
            n_frames=-1,
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
        # Stream frames sequentially
        if type(source_path) == bytes:
            is_image_flag = True
            rgb = load_image(source_path, max_dim)
            for rgb in tqdm([rgb], desc='register avatar'):
                info = self.source2info(rgb, last_lmk, **kwargs)
                for k in keys:
                    source_info[f"{k}_lst"].append(info[k])
                source_info["img_rgb_lst"].append(rgb)
                last_lmk = info["lmk203"]

        elif is_video(source_path):
            is_image_flag = False
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
                    # source_info["img_rgb_lst"].append(frame_rgb)
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

        return source_info

    def __call__(self, *args, **kwargs):
        return self.register(*args, **kwargs)
