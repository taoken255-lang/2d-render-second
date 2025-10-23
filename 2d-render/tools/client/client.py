import grpc
from grpc import ssl_channel_credentials
from proto.render_service_pb2_grpc import RenderServiceStub
from proto.render_service_pb2 import RenderRequest, ImageChunk, AudioChunk, PlayAnimation, SetAvatar, SetEmotion, InfoRequest
from loguru import logger
import wave
from PIL import Image
import time
import numpy as np


def video_request(audio_file):
	with wave.open(audio_file, 'rb') as wf:
		yield RenderRequest(set_avatar=SetAvatar(avatar_id="ermakova"), online=True, alpha=False, output_format="RGB")
		logger.info("AVATAR SENT")

		sample_rate = wf.getframerate()
		bps = 16
		audio_chunk = 1 * sample_rate
		logger.info(audio_chunk)
		logger.info((sample_rate, 16))
		anim_idx = 2
		cur_idx = 0
		while chunk := wf.readframes(audio_chunk):
			yield RenderRequest(audio=AudioChunk(data=chunk, sample_rate=sample_rate, bps=bps, is_voice=True))
			logger.info("AUD SENT")

			# if cur_idx == anim_idx:  # or cur_idx == anim_idx + 1:
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="idle2"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="idle"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="idle3"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="idle3"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="idle3"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="Idle_Drinking"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="Idle_Drinking"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="Idle_Drinking"))
				# yield RenderRequest(set_emotion=SetEmotion(emotion="angry"))
				# logger.info("EMOTION SENT")
				# yield RenderRequest(play_animation=PlayAnimation(animation="point_suit"))
			# 	# yield RenderRequest(play_animation=PlayAnimation(animation="talk_suit"))
			# 	# yield RenderRequest(play_animation=PlayAnimation(animation="idle"))
			# 	logger.info("sent play animation command")
			# elif cur_idx == anim_idx + 2:
			# 	yield RenderRequest(set_emotion=SetEmotion(emotion="happy"))
			# 	logger.info("EMOTION SENT")
			# elif cur_idx == anim_idx + 4:
			# 	yield RenderRequest(set_emotion=SetEmotion(emotion="sad"))
			# 	logger.info("EMOTION SENT")
			# elif cur_idx == anim_idx + 6:
			# 	yield RenderRequest(set_emotion=SetEmotion(emotion="angry"))
			# 	logger.info("EMOTION SENT")

			time.sleep(0.3)
			cur_idx += 1


def image_request(audio_file, image_file):
	with wave.open(audio_file, 'rb') as wf:
		logger.info("FILE OPENED")
		image_file_width = Image.open(image_file).width
		image_file_height = Image.open(image_file).height
		logger.info(image_file_width)
		logger.info(image_file_height)
		with open(image_file, "rb") as file:
			image_bytes = file.read()
			yield RenderRequest(image=ImageChunk(data=image_bytes, width=image_file_width, height=image_file_height),
			                    online=True, alpha=False, output_format="RGB")
		logger.info("IMG SENT")

		sample_rate = wf.getframerate()
		bps = 16
		audio_chunk = 1 * sample_rate
		logger.info(audio_chunk)
		logger.info((sample_rate, 16))
		while chunk := wf.readframes(audio_chunk):
			yield RenderRequest(audio=AudioChunk(data=chunk, sample_rate=sample_rate, bps=bps))
			logger.info("AUD SENT")
			time.sleep(0.3)


def video_run(audio_file, url):
	creds = ssl_channel_credentials()
	channel = grpc.secure_channel(url, credentials=creds, options=[
		('grpc.max_receive_message_length', 10 * 1024 * 1024),
		('grpc.max_send_message_length', 10 * 1024 * 1024),
	])
	stub = RenderServiceStub(channel)

	response_stream = stub.RenderStream(video_request(audio_file))
	img_idx = 0
	for idxa, response_chunk in enumerate(response_stream):
		logger.info(f"CHUNK {idxa}")
		if response_chunk.WhichOneof("chunk") == "video":
			logger.info(f"SAVE IMAGE {img_idx}")
			logger.info(img_idx)
			img = Image.frombytes("RGB", (response_chunk.video.width, response_chunk.video.height),
			                      response_chunk.video.data, "raw")
			img.save(f"tools/client/imgs/frame_{img_idx}.png")
			img_idx += 1
		elif response_chunk.WhichOneof("chunk") == "start_animation":
			logger.info(f"START ANIMATION {response_chunk.start_animation.animation_name}")
		elif response_chunk.WhichOneof("chunk") == "end_animation":
			logger.info(f"END ANIMATION {response_chunk.end_animation.animation_name}")
		elif response_chunk.WhichOneof("chunk") == "emotion_set":
			logger.info(f"EMOTION SET {response_chunk.emotion_set.emotion_name}")


