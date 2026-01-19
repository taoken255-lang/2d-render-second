import subprocess
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
from proto.render_service_pb2 import RenderResponse, VideoChunk, StartAnimation, EndAnimation, AvatarSet, InfoResponse, ItemList, RequestError, EmotionSet

from service.render import RenderService
from service.object_models import IPCObject, IPCDataType, CommandObject, CommandDataType, AudioObject, ImageObject, EventObject, ErrorObject, ErrorDataType

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
		pass

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
				logger.info(f"FOUND NOVERSION FOLDER NAME {name}")
				continue
			except Exception:
				logger.info(f"EXCEPTION WHILE READING VERSIONS FOR AVATAR")
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
	logger.debug("RENDER GOT AUDIO")

	if is_last:
		logger.debug("RENDER GOT LAST AUDIO - None")
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
	every_frame_time = 0.04
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
			if frame_idx % 25 == 0:
				logger.info(f"Frame ready from NN {frame_idx}, realtime_metric: {realtime_metric}, {time.perf_counter() - realtime_metric} {width}x{height}, {len(frame)}")
			video_queue.put(ImageObject(data=frame, height=height, width=width))
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
		is_online_chunk = True
		while True:
			chunk = audio_queue.get()
			if is_online_chunk:
				render = RenderService(is_online=chunk["is_online"], sampling_timestamps=sampling_timestamps.value, video_queue=video_queue)
				is_online_chunk = False
				continue

			if chunk is None:
				render_stream(chunk=chunk, render=render, is_last=True)
				break

			if chunk.data_type == IPCDataType.IMAGE:
				logger.info("RECEIVE IMAGE CHUNK")
				img_chunk = chunk.data

				img_height = img_chunk.height
				img_width = img_chunk.width
				img_data = img_chunk.data
				if img_height % 2 != 0:
					img_height -= 1
				if img_width % 2 != 0:
					img_width -= 1
				render.handle_image(image_chunk=img_data, video_queue=video_queue)

				logger.info("START STREAMING THREAD")
				stream_thread = Thread(target=stream_frames_thread, args=(
					render, video_queue, img_height, img_width, start_time, request_id,))
				stream_thread.start()

			elif chunk.data_type == IPCDataType.AUDIO:
				logger.info("RECEIVE AUDIO CHUNK")
				render_stream(chunk=chunk, render=render)

			elif chunk.data_type == IPCDataType.COMMAND:
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

					if os.path.exists(f"{base_path}/config.json"):
						with open(f"{base_path}/config.json", "r", encoding="utf-8") as f:
							agnet_config = json.load(f)
					else:
						agnet_config = {}

					emotions_exist = os.path.exists(f"{base_path}/emotions/")
					render.handle_video(
						base_path=base_path,
						avatar_name=avatar_name,
						version_name=last_version,
						emotions=emotions_exist,
						agnet_config=agnet_config,
						video_queue=video_queue,
						idle_name=idle_name
					)

					logger.info("START STREAMING THREAD")
					video_queue.put(EventObject(event_name="avatar_set", event_data={"avatar_id": avatar_name}))
					stream_thread = Thread(target=stream_frames_thread, args=(
						render, video_queue, img_height, img_width, start_time, request_id,))
					stream_thread.start()

				elif chunk.data.command_type == CommandDataType.PlayAnimation:
					animation_name = chunk.data.command_data
					if animation_name not in get_avatars()[avatar_name]["animations"]:
						video_queue.put(ErrorObject(error_type=ErrorDataType.Animation,
						                            error_message=f"Animation {animation_name} for avatar {avatar_name} does not exist"))
						continue
					render.play_animation(animation=chunk.data.command_data, auto_idle=chunk.data.additional_data["auto_idle"])

				elif chunk.data.command_type == CommandDataType.SetEmotion:
					render.set_emotion(emotion=chunk.data.command_data)

		logger.info("START CLOSING PROCESSES IN PROCESS")
		render.sdk.close()
		logger.info("FINISH JOINING THREAD IN PROCESS")
		stream_thread.join()
		logger.info("FINISH CLOSING PROCESSES IN PROCESS")


