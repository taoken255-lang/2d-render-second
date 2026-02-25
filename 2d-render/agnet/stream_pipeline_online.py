from loguru import logger
import numpy as np
import os
import threading
import queue
import time
import traceback
import json
import torch

from agnet.core.atomic_components.avatar_registrar import AvatarRegistrar, smooth_x_s_info_lst
from agnet.core.atomic_components.condition_handler import ConditionHandler, _mirror_index
from agnet.core.atomic_components.audio2motion import Audio2Motion
from agnet.core.atomic_components.motion_stitch import MotionStitch
from agnet.core.atomic_components.warp_f3d import WarpF3D
from agnet.core.atomic_components.decode_f3d import DecodeF3D
from agnet.core.atomic_components.putback import PutBack
from agnet.core.atomic_components.video_frame_extractor import SequentialPyAVFrameExtractor
from agnet.core.atomic_components.wav2feat import Wav2Feat
from agnet.core.atomic_components.cfg import parse_cfg, print_cfg

from service.object_models import EventObject, RenderEmotionObject, RenderAnimationObject
from service.progress.timing import TimingTracker
from config import Config


"""
avatar_registrar_cfg:
    insightface_det_cfg,
    landmark106_cfg,
    landmark203_cfg,
    landmark478_cfg,
    appearance_extractor_cfg,
    motion_extractor_cfg,

condition_handler_cfg:
    use_emo=True,
    use_sc=True,
    use_eye_open=True,
    use_eye_ball=True,
    seq_frames=80,

wav2feat_cfg:
    w2f_cfg, 
    w2f_type
"""


