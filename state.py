import asyncio
from custom_types import Client, Server, Peer, Results

class PeerState:
    def __init__(self) -> None:
        self.sid: str | None = None
        self.role: str | None = None
        
        self.client: Client = {
            "control_channel": None,
            "throughput_channel": None,
            "latency_channel": None,
            "t0_latency": None,
            "t1_latency": None,
            "t0_throughput": None,
            "t1_throughput": None,
            "qtd_packages": 0,
            "qtd_total_bytes": 0
        }

        self.server: Server = {
            "channels": {},
            "t0_latency": None,
            "t1_latency": None,
            "t0_throughput": None,
            "t1_throughput": None,
            "qtd_packages": 0,
            "qtd_total_bytes": 0
        }

        self.results: Results = {
            "role": None,
            "sid": None,
            "latency": None,
            "upload": None,
            "download": None
        }

        self.events = {
            "lat_ack_received": asyncio.Event(),
            "ack_received": asyncio.Event(),
            "lat_ack_error": asyncio.Event(),
            "upload_error": asyncio.Event(),
            "upload_received": asyncio.Event(),
            "start_server_throughput": asyncio.Event(),
            "latency_finished": asyncio.Event(),
            "throughput_finished": asyncio.Event()
        }

state = PeerState()