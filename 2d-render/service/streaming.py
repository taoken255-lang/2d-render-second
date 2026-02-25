from multiprocessing import Queue as mQueue, Process, Value
from threading import Event, Thread, Lock
from queue import Queue as tQueue
from grpc import RpcError
from config import Config
from loguru import logger
from uuid import uuid4
import numpy as np
import imageio
import signal
import time
import json
import sys
import os

from proto import render_service_pb2_grpc
from proto.render_service_pb2 import RenderResponse, VideoChunk, StartAnimation, EndAnimation, AvatarSet, InfoResponse, ItemList, RequestError, EmotionSet, AnimationsCleared

from service.render import RenderService
from service.object_models import IPCObject, IPCDataType, CommandObject, CommandDataType, AudioObject, ImageObject, EventObject, ErrorObject, ErrorDataType, InterruptObject
from service.progress import ProgressTracker
from service.progress.timing import TimingTracker
from config import Config

TARGET_FPS = 25

class SharedValue:
	def __init__(self, value):
		self._value = value
		self._lock = Lock()

	def set(self, value):
		with self._lock:
			self._value = value

	def get(self):
		with self._lock:
			return self._value


def clear_queue(q):
	try:
		while not q.empty():
			q.get_nowait()
	except Exception:
		return

#
# class InputObject:
#     def __init__(self, image: ImageObject = None, audio: AudioObject = None):
#         self.image = image
#         self.audio = audio


def get_avatars(folder_path="/app/assets/"):
	result = {}

	# проходим по папкам в folder_path
	for avatar_name in os.listdir(folder_path):
		avatar_dir = os.path.join(folder_path, avatar_name)
		if not os.path.isdir(avatar_dir):
			continue  # пропускаем файлы, если вдруг они есть

		animations = []
		emotions = []

		# путь к основному json (animations)
		last_version = get_latest_version_folder(base_path=avatar_dir)
		if last_version is not None:
			avatar_dir = f"{avatar_dir}/{last_version}/"
		
		avatar_json_path = os.path.join(avatar_dir, f"{avatar_name}.json")
		if os.path.exists(avatar_json_path):
			with open(avatar_json_path, "r", encoding="utf-8") as f:
				data = json.load(f)
				animations = list(data.keys())

		# путь к emotions/info.json
		emotions_dir = os.path.join(avatar_dir, "emotions")
		info_json_path = os.path.join(emotions_dir, "info.json")
		if os.path.exists(info_json_path):
			with open(info_json_path, "r", encoding="utf-8") as f:
				emotions_data = json.load(f)
				emotions = list(emotions_data.keys())

		if emotions:
			emotions.append("idle")
		# формируем словарь
		result[avatar_name] = {
			"animations": animations,
			"emotions": emotions
		}

	return result


def get_latest_version_folder(base_path: str) -> str | None:
	versions = []
	for name in os.listdir(base_path):
		if os.path.isdir(os.path.join(base_path, name)):
			try:
				major, middle, minor = map(int, name.split("."))
				versions.append((major, middle, minor, name))
			except ValueError:
				logger.trace(f"FOUND NOVERSION FOLDER NAME {name}")
				continue
			except Exception:
				logger.trace("EXCEPTION WHILE READING VERSIONS FOR AVATAR")
				continue

	if not versions:
		return None

	versions.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
	return versions[0][3]


def render_stream(chunk, render, is_last=False):
	BitsPerSample = 16
	NumChannels = 1
	SampleRate = 16000
	ChunkDuration = 2.0
	logger.trace("RENDER GOT AUDIO")

	if is_last:
		logger.trace("RENDER GOT LAST AUDIO - None")
		render.render_chunk(None, frame_rate=0, is_last=True)
		return

	aud_chunk = chunk.data
	is_voice = aud_chunk.is_voice

	aud_sample_rate = aud_chunk.sample_rate if aud_chunk.sample_rate != 0 and aud_chunk.sample_rate else SampleRate
	aud_bps = aud_chunk.bps if aud_chunk.bps != 0 and aud_chunk.bps else BitsPerSample
	aud_data = aud_chunk.data
	# aud_data = np.zeros_like(len(aud_data))
	# is_voice = False
	# logger.debug(f"AUDIO DATA {len(aud_data)}, {aud_bps}, {aud_sample_rate}")
	# aud_time = len(aud_data) / ((aud_bps / 8) * aud_sample_rate)
	render.render_chunk(aud_data, frame_rate=aud_sample_rate, is_last=is_last, is_voice=is_voice)


