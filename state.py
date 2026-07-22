import asyncio
from custom_types import Client, Server, Peer, Results
from constants import THROUGHPUT_LABELS

class PeerState:
    def __init__(self) -> None:
        self.sid: str | None = None
        self.role: str | None = None
        self.latency_type: str = "unloaded"

        self.client: Client = {
            "control_channel": None,
            "throughput_channel": None,
            "latency_channel": None,
            "package_loss_channel": None,
            "t0_latency": [],
            "t1_latency": [],
            "t0_loaded_latency": [],
            "t1_loaded_latency": [],
            "t0_throughput": None,
            "t1_throughput": None,
            "received_packages": 0,
            "qtd_packages": 0,
            "qtd_total_bytes": 0
        }

        self.server: Server = {
            "channels": {},
            "t0_latency": [],
            "t1_latency": [],
            "t0_loaded_latency": [],
            "t1_loaded_latency": [],
            "t0_throughput": None,
            "t1_throughput": None,
            "received_packages": 0, 
            "qtd_packages": 0,
            "qtd_total_bytes": 0
        }

        self.results: Results = {
            "role": None,
            "ip": None,
            "candidate_type": None,
            "latency": None,
            "jitter": None,

            "100KB_upload": None,
            "100KB_download": None,
            "100KB_loaded_latency": None,
            "100KB_loaded_jitter": None,

            "1MB_upload": None,
            "1MB_download": None,
            "1MB_loaded_latency": None,
            "1MB_loaded_jitter": None,

            "10MB_upload": None,
            "10MB_download": None,
            "10MB_loaded_latency": None,
            "10MB_loaded_jitter": None,

            "100MB_upload": None,
            "100MB_download": None,
            "100MB_loaded_latency": None,
            "100MB_loaded_jitter": None,

            "package_loss": None,
        }

        self.events = {
            "lat_ack_received": asyncio.Event(),
            "ack_received": asyncio.Event(),
            "lat_ack_error": asyncio.Event(),
            "upload_error": asyncio.Event(),
            "upload_received": asyncio.Event(),
            "start_server_upload": asyncio.Event(),
            "latency_finished": asyncio.Event(),
            "throughput_finished": asyncio.Event(),
            "test_complete": asyncio.Event(),
            "end_iteration": asyncio.Event(),
            "end_throughput_experiments": asyncio.Event(),
            "loaded_latency_finished": asyncio.Event(),
            "package_loss_received": asyncio.Event(),
            "ack_package_loss_received": asyncio.Event(),
            "throughput_buffer_drained": asyncio.Event(),
        }
    
    def t0_latency_key(self):
        return "t0_loaded_latency" if self.latency_type == "loaded" else "t0_latency"

    def t1_latency_key(self):
        return "t1_loaded_latency" if self.latency_type == "loaded" else "t1_latency"

    def reset_loaded_latency(self, peer):
        peer["t0_loaded_latency"].clear()
        peer["t1_loaded_latency"].clear()

    def reset_results(self):
        for key in self.results:
            if key not in ("role", "ip", "candidate_type"):
                self.results[key] = None

    def reset_for_test(self):
        self.events["upload_received"].clear()
        self.events["start_server_upload"].clear()
        self.events["throughput_finished"].clear()
        self.events["test_complete"].clear()
        self.events["upload_error"].clear()


state = PeerState()