class StreamSDK:
    def __init__(self, cfg_pkl, data_root, **kwargs):

        [
            avatar_registrar_cfg,
            condition_handler_cfg,
            lmdm_cfg,
            stitch_network_cfg,
            warp_network_cfg,
            decoder_cfg,
            wav2feat_cfg,
            default_kwargs,
        ] = parse_cfg(cfg_pkl, data_root, kwargs)  # по факту kwrgs нужен для дебага, для норм работы все остальные берутся из cfg_pkl

        """
        Стандартный конфиг
        ==================== setup kwargs ====================
        online_mode <class 'bool'> True
        max_size <class 'int'> 1920
        template_n_frames <class 'int'> -1
        crop_scale <class 'float'> 2.3
        crop_vx_ratio <class 'int'> 0
        crop_vy_ratio <class 'float'> -0.125
        crop_flag_do_rot <class 'bool'> True
        smo_k_s <class 'int'> 13
        emo <class 'numpy.ndarray'> (600, 8)
        eye_f0_mode <class 'bool'> False
        ch_info <class 'dict'>
        overlap_v2 <class 'int'> 70
        fix_kp_cond <class 'int'> 1
        fix_kp_cond_dim <class 'list'> [0, 202]
        sampling_timesteps <class 'int'> 10
        v_min_max_for_clip <class 'numpy.ndarray'> (4, 265)
        smo_k_d <class 'int'> 3
        N_d <class 'int'> -1
        use_d_keys <class 'NoneType'> None
        relative_d <class 'bool'> True
        drive_eye <class 'NoneType'> None
        delta_eye_arr <class 'numpy.ndarray'> (15, 63)
        delta_eye_open_n <class 'int'> 0
        fade_type <class 'str'> d0
        fade_out_keys <class 'list'> ['exp']
        flag_stitching <class 'bool'> True
        overall_ctrl_info <class 'dict'> {'delta_pitch': 2}
        ==================================================
        """

        self.default_kwargs = default_kwargs

        t_loading = TimingTracker(enabled=True, log_each=False)
        t_loading.point("loading_start")
        with t_loading.measure("load_avatar_registrar"):
            self.avatar_registrar = AvatarRegistrar(**avatar_registrar_cfg)
        self.condition_handler = ConditionHandler(**condition_handler_cfg)
        with t_loading.measure("load_audio2motion"):
            self.audio2motion = Audio2Motion(lmdm_cfg)
        with t_loading.measure("load_motion_stitch"):
            self.motion_stitch = MotionStitch(stitch_network_cfg)
        with t_loading.measure("load_warp_f3d"):
            self.warp_f3d = WarpF3D(warp_network_cfg)
        with t_loading.measure("load_decode_f3d"):
            self.decode_f3d = DecodeF3D(decoder_cfg)
        self.putback = PutBack()

        with t_loading.measure("load_wav2feat"):
            self.wav2feat = Wav2Feat(**wav2feat_cfg)
        t_loading.point("loading_end")
        t_loading.delta("total", "loading_start", "loading_end")
        t_loading.log_summary(label="LOADING", include_points=False)

        self.seq_video_extractor = SequentialPyAVFrameExtractor()
        self.stop_event = threading.Event()

    def add_video_segment(self, video_segment_name: str):
        """
        Add video segment to buffer.
        """
        logger.debug(f"add video segment {video_segment_name[0]} into buffer({self.video_segment_buffer})")

        self.video_segment_buffer.append(video_segment_name)

    def clear_animations(self):
        """
        Clear all pending animations from the queue.
        After current animation finishes, will return to idle.
        """
        logger.info(f"Clearing animation queue. Current queue: {self.video_segment_buffer}")
        self.video_segment_buffer.clear()
        logger.info("Animation queue cleared")

    def add_emotion(self, emotion_name: str, gain: int = 10):
        """
        Add emotion into processing.
        """

        logger.info(f"add emotion {emotion_name} with gain {gain} into buffer({self.emotions_buffer})")

        self.emotions_buffer.append((emotion_name, gain))
        # if emotion_name in self.emotions_exp:
        #     self.next_emotion = emotion_name
        #     self.next_emotion_gain = gain
        #     # logger.info(f"set next emotion to {emotion_name} with gain {gain}")
        # else:
        #     raise ValueError(f"invalid emotion: {emotion_name}. Available: {list(self.emotions_exp.keys())}")

    def _merge_kwargs(self, default_kwargs, run_kwargs):
        for k, v in default_kwargs.items():
            if k not in run_kwargs:
                run_kwargs[k] = v
        return run_kwargs

    def setup_Nd(self, N_d, fade_in=-1, fade_out=-1, ctrl_info=None):
        # for eye open at video end
        self.motion_stitch.set_Nd(N_d)

        # for fade in/out alpha
        if ctrl_info is None:
            ctrl_info = self.ctrl_info
        if fade_in > 0:
            for i in range(fade_in):
                alpha = i / fade_in
                item = ctrl_info.get(i, {})
                item["fade_alpha"] = alpha
                ctrl_info[i] = item
        if fade_out > 0:
            ss = N_d - fade_out - 1
            ee = N_d - 1
            for i in range(ss, N_d):
                alpha = max((ee - i) / (ee - ss), 0)
                item = ctrl_info.get(i, {})
                item["fade_alpha"] = alpha
                ctrl_info[i] = item
        self.ctrl_info = ctrl_info

    def setup(self, source_path, output_path, **kwargs):  # SOURCE_PATH: BYTES  <---------------------------------------
        # ======== Create Timing Tracker ========
        self.timing = TimingTracker(enabled=True)
        self.timing.point("setup_start")

        # ======== Prepare Options ========
        kwargs = self._merge_kwargs(self.default_kwargs, kwargs)
        if os.getenv("LOG_DUMP_KWARGS") == "1":
            print("=" * 20, "setup kwargs", "=" * 20)
            print_cfg(**kwargs)
            print("=" * 50)

        # -- avatar_registrar: template cfg --
        self.max_size = kwargs.get("max_size", 1920)
        self.template_n_frames = kwargs.get("template_n_frames", -1)

        # -- avatar_registrar: crop cfg --
        self.crop_scale = kwargs.get("crop_scale", 2.3)
        self.crop_vx_ratio = kwargs.get("crop_vx_ratio", 0)
        self.crop_vy_ratio = kwargs.get("crop_vy_ratio", -0.125)
        self.crop_flag_do_rot = kwargs.get("crop_flag_do_rot", True)

        # -- avatar_registrar: smooth for video --
        self.smo_k_s = kwargs.get('smo_k_s', 13)

        # -- condition_handler: ECS --
        self.emo = kwargs.get("emo", 4)    # int | [int] | [[int]] | numpy
        self.eye_f0_mode = kwargs.get("eye_f0_mode", False)    # for video
        self.ch_info = kwargs.get("ch_info", None)    # dict of np.ndarray

        # -- audio2motion: setup --
        self.overlap_v2 = kwargs.get("overlap_v2", 10)
        self.fix_kp_cond = kwargs.get("fix_kp_cond", 0)
        self.fix_kp_cond_dim = kwargs.get("fix_kp_cond_dim", None)  # [ds,de]
        self.sampling_timesteps = kwargs.get("sampling_timesteps", 50)
        self.online_mode = kwargs.get("online_mode", False)
        self.v_min_max_for_clip = kwargs.get('v_min_max_for_clip', None)
        self.smo_k_d = kwargs.get("smo_k_d", 3)

        # -- motion_stitch: setup --
        self.N_d = kwargs.get("N_d", -1)
        self.use_d_keys = kwargs.get("use_d_keys", {"exp": 1.0, "pitch": 0.3, "yaw": 0.3, "roll": 0.3, "t": 1})
        self.relative_d = kwargs.get("relative_d", True)
        self.drive_eye = kwargs.get("drive_eye", None)    # None: true4image, false4video
        self.delta_eye_arr = kwargs.get("delta_eye_arr", None)
        self.delta_eye_arr = np.zeros_like(self.delta_eye_arr)
        self.delta_eye_open_n = kwargs.get("delta_eye_open_n", 0)
        self.fade_type = kwargs.get("fade_type", "")    # "" | "d0" | "s"
        self.fade_out_keys = kwargs.get("fade_out_keys", ("exp",))
        self.flag_stitching = kwargs.get("flag_stitching", True)

        self.ctrl_info = kwargs.get("ctrl_info", dict())
        self.ctrl_kwargs = kwargs.get("ctrl_kwargs", dict())
        self.overall_ctrl_info = kwargs.get("overall_ctrl_info", dict())
        """
        ctrl_info: list or dict
            {
                fid: ctrl_kwargs
            }

            ctrl_kwargs (see motion_stitch.py):
                fade_alpha
                fade_out_keys

                delta_pitch
                delta_yaw
                delta_roll
        """

        # only hubert support online mode
        assert self.wav2feat.support_streaming or not self.online_mode
        # ======== Register Avatar ========
        crop_kwargs = {
            "crop_scale": self.crop_scale,
            "crop_vx_ratio": self.crop_vx_ratio,
            "crop_vy_ratio": self.crop_vy_ratio,
            "crop_flag_do_rot": self.crop_flag_do_rot,
        }
        n_frames = self.template_n_frames if self.template_n_frames > 0 else self.N_d  # ? -1 картинка, больше - видос
        # logger.info(f"source_path: {source_path}"
        # if type(source_path) == bytes:
        #     source_info = self.avatar_registrar(
        #         source_path,
        #         max_dim=self.max_size,
        #         n_frames=n_frames,
        #         **crop_kwargs,
        #     )
        # else:
        #     # if kwargs.get("preprocess", False):
        #     #     source_info = self.avatar_registrar(
        #     #         source_path,
        #     #         max_dim=self.max_size,
        #     #         n_frames=n_frames,
        #     #         **crop_kwargs,
        #     #     )
        #     #     with open(Path(source_path).with_suffix(".pickle"), 'wb') as f:
        #     #         pickle.dump(source_info, f)
        #     #     return
        #     # else:
        #     # if os.path.isfile(Path(source_path).with_suffix(".pickle")):
        #     #     with open(Path(source_path).with_suffix(".pickle"), 'rb') as f:
        #     #         source_info = pickle.load(f)
        #     # else:
        source_info = self.avatar_registrar(
            source_path,
            max_dim=self.max_size,
            n_frames=n_frames,
            version_name=kwargs.get("version_name", None),
            **crop_kwargs,
        )
        self.timing.point("avatar_loaded")

        if len(source_info["x_s_info_lst"]) > 1 and self.smo_k_s > 1:
            source_info["x_s_info_lst"] = smooth_x_s_info_lst(source_info["x_s_info_lst"], smo_k=self.smo_k_s)

        self.source_info = source_info

        self.si_img_rgb_list = self.source_info["img_rgb_lst"]
        self.si_M_c2o_lst = self.source_info["M_c2o_lst"]
        self.si_f_s_lst = self.source_info["f_s_lst"]
        self.si_x_s_info_lst = self.source_info["x_s_info_lst"]

        self.source_info_frames = len(source_info["x_s_info_lst"])

        # ======== Setup Condition Handler ========
        self.condition_handler.setup(source_info, self.emo, eye_f0_mode=self.eye_f0_mode, ch_info=self.ch_info)  # Доп условия (эмоции, глаза и т.д.)

        # ======== Setup Audio2Motion (LMDM) ========
        x_s_info_0 = self.condition_handler.x_s_info_0
        self.audio2motion.setup(
            x_s_info_0,
            overlap_v2=self.overlap_v2,
            fix_kp_cond=self.fix_kp_cond,
            fix_kp_cond_dim=self.fix_kp_cond_dim,
            sampling_timesteps=self.sampling_timesteps,
            online_mode=self.online_mode,
            v_min_max_for_clip=self.v_min_max_for_clip,
            smo_k_d=self.smo_k_d,
        )

        # ======== Setup Motion Stitch ========
        is_image_flag = source_info["is_image_flag"]
        x_s_info = source_info['x_s_info_lst'][0]
        fix_exp_a1_alpha = kwargs.get("fix_exp_a1_alpha", None)
        self.motion_stitch.setup(
            N_d=self.N_d,
            use_d_keys=self.use_d_keys,
            relative_d=self.relative_d,
            drive_eye=self.drive_eye,
            delta_eye_arr=self.delta_eye_arr,
            delta_eye_open_n=self.delta_eye_open_n,
            fade_out_keys=self.fade_out_keys,
            fade_type=self.fade_type,
            flag_stitching=self.flag_stitching,
            is_image_flag=is_image_flag,
            x_s_info=x_s_info,
            d0=None,
            ch_info=self.ch_info,
            overall_ctrl_info=self.overall_ctrl_info,
            fix_exp_a1_alpha=fix_exp_a1_alpha
        )

        del source_info
        del self.source_info

        # ======== Video Writer ========
        # self.output_path = output_path
        # self.tmp_output_path = output_path + ".tmp.mp4"
        # self.writer = VideoWriterByImageIO(self.tmp_output_path)
        # self.writer_pbar = tqdm(desc="writer")

        # ======== Audio Feat Buffer ========
        if self.online_mode:
            # buffer: seq_frames - valid_clip_len
            self.audio_feat = self.wav2feat.wav2feat(np.zeros((self.overlap_v2 * 640,), dtype=np.float32), sr=16000)
            assert len(self.audio_feat) == self.overlap_v2, f"{len(self.audio_feat)}"
        else:
            self.audio_feat = np.zeros((0, self.wav2feat.feat_dim), dtype=np.float32)
        self.cond_idx_start = 0 - len(self.audio_feat)

        # ======== Setup Worker Threads ========
        QUEUE_MAX_SIZE = kwargs.get("QUEUE_MAX_SIZE", 1)   # Максимальный размер очереди обработчиков чанков
        MS_MAX_SIZE = kwargs.get("MS_MAX_SIZE", 3)
        A2M_MAX_SIZE = kwargs.get("A2M_MAX_SIZE", 5)
        # self.QUEUE_TIMEOUT = None

        self.worker_exception = None

        self.audio2motion_queue = queue.Queue(maxsize=A2M_MAX_SIZE)
        self.motion_stitch_queue = queue.Queue(maxsize=MS_MAX_SIZE)
        self.warp_f3d_queue = queue.Queue(maxsize=QUEUE_MAX_SIZE)
        self.decode_f3d_queue = queue.Queue(maxsize=QUEUE_MAX_SIZE)
        self.putback_queue = queue.Queue(maxsize=QUEUE_MAX_SIZE)
        self.writer_queue = queue.Queue(maxsize=QUEUE_MAX_SIZE)

        self.queue_list = [
            self.audio2motion_queue,
            self.motion_stitch_queue,
            self.warp_f3d_queue,
            self.decode_f3d_queue,
            self.putback_queue,
            self.writer_queue
        ]

        self.thread_list = [
            threading.Thread(target=self.audio2motion_worker),
            threading.Thread(target=self.motion_stitch_worker),
            threading.Thread(target=self.warp_f3d_worker),
            threading.Thread(target=self.decode_f3d_worker),
            threading.Thread(target=self.putback_worker),
            # threading.Thread(target=self.writer_worker),     <--------------------------------------------------------
        ]
        self.first_chunk = True
        self._inference_start_logged = False
        for thread in self.thread_list:
            thread.start()
        self.timing.point("threads_started")

        logger.info("------------------------------ ALL THREADS STARTED ------------------------------")

        video_segments_path = kwargs.get("video_segments_path", None)
        self.video_exists = False
        if video_segments_path:
            self.idle_name = kwargs.get("idle_name", "idle")
            self.seq_video_extractor._initialize_video(source_path)
            self.video_exists = True
            self.setup_video_segments(video_segments_path)

        emotions_info_path = kwargs.get("emotions_path", None)
        self.emotion_exists = False
        if emotions_info_path and os.path.exists(os.path.join(emotions_info_path, "info.json")):
            self.emotion_exists = True
            self.setup_emotions(kwargs['emotions_path'])

        logger.info(f"ANIMATIONS: {self.video_exists}")
        logger.info(f"EMOTIONS: {self.emotion_exists}")
        self.timing.point("setup_end")

    def interrupt(self):
        """
        Soft interrupt: force mouth to close without dropping frames.
        Sets force_silence flag to override is_voice for all frames.
        """
        logger.info("INTERRUPT: enabling force_silence mode")
        self.force_silence = True

    def setup_emotions(self, emotions_path: str):
        emotions_info_path = f"{emotions_path}/info.json"
        logger.info(f"setup emotions from {emotions_info_path}")
        with open(emotions_info_path, 'r') as fp:
            self.emotions_info = json.load(fp)

        self.emotions_exp = {}

        for name in self.emotions_info:
            exp_path = self.emotions_info[name]['path']
            exp = np.load(f"{emotions_path}/{exp_path}")

            # zero head movements
            kp_head_mask = np.isin(np.arange(0, 21), [0, 3, 4, 5, 7, 8, 9, 10])

            exp = exp.reshape(-1, 1, 21, 3)
            exp[:, :, kp_head_mask] *= 0
            exp = exp.reshape(-1, 1, 63)

            emo_exps = exp[30:-30]
            emo_exps_0 = exp[0]

            emo_exps_0 = emo_exps_0.reshape(-1, 1, 21, 3)
            emo_exps_0[:, :, kp_head_mask] *= 0
            emo_exps_0 = emo_exps_0.reshape(-1, 1, 63)

            # make emotion cyclically consistent
            emo_exps -= emo_exps_0[0]

            emo_exps = np.concatenate([emo_exps, emo_exps[::-1]])

            self.emotions_exp[name] = emo_exps

        self.emotions_exp['none'] = np.zeros([1, 1, 63])

        self.emotions_buffer = []
        self.emo_gain = 0
        self.current_emotion_complete = False
        self.emo_gain_max = 10

        # self.current_emotion = 'none'
        self.emo_frame_idx = 0


    def setup_video_segments(self, video_segments_path: str):
        """
        Read video segments from json file information.
        """

        self.video_segment_info = None

        if video_segments_path:
            with open(video_segments_path, 'r') as fp:
                self.video_segment_info = json.load(fp)

        self.video_segment_buffer = []
        self.video_segment_current = self.idle_name
        self.video_segment_auto_idle = True
        self.video_segment_previous = ""

        # Soft interrupt: force silence without clearing queues
        self.force_silence = False
        # Per-frame muted state queue, filled by _motion_stitch_worker, consumed by stream_frames
        self.muted_frames_queue = queue.Queue()

        # Handle global gen frame index
        self.gen_frame_idx = self.video_segment_info[self.idle_name]["start"]
        self.seq_video_extractor.seek_to_frame(self.gen_frame_idx)

        print(f'loaded video segments {self.video_segment_info}')

    def stream_frames(self):
        while not self.stop_event.is_set():
            try:
                item = self.writer_queue.get(timeout=1)
                if item is not None:
                    yield item
            except queue.Empty:
                # TRACE: heartbeat every 1s while waiting - spam during normal operation
                logger.trace("[EVENT] processes_running")
                continue
            except Exception as e:
                self.worker_exception = e
                self.stop_event.set()
                break

            if item is None:
                logger.info("PROCESSES STOPPED")
                break

    def _get_ctrl_info(self, fid):
        try:
            if isinstance(self.ctrl_info, dict):
                return self.ctrl_info.get(fid, {})
            elif isinstance(self.ctrl_info, list):
                return self.ctrl_info[fid]
            else:
                return {}
        except Exception as e:
            traceback.print_exc()
            return {}

    # def writer_worker(self):
    #     try:
    #         self._writer_worker()
    #     except Exception as e:
    #         self.worker_exception = e
    #         self.stop_event.set()
    #
    # def _writer_worker(self):
    #     while not self.stop_event.is_set():
    #         try:
    #             item = self.writer_queue.get(timeout=1)
    #         except queue.Empty:
    #             continue
    #
    #         if item is None:
    #             break
    #         res_frame_rgb = item
    #         self.writer(res_frame_rgb, fmt="rgb")
    #         self.writer_pbar.update()

    def putback_worker(self):
        try:
            self._putback_worker()
        except Exception as e:
            logger.error(f"STREAM_PIPELINE_ONLINE putback_worker: {str(e)}")
            self.worker_exception = e
            self.stop_event.set()

    def _putback_worker(self):
        # pb_res_time_list = []
        # first_flag = True
        while not self.stop_event.is_set():
            try:
                item = self.putback_queue.get(timeout=1)
                if isinstance(item, EventObject):
                    self.writer_queue.put(item)
                    continue
                # if first_flag:
                #     pb_start_time = time.perf_counter()
                #     first_flag = False
            except queue.Empty:
                continue
            if item is None:
                self.writer_queue.put(None)
                # logger.info(pb_res_time_list)
                break
            frame_idx, render_img, frame_pb, is_alpha = item
            # frame_rgb = self.si_img_rgb_list[frame_idx]
            if self.video_exists:
                frame_rgb = frame_pb
            else:
                frame_rgb = self.si_img_rgb_list[frame_idx]
            M_c2o = self.si_M_c2o_lst[frame_idx]
            logger.trace(f"FRAME_PB IN PB WORKER: {type(frame_pb)}")
            res_frame_rgb = self.putback(frame_rgb, render_img, M_c2o)
            self.writer_queue.put(res_frame_rgb)
            # pb_res_time_list.append(time.perf_counter() - self.union_start)

    def decode_f3d_worker(self):
        try:
            self._decode_f3d_worker()
        except Exception as e:
            logger.error(f"STREAM_PIPELINE_ONLINE decode_f3d_worker: {str(e)}")
            self.worker_exception = e
            self.stop_event.set()

    def _decode_f3d_worker(self):
        # df3d_res_time_list = []
        # first_flag = True
        while not self.stop_event.is_set():
            try:
                item = self.decode_f3d_queue.get(timeout=1)
                if isinstance(item, EventObject):
                    self.putback_queue.put(item)
                    continue
                # if first_flag:
                #     df3d_start_time = time.perf_counter()
                #     first_flag = False
            except queue.Empty:
                continue
            if item is None:
                self.putback_queue.put(None)
                # logger.info(df3d_res_time_list)
                break
            frame_idx, f_3d, frame_pb, is_alpha = item
            # TODO: clean old commented logs
            # start = time.perf_counter()
            # logger.info("------------------------ DECODOE F3D START ------------------------")
            with self.timing.measure("inference_decode_f3d"):
                render_img = self.decode_f3d(f_3d)
            # end = time.perf_counter()
            # logger.info(f"------------------------ DECODOE F3D END {end-start} ------------------------")
            # logger.info("PUTBACK QUEUE PUT")
            self.timing.point_once("first_decode_f3d_out")
            self.putback_queue.put([frame_idx, render_img, frame_pb, is_alpha])
            # df3d_res_time_list.append(time.perf_counter() - self.union_start)

    def warp_f3d_worker(self):
        try:
            self._warp_f3d_worker()
        except Exception as e:
            logger.error(f"STREAM_PIPELINE_ONLINE warp_f3d_worker: {str(e)}")
            self.worker_exception = e
            self.stop_event.set()

    def _warp_f3d_worker(self):
        # wf3d_res_time_list = []
        # first_flag = True
        while not self.stop_event.is_set():
            try:
                item = self.warp_f3d_queue.get(timeout=1)
                if isinstance(item, EventObject):
                    self.decode_f3d_queue.put(item)
                    continue
                # if first_flag:
                #     wf3d_start_time = time.perf_counter()
                #     first_flag = False
            except queue.Empty:
                continue
            if item is None:
                self.decode_f3d_queue.put(None)
                # logger.info(wf3d_res_time_list)
                break
            frame_idx, x_s, x_d, frame_pb, is_alpha = item
            f_s = self.si_f_s_lst[frame_idx]
            if self.video_exists:
                f_s = f_s.to(torch.float32).numpy()
            # TODO: clean old commented logs
            # start = time.perf_counter()
            # logger.info("---------------- WARP F3D START ----------------")
            with self.timing.measure("inference_warp_f3d"):
                f_3d = self.warp_f3d(f_s, x_s, x_d)
            # end = time.perf_counter()
            # logger.info(f"---------------- WARP F3D END {end-start} ----------------")
            # logger.info("DECODE F3D QUEUE PUT")
            self.timing.point_once("first_warp_f3d_out")
            self.decode_f3d_queue.put([frame_idx, f_3d, frame_pb, is_alpha])
            # wf3d_res_time_list.append(time.perf_counter() - self.union_start)

    def motion_stitch_worker(self):
        try:
            self._motion_stitch_worker()
        except Exception as e:
            logger.error(f"STREAM_PIPELINE_ONLINE motion_stitch_worker: {str(e)}")
            self.worker_exception = e
            self.stop_event.set()

    def _motion_stitch_worker(self):
        # ms_res_time_list = []
        # first_flag = True
        while not self.stop_event.is_set():
            try:
                item = self.motion_stitch_queue.get(timeout=1)
                if isinstance(item, EventObject):
                    self.warp_f3d_queue.put(item)
                    continue
                elif isinstance(item, RenderEmotionObject):
                    self.add_emotion(emotion_name=item.render_data)
                    continue
                # if first_flag:
                #     ms_start_time = time.perf_counter()
                #     first_flag = False
            except queue.Empty:
                continue
            if item is None:
                self.warp_f3d_queue.put(None)
                # logger.info(ms_res_time_list)
                break
            item, is_voice = item
            # Короче x_s - Motion Extractor, f_s - Appearance Extractor

            frame_idx, x_d_info, ctrl_kwargs, frame_pb, vad = item  # Получаем данные из audio2motion (x_d_info - кадр-кейпоинт)
            if self.video_exists:
                ctrl_kwargs['is_voice'] = is_voice
                if vad:
                    ctrl_kwargs["is_voice"] = False

            # Force silence on interrupt: override is_voice and use softer silence params
            is_muted = self.force_silence
            if self.force_silence:
                ctrl_kwargs["is_voice"] = False
                ctrl_kwargs["silence_threshold"] = 0  # Start immediately
                ctrl_kwargs["silence_transition_frames"] = 10  # Smooth 400ms transition
                ctrl_kwargs["silence_alpha"] = 0.0  # Full mute

            # Record per-frame muted state for stream_frames_thread
            self.muted_frames_queue.put(is_muted)

            x_s_info = self.si_x_s_info_lst[frame_idx]  # Данные по картинке - ? motion extractor по кадру (? кадр 1 для картинки
            if not self.emotion_exists:
                with self.timing.measure("inference_motion_stitch"):
                    x_s, x_d, is_alpha = self.motion_stitch(x_s_info, x_d_info, **ctrl_kwargs)
            else:
                # TODO: clean old commented logs
                # start = time.perf_counter()
                # logger.info("-------- MOTION STITCH START --------")
                # logger.info(f"{self.current_emotion}, {self.next_emotion}")

                # ------------------- Manage Emotions -------------------

                if len(self.emotions_buffer) > 0 and self.emo_gain == 0:
                    self.current_emotion, self.current_emotion_gain = self.emotions_buffer.pop(0)
                    self.warp_f3d_queue.put(EventObject(event_name="emotion", event_data={"name": self.current_emotion}))
                elif len(self.emotions_buffer) == 0 and self.emo_gain == 0:
                    # self.warp_f3d_queue.put(EventObject(event_name="emotion", event_data={"name": "idle"}))
                    self.current_emotion = None

                if self.current_emotion is not None and not self.current_emotion_complete:
                    if self.current_emotion != self.idle_name:
                        x_exp_emo = self.emotions_exp[self.current_emotion][self.emo_frame_idx]
                        self.emo_frame_idx = (self.emo_frame_idx + 1) % self.emotions_exp[self.current_emotion].shape[0]
                        x_exp_emo *= min(self.current_emotion_gain / self.emo_gain_max, self.emo_gain / self.emo_gain_max)
                        if self.emo_gain < self.emo_gain_max:
                            self.emo_gain += 1
                        else:
                            self.current_emotion_complete = True
                elif self.current_emotion is not None and self.current_emotion_complete:
                    x_exp_emo = self.emotions_exp[self.current_emotion][self.emo_frame_idx]
                    self.emo_frame_idx = (self.emo_frame_idx + 1) % self.emotions_exp[self.current_emotion].shape[0]
                    x_exp_emo *= min(self.current_emotion_gain / self.emo_gain_max, self.emo_gain / self.emo_gain_max)
                    if len(self.emotions_buffer) > 0:
                        self.emo_gain -= 1
                        if self.emo_gain == 0:
                            self.current_emotion_complete = False
                elif self.current_emotion is None:
                    x_exp_emo = 0

                # -------------------------------------------------------
                with self.timing.measure("inference_motion_stitch"):
                    x_s, x_d, is_alpha = self.motion_stitch(x_s_info, x_d_info, x_exp_emo=x_exp_emo, **ctrl_kwargs)

            # end = time.perf_counter()
            # logger.info(f"-------- MOTION STITCH END {end-start} --------")
            # logger.info("WARP F3D QUEUE PUT")
            self.timing.point_once("first_motion_stitch_out")
            self.warp_f3d_queue.put([frame_idx, x_s, x_d, frame_pb, is_alpha])
            # ms_res_time_list.append(time.perf_counter() - self.union_start)

    def audio2motion_worker(self):
        try:
            self._audio2motion_worker()
        except Exception as e:
            logger.error(f"STREAM_PIPELINE_ONLINE audio2motion_worker: {str(e)}")
            self.worker_exception = e
            self.stop_event.set()

    def _audio2motion_worker(self):  # Чанки внутри - audio_features
        is_end = False
        seq_frames = self.audio2motion.seq_frames  # 80 поум
        valid_clip_len = self.audio2motion.valid_clip_len  # 10
        aud_feat_dim = self.wav2feat.feat_dim  # 1024 поум
        item_buffer = np.zeros((0, aud_feat_dim), dtype=np.float32)

        res_kp_seq = None
        res_kp_seq_valid_start = None if self.online_mode else 0  # None

        global_idx = 0   # frame idx, for template
        local_idx = 0    # for cur audio_feat
        gen_frame_idx = 0
        # a2m_res_time_list = []
        # first_flag = True
        vad = False
        while not self.stop_event.is_set():
            try:
                item = self.audio2motion_queue.get(timeout=1)  # audio feat
            except queue.Empty:
                continue
            if item is None:
                is_end = True
            elif isinstance(item, RenderEmotionObject):
                self.motion_stitch_queue.put(item)
                continue
            elif isinstance(item, RenderAnimationObject):
                self.add_video_segment(video_segment_name=item.render_data)
                continue
            else:
                item, is_voice = item
                item_buffer = np.concatenate([item_buffer, item], 0)

            if not is_end and item_buffer.shape[0] < valid_clip_len:  # 10
                # wait at least valid_clip_len new item - Буфер в 10
                continue
            else:
                self.audio_feat = np.concatenate([self.audio_feat, item_buffer], 0)  # Сливаем текущий чанк и буфер
                item_buffer = np.zeros((0, aud_feat_dim), dtype=np.float32)  # обнуляем буфер

            while True:
                aud_feat = self.audio_feat[local_idx: local_idx+seq_frames]  # Вырезаем длину seq_frames=10 поум
                real_valid_len = valid_clip_len
                if len(aud_feat) == 0:
                    break
                elif len(aud_feat) < seq_frames:  # обработка последнего чанка
                    if not is_end:
                        break
                    else:
                        # final clip: pad to seq_frames
                        real_valid_len = len(aud_feat)  # обновляем длину !!! ТЕПЕРЬ 10
                        pad = np.stack([aud_feat[-1]] * (seq_frames - len(aud_feat)), 0)  # массив из последнего элемента
                        aud_feat = np.concatenate([aud_feat, pad], 0)  # Теперь у нас [a, b, c, c, c, c, ...]
                # if first_flag:
                #     a2m_start_time = time.perf_counter()
                #     first_flag = False
                aud_cond = self.condition_handler(aud_feat, global_idx + self.cond_idx_start)[None]  # берем условия если есть
                with self.timing.measure("inference_audio2motion"):
                    res_kp_seq = self.audio2motion(aud_cond, res_kp_seq)  # предсказываем кейпоинты по условиям из аудио и предыдущему чанку
                if res_kp_seq_valid_start is None:  # Отрабатывает первый раз
                    # online mode, first chunk
                    res_kp_seq_valid_start = res_kp_seq.shape[1] - self.audio2motion.fuse_length  # 80 - 10 = 70
                    d0 = self.audio2motion.cvt_fmt(res_kp_seq[0:1])[0]
                    self.motion_stitch.d0 = d0

                    local_idx += real_valid_len
                    global_idx += real_valid_len
                    continue
                else:
                    valid_res_kp_seq = res_kp_seq[:, res_kp_seq_valid_start: res_kp_seq_valid_start + real_valid_len]  # ? Кадры-кейпоинты не из области слияния
                    x_d_info_list = self.audio2motion.cvt_fmt(valid_res_kp_seq)  # Список кадров-кейпоинтов формата [{np[]}, ...]
                    for x_d_info in x_d_info_list:
                        if self.video_exists:
                        # ------------------- Manage Video Segments -------------------
                            video_segment = self.video_segment_info[self.video_segment_current]
                            if self.gen_frame_idx >= video_segment["end"]:
                                # if current video segment is ended, switch to idle
                                if len(self.video_segment_buffer) > 0:
                                    new_video_segment = self.video_segment_buffer.pop(0)
                                    self.video_segment_current = new_video_segment[0]
                                    self.video_segment_auto_idle = new_video_segment[1]
                                    self.gen_frame_idx = self.video_segment_info[self.video_segment_current]["start"]
                                    if "vad" in self.video_segment_info[self.video_segment_current]:
                                        vad = self.video_segment_info[self.video_segment_current]["vad"]
                                        logger.debug(f"ASSIGN VAD {vad} TO {self.video_segment_current}")
                                    else:
                                        vad = False
                                    self.seq_video_extractor.seek_to_frame(self.gen_frame_idx)
                                else:
                                    if self.video_segment_auto_idle:
                                        self.video_segment_current = self.idle_name  # тест анимация за анимацией
                                        logger.debug(f"SWITCH TO IDLE CAUSE AUTO IDLE {self.video_segment_auto_idle}")
                                    self.gen_frame_idx = self.video_segment_info[self.video_segment_current]["start"]
                                    self.seq_video_extractor.seek_to_frame(self.gen_frame_idx)
                                    vad = False
                            frame_pb = self.seq_video_extractor.get_next_frame()
                            frame_idx = _mirror_index(
                                self.gen_frame_idx,
                                self.video_segment_info[self.video_segment_current]["end"])
                            frame_idx = max(0, min(frame_idx, self.source_info_frames - 1))

                            if getattr(self, 'is_image_flag', False):
                                frame_idx = 0
                            ctrl_kwargs = {}
                        # -------------------------------------------------------------
                        else:
                            frame_pb = None
                            vad = False
                            frame_idx = _mirror_index(gen_frame_idx, self.source_info_frames)  # ? Индекс кадра
                            ctrl_kwargs = self._get_ctrl_info(gen_frame_idx)  # ? Управляющие параметры
                        logger.trace(f"FRAME_PB IN A2M WORKER: {type(frame_pb)}")
                        while not self.stop_event.is_set():
                            try:
                                if self.video_exists:
                                    if self.video_segment_current != self.video_segment_previous:
                                        # logger.info(self.video_segment_previous)
                                        # logger.info(self.video_segment_current)
                                        if self.video_segment_previous == "":
                                            self.motion_stitch_queue.put(EventObject(event_name="animation", event_data={
                                                "name": self.video_segment_current,
                                                "event": 1
                                            }))
                                        else:
                                            self.motion_stitch_queue.put(EventObject(event_name="animation", event_data={
                                                "name": self.video_segment_previous,
                                                "event": 0
                                            }))
                                            self.motion_stitch_queue.put(EventObject(event_name="animation", event_data={
                                                "name": self.video_segment_current,
                                                "event": 1
                                            }))
                                self.motion_stitch_queue.put(([frame_idx, x_d_info, self.ctrl_kwargs, frame_pb, vad], is_voice))  # Кладем в очередь обработчика
                                self.timing.point_once("first_audio2motion_out")
                                if self.video_exists:
                                    self.video_segment_previous = self.video_segment_current
                                break
                            except queue.Full:
                                continue

                        if self.video_exists:
                            self.gen_frame_idx += 1
                        else:
                            gen_frame_idx += 1

                    res_kp_seq_valid_start += real_valid_len

                    local_idx += real_valid_len
                    global_idx += real_valid_len

                L = res_kp_seq.shape[1]
                if L > seq_frames * 2:  # ? Очистка массивов
                    cut_L = L - seq_frames * 2
                    res_kp_seq = res_kp_seq[:, cut_L:]
                    res_kp_seq_valid_start -= cut_L

                if local_idx >= len(self.audio_feat):
                    break

            L = len(self.audio_feat)
            if L > seq_frames * 2:  # ? Очистка массивов
                cut_L = L - seq_frames * 2
                self.audio_feat = self.audio_feat[cut_L:]
                local_idx -= cut_L

            if is_end:
                break
        if self.video_exists:
            self.motion_stitch_queue.put(EventObject(event_name="animation", event_data={
                "name": self.video_segment_current,
                "event": 0
            }))
        self.motion_stitch_queue.put(None)
        # logger.info(a2m_res_time_list)

    def close(self):
        # flush frames
        self.audio2motion_queue.put(None)
        # Wait for worker threads to finish
        for thread in self.thread_list:
            thread.join()
        self.seq_video_extractor.close()
        logger.info("PROCESSES CLOSED")

        # Log timing summary before cleanup
        self.timing.log_summary(label="INFER")

        # try:
        #     self.writer.close()
        #     self.writer_pbar.close()
        # except:
        #     traceback.print_exc()
        # Check if any worker encountered an exception
        if self.worker_exception is not None:
            raise self.worker_exception

    def run_chunk(self, audio_chunk, chunksize=(3, 5, 2), is_voice: bool = True):
        # only for hubert
        # is_voice = False
        # audio_chunk = np.zeros_like(audio_chunk)
        if not self._inference_start_logged:
            logger.info("[EVENT] inference_start")
            self._inference_start_logged = True
        self.timing.point_once("first_wav2feat_enter")
        if isinstance(audio_chunk, RenderEmotionObject) or isinstance(audio_chunk, RenderAnimationObject):
            self.audio2motion_queue.put(audio_chunk)
        else:
            with self.timing.measure("inference_wav2feat"):
                aud_feat = self.wav2feat(audio_chunk, chunksize=chunksize)  # aud_feat - audio features через HuBERT
            while not self.stop_event.is_set():
                try:
                    if self.first_chunk:
                        self.union_start = time.perf_counter()
                        self.first_chunk = False
                    self.audio2motion_queue.put((aud_feat, is_voice), timeout=1)
                    self.timing.point_once("first_wav2feat_out")
                    break
                except queue.Full:
                    continue