def stream_frames_thread(render, video_queue, height, width, start_time, request_id):
	frame_idx = 0
	first_frame_time = None
	every_frame_time = 1 / TARGET_FPS
	realtime_metric = 0
	current_frame_time = None

	if int(Config.MAX_SIZE) < width:
		new_width = int(Config.MAX_SIZE)
		new_height = int(round(height * int(Config.MAX_SIZE) / width))
		height = new_height
		width = new_width
	with logger.contextualize(request_id=request_id):
		# time_chunks_list = []
		# dt_full_time = 0
		# dt_min_time = 1000
		# dt_max_time = 0
		# dt_chunk_counter = 0
		# dt_first_chunk_flag = False
		# dt_first_cur_time = 0
		# good_chunks = 0
		# bad_chunks = 0
		# gb_info = ""
		for frame in render.sdk.stream_frames():
			# if not dt_first_chunk_flag:  # первый чанк
			# 	dt_first_cur_time = time.perf_counter()

			# dt_cur_time = time.perf_counter()
			# if dt_first_chunk_flag:  # все кроме первого чанка
			# 	dt_chunk_time = dt_cur_time - dt_start_time
			# 	if dt_chunk_time >= dt_max_time:
			# 		dt_max_time = dt_chunk_time
			# 	if dt_chunk_time <= dt_min_time:
			# 		dt_min_time = dt_chunk_time
			# 	dt_full_time += dt_chunk_time
			# 	dt_chunk_counter += 1
			# start_time.value = render.start_time
			if isinstance(frame, EventObject):
				video_queue.put(frame)
				continue
			
			if first_frame_time is None:
				first_frame_time = time.perf_counter()
			realtime_metric = (time.perf_counter() - first_frame_time) - frame_idx * every_frame_time
			if frame_idx == 0:
				elapsed_ms = (time.perf_counter() - start_time.value) * 1000 if start_time.value else 0.0
				video_queue.put(EventObject(event_name="wall_point", event_data={"name": "first_frame_ready", "elapsed_ms": elapsed_ms}))
				logger.debug(f"[EVENT] first_frame_ready frame_idx=0 realtime_metric={realtime_metric:.3f}s {width}x{height}")
			elif frame_idx % 100 == 0:
				# TRACE: progress every 100 frames (4s at 25fps) - for debugging only
				logger.trace(f"[EVENT] frame_ready frame_idx={frame_idx} realtime_metric={realtime_metric:.3f}s")
			# Read per-frame muted state recorded by _motion_stitch_worker
			try:
				is_muted = render.sdk.muted_frames_queue.get_nowait()
			except Exception:
				is_muted = render.sdk.force_silence
			video_queue.put(ImageObject(data=frame, height=height, width=width, is_muted=is_muted))
			frame_idx += 1
		# dt_start_time = dt_cur_time
		# dt_first_chunk_flag = True
		# time_difference = dt_cur_time - dt_first_cur_time
		# req_time_difference = 0.04 * dt_chunk_counter
		# logger.info(f"TIME DIFFERENCES (req:act) {req_time_difference}:{time_difference}")
		# time_chunks_list.append(time_difference)
		# if time_difference <= req_time_difference:
		# 	good_chunks += 1
		# 	gb_info += "1"
		# else:
		# 	bad_chunks += 1
		# 	gb_info += "0"
		video_queue.put(ImageObject(data=None, height=height, width=width))
	# logger.info(f"CHUNKS REALTIME INFO (g:b): {good_chunks}:{bad_chunks}")
	# logger.info(f"CHUNKS REALTIME INFO (g:b): {gb_info}")
	# logger.info(time_chunks_list)


