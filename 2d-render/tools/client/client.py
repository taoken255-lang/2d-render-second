import grpc
from grpc import ssl_channel_credentials
from proto.render_service_pb2_grpc import RenderServiceStub
from proto.render_service_pb2 import RenderRequest, ImageChunk, AudioChunk, PlayAnimation, SetAvatar, SetEmotion, InfoRequest
from loguru import logger
import wave
from PIL import Image
import time
import numpy as np


def video_request(audio_file, avatar_id):
	with wave.open(audio_file, 'rb') as wf:
		yield RenderRequest(set_avatar=SetAvatar(avatar_id=avatar_id), online=True, alpha=False, output_format="RGB")
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
				# yield RenderRequest(play_animation=PlayAnimation(animation="Info_FinanceInfographics"))
				# yield RenderRequest(play_animation=PlayAnimation(animation="Working_TypingV4"))
				# yield RenderRequest(play_animation=PlayAnimation(animation="Speaking_SmallTalkV1"))

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
			yield RenderRequest(audio=AudioChunk(data=chunk, sample_rate=sample_rate, bps=bps, is_voice=False))
			logger.info("AUD SENT")
			time.sleep(0.3)


def video_run(audio_file, avatar_id, url):
	creds = ssl_channel_credentials()
	channel = grpc.secure_channel(url, credentials=creds, options=[
		('grpc.max_receive_message_length', 10 * 1024 * 1024),
		('grpc.max_send_message_length', 10 * 1024 * 1024),
	])
	stub = RenderServiceStub(channel)

	response_stream = stub.RenderStream(video_request(audio_file, avatar_id))
	img_idx = 0
	for idxa, response_chunk in enumerate(response_stream):
		logger.info(f"CHUNK {idxa}")
		if response_chunk.WhichOneof("chunk") == "video":
			logger.info(f"SAVE IMAGE {img_idx}")
			logger.info(f"{response_chunk.video.width}x{response_chunk.video.height}")
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


def local_video_run(audio_file, avatar_id, port):
	channel = grpc.insecure_channel(f"localhost:{port}", options=[
		('grpc.max_receive_message_length', 10 * 1024 * 1024),
		('grpc.max_send_message_length', 10 * 1024 * 1024),
	])
	stub = RenderServiceStub(channel)
	response_stream = stub.RenderStream(video_request(audio_file, avatar_id))
	img_idx = 0
	for idxa, response_chunk in enumerate(response_stream):
		logger.info(f"CHUNK {idxa}")
		if response_chunk.WhichOneof("chunk") == "video":
			logger.info(f"SAVE IMAGE {img_idx}")
			logger.info(f"{response_chunk.video.width}x{response_chunk.video.height}")
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
		elif response_chunk.WhichOneof("chunk") == "request_error":
			logger.info(f"REQUEST ERROR: {response_chunk.request_error.error_type}, {response_chunk.request_error.error_message}")


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
	aud_file = "tools/client/res/ved_00.wav"
	img_file = "tools/client/res/gomer.png"
	avatar_id = "visper_sofia"
	port = "8503"
	# info(url)
	local_video_run(aud_file, avatar_id, port)
	# video_run(aud_file, avatar_id, url)
	# local_image_run(aud_file, img_file, port)
	# image_run(aud_file, img_file, url)
