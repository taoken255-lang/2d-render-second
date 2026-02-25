from threading import Lock
import os

import imageio
import numpy as np
from loguru import logger


class FrameFlowControl:
	def __init__(self):
		self._lock = Lock()
		self._skip_frames = 0

	def request_skip(self, n: int):
		if n <= 0:
			return
		with self._lock:
			self._skip_frames += n

	def consume_skip(self) -> int:
		with self._lock:
			value = self._skip_frames
			self._skip_frames = 0
			return value


class RenderRequestRuntime:
	def __init__(self, output_format_ref):
		self.output_format_ref = output_format_ref
		self.flow_control = FrameFlowControl()
		self.loop_video_path = os.getenv("STREAM_LOOP_VIDEO", "").strip()
		self.loop_frames = []
		self.loop_pos = 0
		self.loop_width = None
		self.loop_height = None
		self.enabled = False
		self._load_loop_video()

	def _load_loop_video(self):
		if not self.loop_video_path:
			return

		try:
			reader = imageio.get_reader(self.loop_video_path)
			for frame in reader:
				arr = np.asarray(frame, dtype=np.uint8)
				if arr.ndim != 3 or arr.shape[2] < 3:
					continue
				self.loop_frames.append(arr[..., :3])
			reader.close()
			if not self.loop_frames:
				logger.warning(f"[LOOP_VIDEO] No valid frames in {self.loop_video_path}")
				return
			self.loop_height, self.loop_width = self.loop_frames[0].shape[:2]
			self.enabled = True
			logger.info(f"[LOOP_VIDEO] Loaded {len(self.loop_frames)} frames from {self.loop_video_path}")
		except Exception as e:
			logger.exception(f"[LOOP_VIDEO] Failed to load {self.loop_video_path}: {str(e)}")

	def _to_bgra(self, rgb: np.ndarray) -> bytes:
		bgra = np.empty((rgb.shape[0], rgb.shape[1], 4), dtype=np.uint8)
		bgra[..., 0] = rgb[..., 2]
		bgra[..., 1] = rgb[..., 1]
		bgra[..., 2] = rgb[..., 0]
		bgra[..., 3] = 255
		return bgra.tobytes()

	def _to_rgba(self, rgb: np.ndarray) -> bytes:
		alpha = np.full((rgb.shape[0], rgb.shape[1], 1), 255, dtype=np.uint8)
		rgba = np.concatenate((rgb, alpha), axis=2)
		return rgba.tobytes()

	def try_handle_skip_command(self, animation_name: str) -> bool:
		if not animation_name.startswith("__skip__:"):
			return False
		try:
			parts = animation_name.split(":", 1)
			skip_frames = int(parts[1])
			self.flow_control.request_skip(skip_frames)
			logger.info(f"[FRAME_CTRL] Requested skip={skip_frames}")
		except Exception:
			logger.warning(f"[FRAME_CTRL] Invalid skip command: {animation_name}")
		return True

	def consume_skip(self) -> int:
		return self.flow_control.consume_skip()

	def has_loop_video(self) -> bool:
		return self.enabled and len(self.loop_frames) > 0

	def next_loop_frame(self):
		if not self.has_loop_video():
			return None

		rgb = self.loop_frames[self.loop_pos]
		self.loop_pos = (self.loop_pos + 1) % len(self.loop_frames)
		fmt = self.output_format_ref.get()
		if fmt == "BGRA":
			data = self._to_bgra(rgb)
		elif fmt == "RGBA":
			data = self._to_rgba(rgb)
		else:
			data = rgb.tobytes()
		return data, self.loop_width, self.loop_height
