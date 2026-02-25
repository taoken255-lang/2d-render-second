from proto import render_service_pb2_grpc
from concurrent import futures
from config import Config
from loguru import logger
import multiprocessing
import threading
import torch
import grpc
import sys
import os

from service.streaming import StreamingService
from service.offline_alpha import OfflineAlphaService

logger.configure(extra={"request_id": "START SERVER"})  # Устанавливаем request_id по умолчанию
logger.remove()

if Config.LOG_LEVEL == "INFO":
	logger.add(
		sys.stdout,
		format="{time} | {level} | {extra[request_id]} | {message}",
		level="INFO"
	)
elif Config.LOG_LEVEL == "DEBUG":
	logger.add(
		sys.stdout,
		format="{time} | {level} | {extra[request_id]} | {message}",
		level="DEBUG"
	)
elif Config.LOG_LEVEL == "TRACE":
	logger.add(
		sys.stdout,
		format="{time} | {level} | {extra[request_id]} | {message}",
		level="TRACE"
	)
elif Config.LOG_LEVEL == "WARNING":
	logger.add(
		sys.stdout,
		format="{time} | {level} | {extra[request_id]} | {message}",
		level="WARNING"
	)
elif Config.LOG_LEVEL == "ERROR":
	logger.add(
		sys.stdout,
		format="{time} | {level} | {extra[request_id]} | {name}:{function}:{line} - {message}:{exception}",
		level="ERROR",
		backtrace=True,
		diagnose=True
	)


def grpc_service() -> None:
	alpha_service = OfflineAlphaService()
	server = grpc.server(futures.ThreadPoolExecutor(max_workers=10),
	                     options=[
		                     ('grpc.keepalive_time_ms', 60000),
		                     ('grpc.keepalive_timeout_ms', 30000),
		                     ('grpc.keepalive_permit_without_calls', True),
		                     ('grpc.http2.min_time_between_pings_ms', 30000),  # ВАЖНО!
		                     ('grpc.http2.min_ping_interval_without_data_ms', 30000),  # ВАЖНО!
		                     ('grpc.http2.max_pings_without_data', 0),  # Отключить лимит
		                     ('grpc.max_receive_message_length', 10 * 1024 * 1024),
		                     ('grpc.max_send_message_length', 10 * 1024 * 1024),
	                     ])
	render_service_pb2_grpc.add_RenderServiceServicer_to_server(StreamingService(alpha_service=alpha_service), server)
	server.add_insecure_port(f'[::]:{Config.RENDER_SERVICE_PORT}')
	server.start()
	logger.info(f"Server is running on port {Config.RENDER_SERVICE_PORT}...")
	server.wait_for_termination()


def serve() -> None:
	grpc_thread = threading.Thread(target=grpc_service)

	grpc_thread.start()

	grpc_thread.join()


if __name__ == '__main__':
	if torch.cuda.is_available():
		logger.info(f"CUDA is available: {torch.cuda.get_device_name(0)}")
	else:
		logger.exception(f"CUDA is not available")
		os._exit(1)

	multiprocessing.set_start_method("spawn")
	serve()
