from enum import Enum


class IPCDataType(Enum):
	IMAGE = 1
	AUDIO = 2
	COMMAND = 3
	INTERRUPT = 4


class IPCObject:
	def __init__(self, data_type: IPCDataType, data: object):
		self.data_type = data_type
		self.data = data


class CommandDataType(Enum):
	SetAvatar = 1
	PlayAnimation = 2
	SetEmotion = 3
	ClearAnimations = 4


class CommandObject:
	def __init__(self, command_type: CommandDataType, command_data: str, additional_data: dict = {}):
		self.command_type = command_type
		self.command_data = command_data
		self.additional_data = additional_data


class ErrorDataType(Enum):
	Avatar = 1
	Animation = 2
	Handling = 3
	Initialization = 4

	def __str__(self):
		return self.name.lower()


class ErrorObject:
	def __init__(self, error_type: ErrorDataType, error_message: str):
		self.error_type = error_type
		self.error_message = error_message


class ImageObject:
	def __init__(self, data: bytes, height: int, width: int, is_muted: bool = False):
		self.data = data
		self.height = height
		self.width = width
		self.is_muted = is_muted


class AudioObject:
	def __init__(self, data: bytes, sample_rate: int, bps: int, is_voice: bool):
		self.data = data
		self.sample_rate = sample_rate
		self.bps = bps
		self.is_voice = is_voice


class InterruptObject:
	def __init__(self):
		...


class EventObject:
	def __init__(self, event_name, event_data):
		self.event_name = event_name
		self.event_data = event_data


class RenderAnimationObject:
	def __init__(self, render_data):
		self.render_data = render_data


class RenderEmotionObject:
	def __init__(self, render_data):
		self.render_data = render_data
