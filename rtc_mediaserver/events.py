import enum

from rtc_mediaserver.webrtc_server.constants import CAN_SEND_FRAMES, AVATAR_SET


class ServiceEvents(enum.Enum):
    INTERRUPT = enum.auto()
    EOS = enum.auto()
    SET_ANIMATION = enum.auto()
    SET_EMOTION = enum.auto()


class Conditions:
    @classmethod
    async def can_process_frames(cls):
        await CAN_SEND_FRAMES.wait()
        await AVATAR_SET.wait()