def local_video_run(audio_file, port):
	channel = grpc.insecure_channel(f"localhost:{port}", options=[
		('grpc.max_receive_message_length', 10 * 1024 * 1024),
		('grpc.max_send_message_length', 10 * 1024 * 1024),
	])
	stub = RenderServiceStub(channel)
	response_stream = stub.RenderStream(video_request(audio_file))
	img_idx = 0
	for idxa, response_chunk in enumerate(response_stream):
		logger.info(f"CHUNK {idxa}")
		if response_chunk.WhichOneof("chunk") == "video":
			logger.info(f"SAVE IMAGE {img_idx}")
			logger.info(img_idx)
			img = Image.frombytes("RGB", (response_chunk.video.width, response_chunk.video.height),
			                      response_chunk.video.data, "raw")
			img.save(f"tools/client/imgs/frame_{img_idx}.png")
			img_idx += 1
		elif response_chunk.WhichOneof("chunk") == "start_animation":
			logger.info(f"START ANIMATION {response_chunk.start_animation.animation_name}")
		elif response_chunk.WhichOneof("chunk") == "end_animation":
			logger.info(f"END ANIMATION {response_chunk.end_animation.animation_name}")
		elif response_chunk.WhichOneof("chunk") == "emotion_set":
			logger.info(f"EMOTION SET {response_chunk.emotion_set.emotion_name}")


def image_run(audio_file, image_file, url):
	creds = ssl_channel_credentials()
	channel = grpc.secure_channel(url, credentials=creds, options=[
		('grpc.max_receive_message_length', 10 * 1024 * 1024),
		('grpc.max_send_message_length', 10 * 1024 * 1024),
	])
	stub = RenderServiceStub(channel)

	response_stream = stub.RenderStream(image_request(audio_file, image_file))
	img_idx = 0
	for idxa, response_chunk in enumerate(response_stream):
		logger.info(f"CHUNK {idxa}")
		if response_chunk.WhichOneof("chunk") == "video":
			logger.info(f"SAVE IMAGE {img_idx}")
			logger.info(img_idx)
			img = Image.frombytes("RGB", (response_chunk.video.width, response_chunk.video.height),
			                      response_chunk.video.data, "raw")
			img.save(f"tools/client/imgs/frame_{img_idx}.png")
			img_idx += 1
		elif response_chunk.WhichOneof("chunk") == "start_animation":
			logger.info(f"START ANIMATION {response_chunk.start_animation.animation_name}")
		elif response_chunk.WhichOneof("chunk") == "end_animation":
			logger.info(f"END ANIMATION {response_chunk.end_animation.animation_name}")
		elif response_chunk.WhichOneof("chunk") == "emotion_set":
			logger.info(f"EMOTION SET {response_chunk.emotion_set.emotion_name}")


def local_image_run(audio_file, image_file, port):
	channel = grpc.insecure_channel(f"localhost:{port}", options=[
		('grpc.max_receive_message_length', 10 * 1024 * 1024),
		('grpc.max_send_message_length', 10 * 1024 * 1024),
	])
	stub = RenderServiceStub(channel)
	response_stream = stub.RenderStream(image_request(audio_file, image_file))
	img_idx = 0
	for idxa, response_chunk in enumerate(response_stream):
		logger.info(f"CHUNK {idxa}")
		if response_chunk.WhichOneof("chunk") == "video":
			logger.info(f"SAVE IMAGE {img_idx}")
			logger.info(img_idx)
			img = Image.frombytes("RGB", (response_chunk.video.width, response_chunk.video.height),
			                      response_chunk.video.data, "raw")
			img.save(f"tools/client/imgs/frame_{img_idx}.png")
			img_idx += 1
		elif response_chunk.WhichOneof("chunk") == "start_animation":
			logger.info(f"START ANIMATION {response_chunk.start_animation.animation_name}")
		elif response_chunk.WhichOneof("chunk") == "end_animation":
			logger.info(f"END ANIMATION {response_chunk.end_animation.animation_name}")
		elif response_chunk.WhichOneof("chunk") == "emotion_set":
			logger.info(f"EMOTION SET {response_chunk.emotion_set.emotion_name}")


def info(url):
	creds = ssl_channel_credentials()
	channel = grpc.secure_channel(url, credentials=creds, options=[
		('grpc.max_receive_message_length', 10 * 1024 * 1024),
		('grpc.max_send_message_length', 10 * 1024 * 1024),
	])
	stub = RenderServiceStub(channel)
	info_response = stub.InfoRouter(InfoRequest())
	avatars = info_response.animations
	for avatar in avatars:
		logger.info(f"{str(avatar)}: {info_response.animations[avatar].items}")
		logger.info(f"{str(avatar)}: {info_response.emotions[avatar].items}")


if __name__ == '__main__':

	"""
	python -m tools.client.client
	"""

	url = "2d-dev.digitalavatars.ru"
	aud_file = "tools/client/res/blue_woman_3.2.wav"
	img_file = "tools/client/res/test1.png"
	port = "8500"
	# info(url)
	# local_video_run(aud_file, port)
	# video_run(aud_file, url)
	# local_image_run(aud_file, img_file, port)
	image_run(aud_file, img_file, url)