def start_render_process(audio_queue, video_queue, start_time, sampling_timestamps, request_id):
	with logger.contextualize(request_id=request_id):
		# gc.disable()
		waiting_init = True
		render = None
		stream_thread = None
		infer_start_sent = False
		post_interrupt = False

		# Wall-clock stage points are forwarded to the main process for an authoritative per-request timeline.
		def send_wall_point(point_name: str):
			if start_time.value:
				elapsed_ms = (time.perf_counter() - start_time.value) * 1000
			else:
				elapsed_ms = 0.0
			video_queue.put(EventObject(event_name="wall_point", event_data={"name": point_name, "elapsed_ms": elapsed_ms}))

		while True:
			chunk = audio_queue.get()
			if waiting_init:
				# Client died / reader_thread aborted before init
				if chunk is None:
					message = "Render process: init chunk missing (client disconnected before first message)."
					logger.error(message)
					video_queue.put(ErrorObject(error_type=ErrorDataType.Handling, error_message=message))
					return
				if not isinstance(chunk, dict) or "is_online" not in chunk:
					message = f"Render process: expected init dict, got {type(chunk)}."
					logger.error(message)
					video_queue.put(ErrorObject(error_type=ErrorDataType.Handling, error_message=message))
					return
				send_wall_point("loading_start")
				render = RenderService(is_online=chunk["is_online"], sampling_timestamps=sampling_timestamps.value, video_queue=video_queue)
				send_wall_point("loading_end")
				waiting_init = False
				continue

			if chunk is None:
				try:
					render_stream(chunk=chunk, render=render, is_last=True)
				except Exception as e:
					logger.exception(f"Render process: finalization failed: {str(e)}")
					video_queue.put(ErrorObject(error_type=ErrorDataType.Handling, error_message=f"Error during finalization: {str(e)}"))
				break

			if chunk.data_type == IPCDataType.IMAGE:
				# logger.info("RECEIVE IMAGE CHUNK")
				img_chunk = chunk.data

				img_height = img_chunk.height
				img_width = img_chunk.width
				img_data = img_chunk.data
				if img_height % 2 != 0:
					img_height -= 1
				if img_width % 2 != 0:
					img_width -= 1
				logger.info(f"[DIMENSION] Image mode dimensions: {img_width}x{img_height}")
				send_wall_point("avatar_start")
				render.handle_image(image_chunk=img_data, video_queue=video_queue)
				send_wall_point("avatar_ready")

				logger.info("START STREAMING THREAD")
				# Send image_set event with dimensions (similar to avatar_set)
				video_queue.put(EventObject(event_name="image_set", event_data={"width": img_width, "height": img_height}))
				stream_thread = Thread(target=stream_frames_thread, args=(
					render, video_queue, img_height, img_width, start_time, request_id,))
				stream_thread.start()
				send_wall_point("stream_start")

			elif chunk.data_type == IPCDataType.AUDIO:
				# TRACE: ~100+ chunks per request = spam
				logger.trace("RECEIVE AUDIO CHUNK")
				if not infer_start_sent:
					send_wall_point("infer_start")
					if getattr(render, "sdk", None) is not None and getattr(render.sdk, "timing", None) is not None:
						render.sdk.timing.reset_clock(clear_points=True)
					infer_start_sent = True

				# Clear force_silence when new speech arrives after interrupt
				if post_interrupt and chunk.data.is_voice:
					logger.info("RESUME: new speech after interrupt, clearing force_silence")
					render.sdk.force_silence = False
					post_interrupt = False

				try:
					render_stream(chunk=chunk, render=render)
				except Exception as e:
					logger.exception(f"Render process: audio handling failed: {str(e)}")
					video_queue.put(ErrorObject(error_type=ErrorDataType.Handling, error_message=f"Error during audio handling: {str(e)}"))
					break

			elif chunk.data_type == IPCDataType.INTERRUPT:
				# INFO: rare event (max 1 per request), important state change
				logger.info("RECEIVE INTERRUPT")
				render.interrupt()
				post_interrupt = True

			elif chunk.data_type == IPCDataType.COMMAND:
				# INFO: rare event, explicit user action
				logger.info(f"RECEIVE COMMAND {chunk.data.command_type} {chunk.data.command_data}")
				if chunk.data.command_type == CommandDataType.SetAvatar:
					# render.set_avatar(avatar_id=chunk.data.command_data)

					avatar_name = chunk.data.command_data
					idle_name = chunk.data.additional_data.get("idle_name", "idle") or "idle"

					if avatar_name not in get_avatars():
						video_queue.put(ErrorObject(error_type=ErrorDataType.Avatar,
						                            error_message=f"Avatar {avatar_name} does not exist"))
						continue

					last_version = get_latest_version_folder(f"/app/assets/{avatar_name}")
					if last_version:
						base_path = f"/app/assets/{avatar_name}/{last_version}"
					else:
						base_path = f"/app/assets/{avatar_name}"
					reader = imageio.get_reader(f"{base_path}/{avatar_name}.mp4")
					size = reader.get_meta_data()["size"]
					img_width = int(size[0])
					img_height = int(size[1])
					logger.info(f"[DIMENSION] Avatar mode dimensions from MP4: {img_width}x{img_height}")

					if os.path.exists(f"{base_path}/config.json"):
						with open(f"{base_path}/config.json", "r", encoding="utf-8") as f:
							agnet_config = json.load(f)
					else:
						agnet_config = {}

					emotions_exist = os.path.exists(f"{base_path}/emotions/")
					send_wall_point("avatar_start")
					render.handle_video(
						base_path=base_path,
						avatar_name=avatar_name,
						version_name=last_version,
						emotions=emotions_exist,
						agnet_config=agnet_config,
						video_queue=video_queue,
						idle_name=idle_name
					)
					send_wall_point("avatar_ready")

					logger.info("START STREAMING THREAD")
					video_queue.put(EventObject(event_name="avatar_set", event_data={"avatar_id": avatar_name, "width": img_width, "height": img_height}))
					stream_thread = Thread(target=stream_frames_thread, args=(
						render, video_queue, img_height, img_width, start_time, request_id,))
					stream_thread.start()
					send_wall_point("stream_start")

				elif chunk.data.command_type == CommandDataType.PlayAnimation:
					animation_name = chunk.data.command_data
					if animation_name not in get_avatars()[avatar_name]["animations"]:
						video_queue.put(ErrorObject(error_type=ErrorDataType.Animation,
						                            error_message=f"Animation {animation_name} for avatar {avatar_name} does not exist"))
						continue
					render.play_animation(animation=chunk.data.command_data, auto_idle=chunk.data.additional_data["auto_idle"])

				elif chunk.data.command_type == CommandDataType.SetEmotion:
					render.set_emotion(emotion=chunk.data.command_data)

				elif chunk.data.command_type == CommandDataType.ClearAnimations:
					if render is not None:
						render.clear_animations()
						video_queue.put(EventObject(event_name="animations_cleared", event_data={}))
					else:
						logger.warning("ClearAnimations called but render is not initialized yet")

		logger.info("START CLOSING PROCESSES IN PROCESS")
		try:
			if render is not None and getattr(render, "sdk", None) is not None:
				render.sdk.close()
		except Exception as e:
			logger.exception(f"ERROR WHILE CLOSING SDK {str(e)}")
		logger.info("FINISH JOINING THREAD IN PROCESS")
		if stream_thread is not None:
			stream_thread.join()
		logger.info("FINISH CLOSING PROCESSES IN PROCESS")


