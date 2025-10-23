import numpy as np
import io
import librosa
import wave
import json
import time

from config import Config
from loguru import logger

from ditto.stream_pipeline_online import StreamSDK as onlineSDK, EventObject, RenderEmotionObject, RenderAnimationObject
from ditto.stream_pipeline_offline import StreamSDK as offlineSDK


class RenderService:
	def __init__(self, is_online: bool = False, sampling_timestamps: int = 0):
		self.is_online = is_online
		self.sampling_timestamps = sampling_timestamps
		cfg_pkl = f"{Config.WEIGHTS_PATH}/checkpoints/ditto_cfg/v0.4_hubert_cfg_trt_online.pkl"
		data_root = Config.DITTO_DATA_ROOT

		if is_online:
			self.sdk = onlineSDK(cfg_pkl, data_root)
			self.render_chunk = self.render_chunk_online
		else:
			self.sdk = onlineSDK(cfg_pkl, data_root)
			self.render_chunk = self.render_chunk_online

		self.bits_per_sample = 16
		self.num_channels = 1
		self.samples_per_sec = 16000
		self.chunk_duration = 2.0

		self.audio_buffer = np.zeros((3 * 640,), dtype=np.float32) if self.is_online else np.array([], dtype=np.float32)

		self.is_setup_nd = True
		self.timer_first_flag = True
		self.start_time = 0
		self.animation_to_play = []
		self.emotion_to_play = []

	def set_avatar(self, avatar_id: str):
		pass

	def play_animation(self, animation: str, auto_idle: bool):
		self.render_object(render_object=RenderAnimationObject(render_data=(animation, auto_idle)))
		# self.animation_to_play.append((animation, auto_idle))
		logger.info(f"animation got: {animation} auto idle: {auto_idle}")

	def set_emotion(self, emotion: str):
		self.render_object(render_object=RenderEmotionObject(render_data=emotion))
		# self.emotion_to_play.append(emotion)
		logger.info(f"emotion got: {emotion}")

	def handle_image(self, image_chunk):

		if self.sampling_timestamps != 0:
			sts = self.sampling_timestamps
		else:
			sts = int(Config.ONLINE_STREAMING_TIMESTAMPS) if self.is_online else int(Config.ONLINE_RENDER_TIMESTAMPS)

		args = {"online_mode": self.is_online,
		        "sampling_timesteps": sts,
		        "max_size": int(Config.MAX_SIZE),
		        "QUEUE_MAX_SIZE": int(Config.QUEUE_MAX_SIZE),
		        "MS_MAX_SIZE": int(Config.MS_MAX_SIZE),
		        "A2M_MAX_SIZE": int(Config.A2M_MAX_SIZE)}
		self.sdk.setup(source_path=image_chunk, output_path="", **args)

	def handle_video(self, video_path: str, video_info_path: str, ditto_config: dict, emotions_path: str = None):

		if self.sampling_timestamps != 0:
			sts = self.sampling_timestamps
		else:
			sts = int(Config.ONLINE_STREAMING_TIMESTAMPS) if self.is_online else int(Config.ONLINE_RENDER_TIMESTAMPS)



		args = {"online_mode": self.is_online,
				"video_segments_path": video_info_path,
				"emotions_path": emotions_path,
		        "sampling_timesteps": sts,
		        "max_size": int(Config.MAX_SIZE),
		        "QUEUE_MAX_SIZE": int(Config.QUEUE_MAX_SIZE),
		        "MS_MAX_SIZE": int(Config.MS_MAX_SIZE),
		        "A2M_MAX_SIZE": int(Config.A2M_MAX_SIZE)}
		logger.info(ditto_config)
		args.update(ditto_config)
		if sts == 5:
			args["sampling_timesteps"] = 5

		self.sdk.setup(source_path=video_path, output_path="", **args)

	def render_chunk_offline(self, audio_chunk, frame_rate: int, is_last: bool):
		if not is_last:
			logger.debug("START CHUNK RENDER")
			wav_buffer = io.BytesIO()
			with wave.open(wav_buffer, 'wb') as wav_file:
				wav_file.setnchannels(1)
				wav_file.setsampwidth(2)
				wav_file.setframerate(frame_rate)
				wav_file.writeframes(audio_chunk)
			wav_buffer.seek(0)

			audio, sr = librosa.load(wav_buffer, sr=16000)

			self.audio_buffer = np.append(self.audio_buffer, audio)
		else:
			aud_feat = self.sdk.wav2feat.wav2feat(self.audio_buffer)
			self.sdk.audio2motion_queue.put(aud_feat)

	def render_chunk_online(self, audio_chunk, frame_rate: int, is_last: bool, is_voice: bool = True):
		logger.debug("START CHUNK RENDER")
		if not is_last:
			logger.debug("IS NOT LAST")
			wav_buffer = io.BytesIO()
			with wave.open(wav_buffer, 'wb') as wav_file:
				wav_file.setnchannels(1)
				wav_file.setsampwidth(2)
				wav_file.setframerate(frame_rate)
				wav_file.writeframes(audio_chunk)
			wav_buffer.seek(0)

			audio, sr = librosa.load(wav_buffer, sr=16000)
			self.audio_buffer = np.append(self.audio_buffer, audio)

		if len(self.animation_to_play) > 0:
			video_segment_name = self.animation_to_play.pop(0)
			logger.info(f"add video segment {video_segment_name[0]}")
			self.sdk.add_video_segment(video_segment_name)

		if len(self.emotion_to_play) > 0:
			emotion = self.emotion_to_play.pop(0)
			logger.info(f"add emotion {emotion}")
			self.sdk.add_emotion(emotion)

		chunksize = (3, 5, 2)
		# audio = np.concatenate([np.zeros((chunksize[0] * 640,), dtype=np.float32), audio], 0)  # 1920 нулей, потом аудио?
		split_len = int(sum(chunksize) * 0.04 * 16000)  # 6400 - длина split_len
		buf_length = len(self.audio_buffer)
		idx = 0
		for i in range(0, buf_length, chunksize[1] * 640):  # от начала до конца с шагом 5 * 640 = 3200
			audio_chunk = self.audio_buffer[i:i + split_len]  # передавать кусок предыдущего чанка в следующий?
			if len(audio_chunk) < split_len:
				if is_last:
					logger.debug("IS LAST")
					audio_chunk = np.pad(audio_chunk, (0, split_len - len(audio_chunk)), mode="constant")  # с конца нули до длины split_len
					if self.timer_first_flag:
						self.start_time = time.perf_counter()
						self.timer_first_flag = False
					self.sdk.run_chunk(audio_chunk, chunksize, is_voice)
			else:
				if self.timer_first_flag:
					self.start_time = time.perf_counter()
					self.timer_first_flag = False
				idx = i + chunksize[1] * 640
				self.sdk.run_chunk(audio_chunk, chunksize, is_voice)
		self.audio_buffer = self.audio_buffer[idx::]

	def render_object(self, render_object):
		self.sdk.run_chunk(render_object)
