from importlib import import_module

# Deferred import to avoid heavy dependencies at package import time
app = import_module('rtc_mediaserver.webrtc_server.api').app
