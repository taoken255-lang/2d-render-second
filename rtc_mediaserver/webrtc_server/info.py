import logging

import grpc

import rtc_mediaserver
from rtc_mediaserver.config import settings
from rtc_mediaserver.proto.render_service_pb2_grpc import RenderServiceStub


def info() -> dict:
    if settings.grpc_secure_channel:
        creds = creds = grpc.ssl_channel_credentials()
        channel = grpc.aio.secure_channel(settings.grpc_server_url, credentials=creds, options=[
            ('grpc.max_receive_message_length', 10 * 1024 * 1024),
            ('grpc.max_send_message_length', 10 * 1024 * 1024),
        ])
    else:
        channel = grpc.insecure_channel(settings.grpc_server_url, options=[
            ('grpc.max_receive_message_length', 10 * 1024 * 1024),
            ('grpc.max_send_message_length', 10 * 1024 * 1024),
        ])
    stub = RenderServiceStub(channel)
    info_response = stub.InfoRouter(rtc_mediaserver.proto.render_service_pb2.InfoRequest())

    data = {}

    for i in info_response.animations:
        if i not in data:
            data[i] = {
                "animations": [],
                "emotions": []
            }
        data[i]["animations"].extend(info_response.animations[i].items)

    for i in info_response.emotions:
        if i not in data:
            data[i] = {
                "animations": [],
                "emotions": []
            }
        data[i]["emotions"].extend(info_response.emotions[i].items)

    return data
