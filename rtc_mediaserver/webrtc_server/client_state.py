from rtc_mediaserver.webrtc_server.constants import AUDIO_SETTINGS


class ClientState:
    """Per-connection state kept between websocket messages."""

    def __init__(self) -> None:
        # Raw PCM bytes buffer (mono int16 little-endian)
        self.pcm_buf = bytearray()
        # Chosen sample rate from the init message
        self.sample_rate: int = AUDIO_SETTINGS.sample_rate
        # Avatar identifier (not used yet)
        self.avatar_id: str | None = None
        # Session identifier (generated on init)
        self.session_id: str | None = None

    # Helper -----------------------------------------------------------
    def _bytes_per_chunk(self) -> int:
        """Return bytes in one configured audio chunk."""
        from .constants import AUDIO_SETTINGS
        return AUDIO_SETTINGS.samples_per_chunk * 2