def reader_thread(request_iterator, audio_queue, video_queue, is_online, is_alpha, output_format, sampling_timestamps, request_id, context, progress_tracker=None, audio_ms_total=None):
	with logger.contextualize(request_id=request_id):
		first_audio_logged = False
		init_sent = False
		try:
			for chunk in request_iterator:
				if chunk.image.data:
					logger.info("[EVENT] request_received")
					logger.info(f"ONLINE MODE: {chunk.online}")
					logger.info(f"ALPHA MODE: {chunk.alpha}")
					logger.info(f"IMAGE SIZE: {chunk.image.width}x{chunk.image.height}")
					if chunk.online:
						is_online.set()
					if chunk.alpha:
						is_alpha.set()
					sampling_timestamps.value = chunk.sampling_timestamps
					output_format.set("BGRA" if chunk.output_format == "" else chunk.output_format)
					audio_queue.put({"is_online": chunk.online})
					audio_queue.put(IPCObject(data_type=IPCDataType.IMAGE,
					                          data=ImageObject(data=chunk.image.data,
					                                           height=chunk.image.height,
					                                           width=chunk.image.width)))
					init_sent = True

				elif chunk.audio.data:
					if not init_sent:
						message = "Audio chunk received before initialization (image/set_avatar)."
						logger.error(message)
						video_queue.put(ErrorObject(error_type=ErrorDataType.Handling, error_message=message))
						break
					if not first_audio_logged:
						logger.info("[EVENT] first_audio_chunk")
						first_audio_logged = True
					sample_rate = chunk.audio.sample_rate or 16000
					bps = chunk.audio.bps or 16
					if bps != 16:
						message = f"Unsupported audio bps={chunk.audio.bps} (expected 16 or 0)"
						logger.error(message)
						video_queue.put(ErrorObject(error_type=ErrorDataType.Handling, error_message=message))
						break
					bps_bytes = bps // 8
					if sample_rate <= 0 or bps_bytes <= 0:
						message = f"Invalid audio parameters: sample_rate={chunk.audio.sample_rate} bps={chunk.audio.bps}"
						logger.error(message)
						video_queue.put(ErrorObject(error_type=ErrorDataType.Handling, error_message=message))
						break
					if len(chunk.audio.data) % bps_bytes != 0:
						message = f"Invalid audio chunk: data_size={len(chunk.audio.data)} not aligned to bytes_per_sample={bps_bytes}"
						logger.error(message)
						video_queue.put(ErrorObject(error_type=ErrorDataType.Handling, error_message=message))
						break
					# фактическое время
					chunk_sum = len(chunk.audio.data) / (bps_bytes * sample_rate)
					# количество байт для 1 секунды
					req_sum = int(bps_bytes * sample_rate * 1)

					if audio_ms_total is not None:
						audio_ms_total.value += chunk_sum * 1000

					# Accumulate total_frames for offline mode progress tracking
					if progress_tracker and not is_online.is_set():
						progress_tracker.total_frames += int(chunk_sum * TARGET_FPS)
					if chunk_sum > 1:  # 1 секунда
						it_num = int(-(-chunk_sum // 1))
						for it in range(it_num):
							l_border = it * req_sum
							r_border = (it + 1) * req_sum
							cut_data = chunk.audio.data[l_border: r_border]
							audio_queue.put(IPCObject(data_type=IPCDataType.AUDIO,
							                          data=AudioObject(data=cut_data,
							                                           sample_rate=sample_rate,
							                                           bps=bps,
							                                           is_voice=chunk.audio.is_voice)))

					else:
						audio_queue.put(IPCObject(data_type=IPCDataType.AUDIO,
						                          data=AudioObject(data=chunk.audio.data,
						                                           sample_rate=sample_rate,
						                                           bps=bps,
						                                           is_voice=chunk.audio.is_voice)))

				elif chunk.WhichOneof("command") == "set_avatar":
					logger.info("[EVENT] request_received")
					logger.info(f"ONLINE MODE: {chunk.online}")
					logger.info(f"ALPHA MODE: {chunk.alpha}")
					if chunk.online:
						is_online.set()
					if chunk.alpha:
						is_alpha.set()
					sampling_timestamps.value = chunk.sampling_timestamps
					output_format.set("BGRA" if chunk.output_format == "" else chunk.output_format)
					audio_queue.put({"is_online": chunk.online})
					audio_queue.put(IPCObject(data_type=IPCDataType.COMMAND,
					                          data=CommandObject(command_type=CommandDataType.SetAvatar,
					                                             command_data=chunk.set_avatar.avatar_id, additional_data={"idle_name": chunk.set_avatar.idle_name})))
					init_sent = True

				elif chunk.WhichOneof("command") == "play_animation":
					audio_queue.put(IPCObject(data_type=IPCDataType.COMMAND,
					                          data=CommandObject(command_type=CommandDataType.PlayAnimation,
					                                             command_data=chunk.play_animation.animation,
					                                             additional_data={"auto_idle": chunk.play_animation.auto_idle})))

				elif chunk.WhichOneof("command") == "set_emotion":
					audio_queue.put(IPCObject(data_type=IPCDataType.COMMAND,
					                          data=CommandObject(command_type=CommandDataType.SetEmotion,
					                                             command_data=chunk.set_emotion.emotion)))

				elif chunk.WhichOneof("command") == "clear_animations":
					logger.info("GOT CLEAR_ANIMATIONS")
					audio_queue.put(IPCObject(data_type=IPCDataType.COMMAND,
					                          data=CommandObject(command_type=CommandDataType.ClearAnimations,
					                                             command_data="")))

				elif chunk.WhichOneof("command") == "interrupt":
					audio_queue.put(IPCObject(data_type=IPCDataType.INTERRUPT,
					                          data=InterruptObject()))

		except RpcError as e:
			logger.exception(f"GOT RPCERROR EXCEPTION IN READER THREAD {str(e)} START CLOSING PROCESS")
			context.cancel()
		except Exception as e:
			logger.exception(f"GOT EXCEPTION IN READER THREAD {str(e)} START CLOSING PROCESS")
			video_queue.put(ErrorObject(error_type=ErrorDataType.Handling, error_message=f"Reader thread error: {str(e)}"))
		except BaseException as e:
			logger.exception(f"GOT BASEEXCEPTION IN READER THREAD {str(e)} START CLOSING PROCESS")
		audio_queue.put(None)
		logger.info("END REQUEST ITERATOR")


def get_mqueue_thread(from_queue, to_queue, is_alpha, output_format, alpha_service, request_id):
	with logger.contextualize(request_id=request_id):
		t_mqueue = TimingTracker(enabled=True, log_each=False)
		shape_debug_logged = False
		try:
			while True:
				# logger.info("WAITING FOR FRAME FROM VIDEO QUEUE")
				frame = from_queue.get(timeout=1000)
				if isinstance(frame, EventObject):
					to_queue.put(frame)
					continue
				elif isinstance(frame, ErrorObject):
					to_queue.put(frame)
					continue
				logger.trace("CHUNK MID -> LAST")
				if frame.data is None:
					logger.trace("CHUNK MID IS NONE - BREAK")
					break

				data_bytes = frame.data if isinstance(frame.data, (bytes, bytearray, memoryview)) else frame.data.tobytes()
				expected = frame.width * frame.height * 3
				if not shape_debug_logged:
					logger.debug(f"[MQUEUE] bytes={len(data_bytes)} expected={expected} w={frame.width} h={frame.height}")
					shape_debug_logged = True
				if len(data_bytes) != expected:
					message = f"[MQUEUE] size mismatch len={len(data_bytes)} expected={expected} w={frame.width} h={frame.height}"
					logger.error(message)
					to_queue.put(ErrorObject(error_type=ErrorDataType.Handling, error_message=message))
					break

				rgb = np.frombuffer(data_bytes, dtype=np.uint8).reshape(frame.width, frame.height, 3)
				is_muted = getattr(frame, 'is_muted', False)

				if is_alpha.is_set():
					frame.data = (rgb[..., [0, 1, 2]]).tobytes()
					with t_mqueue.measure("alpha_segment"):
						new_data = alpha_service.segment_person_from_pil_image(frame_in=frame)
					frame = ImageObject(
						data=new_data,
						width=frame.width,
						height=frame.height,
						is_muted=is_muted
					)

				else:
					if output_format.get() == "BGRA":
						logger.trace("START NUMPY CONVERSIONS")
						bgra = np.empty((frame.width, frame.height, 4), dtype=np.uint8)

						# Channel order: B, G, R, A
						bgra[..., 0] = rgb[..., 2]
						bgra[..., 1] = rgb[..., 1]
						bgra[..., 2] = rgb[..., 0]
						bgra[..., 3] = 255

						bgra_bytes = bgra.tobytes()
						logger.trace("END NUMPY CONVERSIONS")
						frame = ImageObject(
							data=bgra_bytes,
							width=frame.width,
							height=frame.height,
							is_muted=is_muted
						)
					elif output_format.get() == "RGBA":
						alpha = np.full((frame.width, frame.height, 1), 255, dtype=np.uint8)
						rgba = np.concatenate((rgb, alpha), axis=2)
						rgba_bytes = rgba.tobytes()
						frame = ImageObject(
							data=rgba_bytes,
							width=frame.width,
							height=frame.height,
							is_muted=is_muted
						)
					else:
						rgb_bytes = rgb.tobytes()
						frame = ImageObject(
							data=rgb_bytes,
							width=frame.width,
							height=frame.height,
							is_muted=is_muted
						)

				to_queue.put(frame)
		except RpcError as e:
			logger.exception(f"GOT RPCERROR EXCEPTION IN GET MQUEUE THREAD {str(e)} START CLOSING PROCESS")
		except Exception as e:
			logger.exception(f"GOT EXCEPTION IN GET MQUEUE THREAD {str(e)} START CLOSING PROCESS")
		except BaseException as e:
			logger.exception(f"GOT BASEEXCEPTION IN GET MQUEUE THREAD {str(e)} START CLOSING PROCESS")
		t_mqueue.log_summary(label="MQUEUE THREAD", include_points=False)
		to_queue.put(ImageObject(data=None, width=None, height=None))
		logger.info("FINISHED GET MQUEUE THREAD")



class StreamingService(render_service_pb2_grpc.RenderServiceServicer):
	def __init__(self, alpha_service):
		self.alpha_service = alpha_service
		self.critical_exceptions = [ErrorDataType.Initialization]
		self.important_exceptions = [ErrorDataType.Handling]

	def RenderStream(self, request_iterator, context):
		request_id = str(uuid4())
		frame_idx = 0
		with logger.contextualize(request_id=request_id):
			audio_queue = mQueue()
			# audio_queue
			video_queue = mQueue()
			local_queue = tQueue()
			# local_queue
			is_online = Event()
			is_alpha = Event()
			output_format = SharedValue("")
			sampling_timestamps = Value('d', 0)
			audio_ms_total = Value('d', 0.0)
			full_start_time = Value('d', 0.0)
			full_start_time.value = time.perf_counter()
			t_wall = TimingTracker(enabled=True, log_each=False)
			t_wall.point_ms("req_start", 0.0)
			# full_times_list = []
			render_process = Process(target=start_render_process,
			                         args=(audio_queue, video_queue, full_start_time, sampling_timestamps, request_id))
			render_process.start()

			# Progress tracking
			# Offline mode: expects full audio in single chunk (server splits internally)
			# If multi-chunk offline needed, accumulate total_frames in reader_thread
			progress_tracker = ProgressTracker(
				emit_interval=TARGET_FPS,
				is_online=is_online.is_set(),
				total_frames=0,
			)

			reader_trd = Thread(target=reader_thread, args=(
				request_iterator, audio_queue, video_queue, is_online, is_alpha, output_format, sampling_timestamps, request_id, context, progress_tracker, audio_ms_total))
			reader_trd.start()

			mqueue_trd = Thread(target=get_mqueue_thread,
			                              args=(video_queue, local_queue, is_alpha, output_format, self.alpha_service,
			                                    request_id,))
			mqueue_trd.start()

			first_chunk_flag = False
			# first_img_flag = True
			# full_time = 0
			# min_time = 1000
			# max_time = 0
			# chunk_counter = 0
			avatar_sent_mem = None
			avatar_id = None
			# Track frame dimensions for VideoChunk (fallback if frame.width/height is None)
			tracked_width = None
			tracked_height = None
			frames_sent = 0
			bytes_sent = 0
			queue_wait_total_ms = 0.0
			queue_wait_max_ms = 0.0
			queue_get_count = 0
			stream_started = False  # only track queue waits after first frame sent
			grpc_gap_max_ms = 0.0
			prev_video_send_time = None
			first_send_ms = None
			stream_end_ms = None

			first_frame_time = None
			every_frame_time = 1 / TARGET_FPS
			realtime_metric = 0

			try:
				while True:
					queue_get_start = time.perf_counter()
					frame = local_queue.get()
					# only track queue waits after stream started (skip TTFF wait)
					if stream_started:
						queue_wait_ms = (time.perf_counter() - queue_get_start) * 1000
						queue_wait_total_ms += queue_wait_ms
						if queue_wait_ms > queue_wait_max_ms:
							queue_wait_max_ms = queue_wait_ms
						queue_get_count += 1
					logger.trace("CHUNK LAST -> OUTPUT")
					if isinstance(frame, EventObject):
						if frame.event_name == "wall_point":
							point_name = frame.event_data.get("name")
							elapsed_ms = frame.event_data.get("elapsed_ms")
							if point_name and elapsed_ms is not None:
								t_wall.point_once_ms(point_name, float(elapsed_ms))
							continue
						if frame.event_name == "animation":
							if frame.event_data["event"]:
								yield RenderResponse(
									start_animation=StartAnimation(animation_name=frame.event_data["name"]))
								ev = 1
							else:
								yield RenderResponse(
									end_animation=EndAnimation(animation_name=frame.event_data["name"]))
								ev = 0
							logger.debug(f"SENT ANIMATION EVENT {frame.event_data['name']} {ev}")
						elif frame.event_name == "emotion":
							yield RenderResponse(
								emotion_set=EmotionSet(emotion_name=frame.event_data["name"]))
							logger.debug(f"SENT EMOTION EVENT")
						elif frame.event_name == "animations_cleared":
							yield RenderResponse(
								animations_cleared=AnimationsCleared())
							logger.debug(f"SENT ANIMATIONS_CLEARED EVENT")
						elif frame.event_name == "avatar_set":
							avatar_sent_mem = frame
							avatar_id = frame.event_data.get("avatar_id")
							# Track dimensions from avatar_set event (avatar mode)
							if frame.event_data.get("width") is not None and frame.event_data.get("height") is not None:
								tracked_width = frame.event_data.get("width")
								tracked_height = frame.event_data.get("height")
								logger.debug(f"[DIMENSION] Tracked avatar dimensions from event: {tracked_width}x{tracked_height}")
						elif frame.event_name == "image_set":
							# Track dimensions from image_set event (image mode)
							if frame.event_data.get("width") is not None and frame.event_data.get("height") is not None:
								tracked_width = frame.event_data.get("width")
								tracked_height = frame.event_data.get("height")
								logger.debug(f"[DIMENSION] Tracked image dimensions from event: {tracked_width}x{tracked_height}")

						continue
					elif isinstance(frame, ErrorObject):
						yield RenderResponse(request_error=RequestError(error_type=str(frame.error_type), error_message=frame.error_message))
						logger.error(f"GOT ERROR OBJECT. ERROR TYPE: {str(frame.error_type)} ERROR MESSAGE: {frame.error_message}")

						if frame.error_type in self.critical_exceptions:
							logger.info("KILL CONTAINER")
							os._exit(1)
						elif frame.error_type in self.important_exceptions:
							logger.info("STOP PROCESS")
							break
						continue
					# if frame.error_type == "avatar":
					# 	break
					elif frame.data is None:
						# TRACE: final state, not milestone
						logger.trace("CHUNK LAST IS NONE - BREAK")
						elapsed_ms = (time.perf_counter() - full_start_time.value) * 1000 if full_start_time.value else 0.0
						t_wall.point_once_ms("stream_end", elapsed_ms)
						stream_end_ms = elapsed_ms
						break
					# cur_time = time.perf_counter()
					# if first_img_flag:
					#     img = Image.frombytes("RGBA", (frame.height, frame.width),
					#                           frame.data, "raw", "BGRA")
					#     img.save(f"./checkpoints/frame.png")
					#     first_img_flag = False
					# if first_chunk_flag:
					# 	cur_time = time.perf_counter()
					# 	chunk_time = cur_time - start_time
					# 	if chunk_time >= max_time:
					# 		max_time = chunk_time
					# 	if chunk_time <= min_time:
					# 		min_time = chunk_time
					# 	full_time += chunk_time
					# 	chunk_counter += 1
					# logger.info(f"GRPC CHUNK TIME: {chunk_time}")
					# logger.info(f"START GRPC YIELDING {chunk_counter}")
					# full_delta_time = time.perf_counter() - full_start_time.value
					# full_times_list.append(full_delta_time)
					active_client = context.is_active()
					# TRACE: every frame = spam
					logger.trace(f"CLIENT: {active_client}")
					if not active_client:
						raise RpcError
					if frame_idx == 0:
						logger.debug(f"[EVENT] SEND 0")
					elif frame_idx % 100 == 0:
						logger.trace(f"[EVENT] SEND {frame_idx}")
					if avatar_sent_mem:
						yield RenderResponse(
							avatar_set=AvatarSet(avatar_id=avatar_sent_mem.event_data["avatar_id"]))
						logger.debug(f"SENT AVATAR SET EVENT")
						avatar_sent_mem = None
					# logger.info(f"{frame.width}x{frame.height}, {len(frame.data)}")
					if frame_idx == 0:
						elapsed_ms = (time.perf_counter() - full_start_time.value) * 1000 if full_start_time.value else 0.0
						t_wall.point_once_ms("first_send", elapsed_ms)
						first_send_ms = elapsed_ms
						stream_started = True
					send_time = time.perf_counter()
					if prev_video_send_time is not None:
						gap_ms = (send_time - prev_video_send_time) * 1000
						if gap_ms > grpc_gap_max_ms:
							grpc_gap_max_ms = gap_ms
					prev_video_send_time = send_time
					frames_sent += 1
					bytes_sent += len(frame.data)
					# Track dimensions from first valid frame, use as fallback for subsequent frames
					if frame.width is not None and frame.height is not None:
						if tracked_width is None:
							tracked_width = frame.width
							tracked_height = frame.height
							logger.debug(f"[DIMENSION] Tracked frame dimensions: {tracked_width}x{tracked_height}")
					# Use tracked dimensions as fallback if frame dimensions are None
					video_width = frame.width if frame.width is not None else tracked_width
					video_height = frame.height if frame.height is not None else tracked_height
					if video_width is None or video_height is None:
						logger.warning(f"[DIMENSION] Missing frame dimensions at frame_idx={frame_idx}, frame.width={frame.width}, frame.height={frame.height}")
					yield RenderResponse(video=VideoChunk(data=frame.data, width=video_width or 0, height=video_height or 0, frame_idx=frame_idx, is_muted=getattr(frame, 'is_muted', False)))
					if first_frame_time is None:
						first_frame_time = time.perf_counter()
					realtime_metric = (time.perf_counter() - first_frame_time) - frame_idx * every_frame_time
					if frame_idx == 0:
						logger.debug(f"[EVENT] SENT 0 realtime_metric={realtime_metric:.3f}s")
					elif frame_idx % 100 == 0:
						logger.trace(f"[EVENT] SENT {frame_idx} realtime_metric={realtime_metric:.3f}s")

					frame_idx += 1

					# Emit progress metadata periodically
					metadata = progress_tracker.track_frame_progress(realtime_metric=realtime_metric)
					if metadata:
						yield RenderResponse(metadata=metadata)
				# logger.info(f"END GRPC YIELDING {chunk_counter}")
				# start_time = cur_time
				# first_chunk_flag = True
			except RpcError as e:
				logger.exception(f"GOT RPCERROR EXCEPTION IN RENDER STREAM {str(e)} START CLOSING PROCESS")
			except Exception as e:
				logger.exception(f"GOT EXCEPTION IN RENDER STREAM {str(e)} START CLOSING PROCESS")
			except SystemExit as e:
				raise e
			except BaseException as e:
				logger.exception(f"GOT BASEEXCEPTION IN RENDER STREAM {str(e)} START CLOSING PROCESS")
			video_queue.put(ImageObject(data=None, height=None, width=None))
			logger.info(f"START JOINING PROCESS")
			render_process.terminate()
			logger.info(f"PROCESS FINISHED")
			reader_trd.join()
			logger.info(f"READER FINISHED")
			mqueue_trd.join()
			logger.info(f"MQUEUE FINISHED")
			# logger.info(f"MIN CHUNK TIME: {min_time}")
			# logger.info(f"MAX CHUNK TIME: {max_time}")
			# if chunk_counter != 0:
			# 	logger.info(f"AVG CHUNK TIME: {full_time / chunk_counter}")
			# logger.info(f"FULL TIME: {full_time}")
			# logger.info(f"ALL PROCESSES CLOSED")
			# logger.info(full_times_list)
			elapsed_ms = (time.perf_counter() - full_start_time.value) * 1000 if full_start_time.value else 0.0
			t_wall.point_once_ms("stream_end", elapsed_ms)
			if stream_end_ms is None:
				stream_end_ms = elapsed_ms
			t_wall.point_once_ms("req_end", elapsed_ms)
			t_wall.delta("service_init", "loading_start", "loading_end")
			t_wall.delta("avatar_prepare", "avatar_start", "avatar_ready")
			t_wall.delta("audio_wait", "stream_start", "infer_start")
			t_wall.delta("prime_first_frame", "infer_start", "first_frame_ready")
			t_wall.delta("ttff_server", "req_start", "first_send")
			t_wall.delta("stream_duration", "first_send", "stream_end")
			t_wall.delta("cleanup", "stream_end", "req_end")
			t_wall.delta("total", "req_start", "req_end")
			stream_duration_s = 0.0
			if first_send_ms is not None and stream_end_ms is not None and stream_end_ms > first_send_ms:
				stream_duration_s = (stream_end_ms - first_send_ms) / 1000
			avg_send_fps = (frames_sent / stream_duration_s) if stream_duration_s > 0 else 0.0
			queue_wait_avg_ms = (queue_wait_total_ms / queue_get_count) if queue_get_count else 0.0
			t_wall.log_summary(label="WALL", include_points=False, extra={
				"avatar_id": avatar_id or "-",
				"audio_ms": int(audio_ms_total.value),
				"target_fps": TARGET_FPS,
				"frames_sent": frames_sent,
				"bytes_sent": bytes_sent,
				"avg_send_fps": f"{avg_send_fps:.1f}",
				"queue_wait_avg_ms": f"{queue_wait_avg_ms:.1f}",
				"queue_wait_max_ms": f"{queue_wait_max_ms:.1f}",
				"grpc_gap_max_ms": f"{grpc_gap_max_ms:.1f}",
			})
			logger.info(f"COMPLETE")

	def InfoRouter(self, request, context):
		folder_path = "/app/assets/"

		avatars = get_avatars(folder_path=folder_path)
		animations_result = {}
		emotions_result = {}
		for avatar in avatars:
			animations_result[avatar] = ItemList(items=avatars[avatar]["animations"])
			emotions_result[avatar] = ItemList(items=avatars[avatar]["emotions"])

		return InfoResponse(animations=animations_result, emotions=emotions_result)