def reader_thread(request_iterator, audio_queue, is_online, is_alpha, output_format, sampling_timestamps, request_id, context):
	with logger.contextualize(request_id=request_id):
		try:
			for chunk in request_iterator:
				if chunk.image.data:
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

				elif chunk.audio.data:
					bps_bytes = int(chunk.audio.bps / 8)
					chunk_sum = len(chunk.audio.data) / (bps_bytes * chunk.audio.sample_rate)  # фактическое время
					req_sum = int(bps_bytes * chunk.audio.sample_rate * 1)  # количество байт для 1 секунды
					if chunk_sum > 1:  # 1 секунда
						it_num = int(-(-chunk_sum // 1))
						for it in range(it_num):
							l_border = it * req_sum
							r_border = (it + 1) * req_sum
							cut_data = chunk.audio.data[l_border: r_border]
							audio_queue.put(IPCObject(data_type=IPCDataType.AUDIO,
							                          data=AudioObject(data=cut_data,
							                                           sample_rate=chunk.audio.sample_rate,
							                                           bps=chunk.audio.bps,
							                                           is_voice=chunk.audio.is_voice)))

					else:
						audio_queue.put(IPCObject(data_type=IPCDataType.AUDIO,
						                          data=AudioObject(data=chunk.audio.data,
						                                           sample_rate=chunk.audio.sample_rate,
						                                           bps=chunk.audio.bps,
						                                           is_voice=chunk.audio.is_voice)))

				elif chunk.WhichOneof("command") == "set_avatar":
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

				elif chunk.WhichOneof("command") == "play_animation":
					audio_queue.put(IPCObject(data_type=IPCDataType.COMMAND,
					                          data=CommandObject(command_type=CommandDataType.PlayAnimation,
					                                             command_data=chunk.play_animation.animation,
					                                             additional_data={"auto_idle": chunk.play_animation.auto_idle})))

				elif chunk.WhichOneof("command") == "set_emotion":
					audio_queue.put(IPCObject(data_type=IPCDataType.COMMAND,
					                          data=CommandObject(command_type=CommandDataType.SetEmotion,
					                                             command_data=chunk.set_emotion.emotion)))

				# elif chunk.WhichOneof("command") == "interrupt":
				# 	audio_queue...

		except RpcError as e:
			logger.error(f"GOT RPCERROR EXCEPTION IN READER THREAD {str(e)} START CLOSING PROCESS")
			context.cancel()
		except Exception as e:
			logger.error(f"GOT EXCEPTION IN READER THREAD {str(e)} START CLOSING PROCESS")
		except BaseException as e:
			logger.error(f"GOT BASEEXCEPTION IN READER THREAD {str(e)} START CLOSING PROCESS")
		audio_queue.put(None)
		logger.info("END REQUEST ITERATOR")


def get_mqueue_thread(from_queue, to_queue, is_alpha, output_format, alpha_service, request_id):
	with logger.contextualize(request_id=request_id):
		try:
			while True:
				frame = from_queue.get(timeout=600)
				if isinstance(frame, EventObject):
					# logger.info("EVENTOBJECT")
					to_queue.put(frame)
					continue
				elif isinstance(frame, ErrorObject):
					# logger.info("ERROROBJECT")
					to_queue.put(frame)
					continue
				# logger.info("FRAMEOBJECT")
				logger.debug("CHUNK MID -> LAST")
				if frame.data is None:
					logger.debug("CHUNK MID IS NONE - BREAK")
					break

				rgb = np.frombuffer(frame.data.tobytes(), dtype=np.uint8).reshape(frame.width, frame.height, 3)
				if is_alpha.is_set():
					bgra = (rgb[..., [0, 1, 2]]).tobytes()
					frame.data = bgra
					segm_start = time.perf_counter()
					new_data = alpha_service.segment_person_from_pil_image(frame_in=frame)
					segm_end = time.perf_counter()
					logger.debug(f"SEGMENTATION TIME: {segm_end - segm_start}")
					frame = ImageObject(
						data=new_data,
						width=frame.width,
						height=frame.height
					)

				else:
					if output_format.get() == "BGRA":
						logger.debug("START NUMPY CONVERTATIONS")
						bgra = np.empty((frame.width, frame.height, 4), dtype=np.uint8)

						bgra[..., 0] = rgb[..., 2]  # B
						bgra[..., 1] = rgb[..., 1]  # G
						bgra[..., 2] = rgb[..., 0]  # R
						bgra[..., 3] = 255  # A

						bgra_bytes = bgra.tobytes()
						logger.debug("END NUMPY CONVERTATIONS")
						frame = ImageObject(
							data=bgra_bytes,
							width=frame.width,
							height=frame.height
						)
					elif output_format.get() == "RGBA":
						alpha = np.full((frame.height, frame.width, 1), 255, dtype=np.uint8)
						rgba = np.concatenate((rgb, alpha), axis=2)
						rgba_bytes = rgba.tobytes()
						frame = ImageObject(
							data=rgba_bytes,
							width=frame.width,
							height=frame.height
						)
					else:
						rgb_bytes = rgb.tobytes()
						frame = ImageObject(
							data=rgb_bytes,
							width=frame.width,
							height=frame.height
						)

				# frame = ImageObject(
				#     data="CHUNK".encode('utf-8'),
				#     width=frame.width,
				#     height=frame.height
				# )
				to_queue.put(frame)
		except RpcError as e:
			logger.error(f"GOT RPCERROR EXCEPTION IN GET MQUEUE THREAD {str(e)} START CLOSING PROCESS")
		except Exception as e:
			logger.error(f"GOT EXCEPTION IN GET MQUEUE THREAD {str(e)} START CLOSING PROCESS")
		except BaseException as e:
			logger.error(f"GOT BASEEXCEPTION IN GET MQUEUE THREAD {str(e)} START CLOSING PROCESS")
		to_queue.put(ImageObject(data=None, width=None, height=None))


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
			full_start_time = Value('d', 0.0)
			# full_times_list = []
			render_process = Process(target=start_render_process,
			                         args=(audio_queue, video_queue, full_start_time, sampling_timestamps, request_id))
			render_process.start()

			reader_trd = Thread(target=reader_thread, args=(
				request_iterator, audio_queue, is_online, is_alpha, output_format, sampling_timestamps, request_id, context))
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

			first_frame_time = None
			every_frame_time = 0.04
			realtime_metric = 0
			try:
				while True:
					frame = local_queue.get()
					logger.debug("CHUNK LAST -> OUTPUT")
					if isinstance(frame, EventObject):
						if frame.event_name == "animation":
							if frame.event_data["event"]:
								yield RenderResponse(
									start_animation=StartAnimation(animation_name=frame.event_data["name"]))
								ev = 1
							else:
								yield RenderResponse(
									end_animation=EndAnimation(animation_name=frame.event_data["name"]))
								ev = 0
							logger.info(f"SENT ANIMATION EVENT {frame.event_data['name']} {ev}")
						elif frame.event_name == "emotion":
							yield RenderResponse(
								emotion_set=EmotionSet(emotion_name=frame.event_data["name"]))
							logger.info(f"SENT EMOTION EVENT")
						elif frame.event_name == "avatar_set":
							avatar_sent_mem = frame

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
						logger.debug("CHUNK LAST IS NONE - BREAK")
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
					logger.debug(f"CLIENT: {active_client}")
					if not active_client:
						raise RpcError
					if frame_idx % 25 == 0:
						logger.info(f"SEND {frame_idx}")
					if avatar_sent_mem:
						yield RenderResponse(
							avatar_set=AvatarSet(avatar_id=avatar_sent_mem.event_data["avatar_id"]))
						logger.info(f"SENT AVATAR SET EVENT")
						avatar_sent_mem = None
					# logger.info(f"{frame.width}x{frame.height}, {len(frame.data)}")
					yield RenderResponse(video=VideoChunk(data=frame.data, width=frame.width, height=frame.height, frame_idx=frame_idx))
					if first_frame_time is None:
						first_frame_time = time.perf_counter()
					realtime_metric = (time.perf_counter() - first_frame_time) - frame_idx * every_frame_time
					if frame_idx % 25 == 0:
						logger.info(f"SENT {frame_idx}, realtime_metric: {realtime_metric}")
					
					frame_idx += 1
				# logger.info(f"END GRPC YIELDING {chunk_counter}")
				# start_time = cur_time
				# first_chunk_flag = True
			except RpcError as e:
				logger.error(f"GOT RPCERROR EXCEPTION IN RENDER STREAM {str(e)} START CLOSING PROCESS")
			except Exception as e:
				logger.error(f"GOT EXCEPTION IN RENDER STREAM {str(e)} START CLOSING PROCESS")
			except SystemExit as e:
				raise e
			except BaseException as e:
				logger.error(f"GOT BASEEXCEPTION IN RENDER STREAM {str(e)} START CLOSING PROCESS")
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
