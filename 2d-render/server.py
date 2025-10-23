import grpc
import threading
from concurrent import futures
from proto import render_service_pb2_grpc
from config import Config
from loguru import logger
import torch
import sys

from service.streaming import StreamingService
from service.offline_alpha import OfflineAlphaService
import multiprocessing


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


# def cvt_custom_trt():
#     from ditto.scripts.cvt_onnx_to_trt import main as cvt_trt
#     onnx_dir = "./checkpoints/ditto_onnx"
#     trt_dir = "./checkpoints/ditto_trt_custom"
#     assert os.path.isdir(onnx_dir)
#     os.makedirs(trt_dir, exist_ok=True)
#     grid_sample_plugin_file = os.path.join(onnx_dir, "libgrid_sample_3d_plugin.so")
#     logger.info("START CONVERTING")
#     cvt_trt(onnx_dir, trt_dir, grid_sample_plugin_file)
#     logger.info("CONVERTED")


if __name__ == '__main__':
	# cvt_custom_trt()
	multiprocessing.set_start_method("spawn")
	# logger.info(torch.cuda.get_device_capability())
	serve()
