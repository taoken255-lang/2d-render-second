from multiprocessing import Queue
from loguru import logger
import numpy as np
import io
import librosa
import wave
import time

from ditto.stream_pipeline_online import StreamSDK as onlineSDK

from service.object_models import RenderAnimationObject, RenderEmotionObject, ErrorObject, ErrorDataType
from config import Config


class RenderService:
	def __init__(self, video_queue: Queue, is_online: bool = False, sampling_timestamps: int = 0):
		try:
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
		except Exception as e:
			logger.info(f"ERROR WHILE INITIALIZING {str(e)}")
			video_queue.put(ErrorObject(error_type=ErrorDataType.Initialization, error_message=f"Error during initialization of network: {str(e)}"))

	# os.kill(os.getpid(), signal.SIGTERM)

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

	def handle_image(self, image_chunk, video_queue: Queue):
		try:
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
		except Exception as e:
			logger.info(f"ERROR WHILE HANDLING IMAGE {str(e)}")
			video_queue.put(ErrorObject(error_type=ErrorDataType.Handling, error_message=f"Error during image initialization: {str(e)}"))

	def handle_video(
			self,
	        base_path: str,
	        avatar_name: str,
	        version_name: str,
	        emotions: bool,
	        ditto_config: dict,
	        video_queue: Queue,
	        idle_name: str = "idle"
	):
		try:
			if self.sampling_timestamps != 0:
				sts = self.sampling_timestamps
			else:
				sts = int(Config.ONLINE_STREAMING_TIMESTAMPS) if self.is_online else int(Config.ONLINE_RENDER_TIMESTAMPS)

			args = {"online_mode": self.is_online,
			        "video_segments_path": f"{base_path}/{avatar_name}.json",
			        "emotions_path": f"{base_path}/emotions/" if emotions else None,
			        "sampling_timesteps": sts,
			        "max_size": int(Config.MAX_SIZE),
			        "QUEUE_MAX_SIZE": int(Config.QUEUE_MAX_SIZE),
			        "MS_MAX_SIZE": int(Config.MS_MAX_SIZE),
			        "A2M_MAX_SIZE": int(Config.A2M_MAX_SIZE),
			        "idle_name": idle_name,
			        "version_name": version_name}
			logger.info(ditto_config)
			args.update(ditto_config)
			if sts == 5:
				args["sampling_timesteps"] = 5

			self.sdk.setup(source_path=f"{base_path}/{avatar_name}.mp4", output_path="", **args)
		except Exception as e:
			logger.info(f"ERROR WHILE HANDLING VIDEO {str(e)}")
			video_queue.put(ErrorObject(error_type=ErrorDataType.Handling, error_message=f"Error during video initialization {str(e)}"))

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
					audio_chunk = np.pad(audio_chunk, (0, split_len - len(audio_chunk)), mode="constant")
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
