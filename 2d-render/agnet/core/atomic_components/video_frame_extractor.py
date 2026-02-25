"""
ffmpeg -i assets/avatars/erm.mp4 \
  -c:v libx264 -preset slow -crf 14 -tune grain \
  -g 1 -keyint_min 1 -sc_threshold 0 -bf 0 \
  -pix_fmt yuv420p \
  assets/avatars/erm_alli_crf14.mp4
ffmpeg -i assets/avatars/erm.mp4 \
  -c:v libx264 -preset slow -crf 0 -tune grain \
  -g 1 -keyint_min 1 -sc_threshold 0 -bf 0 \
  -pix_fmt yuv420p \
  assets/avatars/erm_alli_lossless.mp4

"""


from typing import List, Optional
from loguru import logger
import av
import numpy as np


class SequentialPyAVFrameExtractor:
	def __init__(self):
		self.container = None
		self.video_stream = None
		self._frame_count = None
		self._fps = None
		self._width = None
		self._height = None

		self._current_position = 0
		self._decoder = None
		self._initialized = False
		self._current_frame = None
		self._frame_consumed = False

	def _initialize_video(self, video_path):
		self.container = av.open(video_path)

		if not self.container.streams.video:
			raise ValueError(f"no video data stream: {video_path}")

		self.video_stream = self.container.streams.video[0]
		self._frame_count = self.video_stream.frames
		self._fps = float(self.video_stream.average_rate)
		self._width = self.video_stream.width
		self._height = self.video_stream.height

		logger.info(f"[VIDEO] frames={self._frame_count} avatar_fps={self._fps}")
		try:
			codec_ctx = self.video_stream.codec_context
			codec_ctx.thread_count = 0
			codec_ctx.thread_type = "AUTO"
		except Exception:
			pass

	@property
	def frame_count(self) -> int:
		return self._frame_count

	@property
	def fps(self) -> float:
		return self._fps

	@property
	def dimensions(self) -> tuple:
		return (self._width, self._height)

	@property
	def current_position(self) -> int:
		"""Текущая позиция в видео."""
		return self._current_position

	def _pts_to_frame_index(self, pts: int) -> int:
		"""Конвертировать PTS в индекс кадра с учетом time_base и FPS."""
		if pts is None:
			return 0
		tb = self.video_stream.time_base
		return int((pts * tb) * self._fps)

	def _frame_index_to_pts(self, frame_index: int) -> int:
		"""Конвертировать индекс кадра в PTS (единицы time_base)."""
		tb = self.video_stream.time_base  # Fraction
		seconds = frame_index / self._fps
		pts = seconds / float(tb)
		return int(pts)

	def seek_to_frame(self, start_frame: int) -> bool:
		if start_frame < 0 or start_frame >= self._frame_count:
			return False

		try:
			target_pts = self._frame_index_to_pts(start_frame)
			self.container.seek(target_pts, stream=self.video_stream, any_frame=False, backward=True)
			self._decoder = self.container.decode(video=0)
			candidate_frame = None
			candidate_index = None
			for frame in self._decoder:
				idx = self._pts_to_frame_index(frame.pts)
				candidate_frame = frame
				candidate_index = idx
				if idx >= start_frame:
					break
			if candidate_frame is None or candidate_index is None:
				return False
			self._current_frame = candidate_frame
			self._frame_consumed = False
			self._current_position = candidate_index
			self._initialized = True
			return True
		except Exception as e:
			logger.error(f"frame {start_frame}: {e}")
			return False

	def get_next_frame(self) -> Optional[np.ndarray]:
		logger.trace(self._current_position)
		logger.trace(self._frame_count)
		if not self._initialized or self._current_position >= self._frame_count:
			return None

		try:
			if self._current_frame is not None and not self._frame_consumed:
				frame = self._current_frame
				self._frame_consumed = True
			else:
				frame = next(self._decoder)
			ndarray = frame.to_ndarray(format='rgb24')
			self._current_position += 1
			return ndarray
		except StopIteration as e:
			logger.error(f"STOPITERATION {e}")
			return None
		except Exception as e:
			logger.error(f"{e}")
			return None

	def get_frame_sequence(self, start_frame: int, count: int) -> List[np.ndarray]:
		frames = []

		if self.seek_to_frame(start_frame):
			for _ in range(count):
				frame = self.get_next_frame()
				if frame is not None:
					frames.append(frame)
				else:
					break

		return frames

	def reset_position(self):
		self._current_position = 0
		self._initialized = False
		self._current_frame = None
		self._frame_consumed = False
		if self._decoder:
			self._decoder = None

	def close(self):
		if self.container is not None:
			self.container.close()
			self.container = None
		self._decoder = None
		self._initialized = False

	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc_val, exc_tb):
		self.close()

	def __del__(self):
		self.close()


# if __name__ == "__main__":
# 	extr = SequentialPyAVFrameExtractor()
# 	extr._initialize_video("assets/avatars/erm_alli_crf14.mp4")
# 	extr.seek_to_frame(500)
# 	image = extr.get_next_frame()
# 	print(image)
