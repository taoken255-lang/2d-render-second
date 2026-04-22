from concurrent import futures
import multiprocessing
import os
import threading

import grpc
import torch
from loguru import logger

from config import Config
from logging_config import configure_logging
from proto import render_service_pb2_grpc
from service.streaming import StreamingService


configure_logging(Config.LOG_LEVEL, Config.LOG_JSON)


def grpc_service() -> None:
	# Avoid importing heavy alpha stack in spawned render worker processes.
	from service.offline_alpha import OfflineAlphaService

	alpha_service = OfflineAlphaService()
	server = grpc.server(
		futures.ThreadPoolExecutor(max_workers=10),
		options=[
			('grpc.keepalive_time_ms', 60000),
			('grpc.keepalive_timeout_ms', 30000),
			('grpc.keepalive_permit_without_calls', True),
			('grpc.http2.min_time_between_pings_ms', 30000),  # ВАЖНО!
			('grpc.http2.min_ping_interval_without_data_ms', 30000),  # ВАЖНО!
			('grpc.http2.max_pings_without_data', 0),  # Отключить лимит
			('grpc.max_receive_message_length', 10 * 1024 * 1024),
			('grpc.max_send_message_length', 10 * 1024 * 1024),
		],
	)
	render_service_pb2_grpc.add_RenderServiceServicer_to_server(
		StreamingService(alpha_service=alpha_service),
		server,
	)
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
		logger.error("CUDA is not available")
		os._exit(1)

	multiprocessing.set_start_method("spawn")
	serve()
