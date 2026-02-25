import grpc
from grpc import ssl_channel_credentials
from proto.render_service_pb2_grpc import RenderServiceStub
from proto.render_service_pb2 import RenderRequest, ImageChunk, AudioChunk, PlayAnimation, SetAvatar, SetEmotion, InfoRequest
from loguru import logger
import base64
from moviepy import VideoFileClip, AudioFileClip
import wave
from PIL import Image
import time
import subprocess
import numpy as np
import cv2
import os


def video_request(audio_file, avatar_id):
	logger.info("start video request")
	with wave.open(audio_file, 'rb') as wf:
		logger.info("file opened")
		yield RenderRequest(set_avatar=SetAvatar(avatar_id=avatar_id, idle_name="idle"), online=False, alpha=False, output_format="RGB")
		logger.info("AVATAR SENT")

		sample_rate = wf.getframerate()
		bps = 16
		audio_chunk = 1 * sample_rate
		logger.info(audio_chunk)
		logger.info((sample_rate, 16))
		anim_idx = 0
		silence_frame = 1000000000000
		cur_idx = 0
		while chunk := wf.readframes(audio_chunk):
			if cur_idx > silence_frame:
				yield RenderRequest(audio=AudioChunk(data=chunk, sample_rate=sample_rate, bps=bps, is_voice=False))
			else:
				yield RenderRequest(audio=AudioChunk(data=chunk, sample_rate=sample_rate, bps=bps, is_voice=True))

			logger.info("AUD SENT")

			# if cur_idx == anim_idx:  # or cur_idx == anim_idx + 1:
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="talk2_chunk_11"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="talk2_chunk_12"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="talk2_chunk_13"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="talk2_chunk_14"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="talk2_chunk_15"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="talk2_chunk_16"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="talk2_chunk_17"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="talk2_chunk_18"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="talk2_chunk_19"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="talk2_chunk_20"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="talk2_chunk_21"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="talk2_chunk_22"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="talk2_chunk_23"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="talk2_chunk_24"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="talk2_chunk_25"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="talk2_chunk_26"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="talk2_chunk_27"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="talk2_chunk_28"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="talk2_chunk_29"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="talk2_chunk_30"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="talk2_chunk_31"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="talk2_chunk_32"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="talk2_chunk_33"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="talk2_chunk_34"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="talk2_chunk_35"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="talk2_chunk_36"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="talk2_chunk_37"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="talk2_chunk_38"))
			# 	yield RenderRequest(play_animation=PlayAnimation(animation="talk2_chunk_39"))

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
			logger.info((response_chunk.video.width, response_chunk.video.height))
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



def ffmpeg_saving(
    audio_file: str,
    audio_path: str,
    output_dir: str,
    avatar_id: str,
    port: str,
    online: bool = True,
    alpha: bool = False
):
    try:
        channel = grpc.insecure_channel(f"localhost:{port}", options=[
            ('grpc.max_receive_message_length', 10 * 1024 * 1024),
            ('grpc.max_send_message_length', 10 * 1024 * 1024),
        ])
        stub = RenderServiceStub(channel)
        responses = stub.RenderStream(video_request(audio_file, avatar_id))

        video_frames = []
        frame_count = 0
        frame_width = None
        frame_height = None

        for idxa, response in enumerate(responses):
            logger.info(f"CHUNK {idxa}")
            if response.WhichOneof("chunk") == "video":
                logger.info(f"{response.video.width}x{response.video.height}")
                if frame_width is None:
                    frame_width = response.video.width
                    frame_height = response.video.height
                video_frames.append(base64.b64encode(response.video.data).decode("utf-8"))
                frame_count += 1
                logger.info(f"  Received frame {frame_count} (size: {len(response.video.data)} bytes)")
            elif response.WhichOneof("chunk") == "start_animation":
                logger.info(f"START ANIMATION {response.start_animation.animation_name}")
            elif response.WhichOneof("chunk") == "end_animation":
                logger.info(f"END ANIMATION {response.end_animation.animation_name}")
            elif response.WhichOneof("chunk") == "emotion_set":
                logger.info(f"EMOTION SET {response.emotion_set.emotion_name}")

        if video_frames:
            output_file = f"{output_dir}/output_online{online}_alpha{alpha}.mp4"

            # Encode raw RGB/RGBA frames to MP4 with audio muxing using ffmpeg
            # Server sends RGB when output_format="mp4"
            create_video(video_frames, frame_width, frame_height, audio_path)
        else:
            print(f"✗ RenderStream failed: No video chunks received")
            return False

    except Exception as e:
        print(f"✗ RenderStream failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def create_video(frames, width, height, audio) -> str | None:
    try:
        video_path = "C:/Users/Georgiy/Desktop/Programs/Sber/2DAvatars/2DRender/tools/client/temp.mp4"
        final_path = "C:/Users/Georgiy/Desktop/Programs/Sber/2DAvatars/2DRender/tools/client/final.mp4"

        # Настройки видео
        fps = 25
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')

        # Создаем VideoWriter
        out = cv2.VideoWriter(video_path, fourcc, fps, (width, height))

        for frame_base64 in frames:
            # Декодируем base64
            raw_bytes = base64.b64decode(frame_base64)
            # Превращаем в numpy array и reshape
            frame_rgb = np.frombuffer(raw_bytes, dtype=np.uint8).reshape((height, width, 3))
            # Конвертируем RGB → BGR для OpenCV
            frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
            # Пишем кадр
            out.write(frame_bgr)

        out.release()

        # Добавляем аудио через moviepy
        video_clip = VideoFileClip(video_path)
        audio_clip = AudioFileClip(audio)
        final_video = video_clip.with_audio(audio_clip)
        final_video.write_videofile(final_path, codec='libx264', audio_codec='aac')

        # Закрываем клипы
        video_clip.close()
        audio_clip.close()
        final_video.close()

        # Удаляем промежуточный файл
        os.remove(video_path)

        return final_path

    except Exception as e:
        logger.info(str(e))
        return None


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
    aud_path = "tools/client/res/ved_00.wav"
    img_file = "tools/client/res/gomer.png"
    avatar_id = "ermakova_doc"
    output_dir = "C:/Users/Georgiy/Desktop/Programs/Sber/2DAvatars/2DRender/tools/client/"
    port = "8503"
    # info(url)
    # local_video_run(aud_file, avatar_id, port)
    ffmpeg_saving(audio_file=aud_file, audio_path=aud_path, avatar_id=avatar_id, output_dir=output_dir, port=port)
    # video_run(aud_file, avatar_id, url)
    # local_image_run(aud_file, img_file, port)
    # image_run(aud_file, img_file, url)
