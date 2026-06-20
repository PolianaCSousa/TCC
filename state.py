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
            "package_loss_channel": None,
            "t0_latency": [],
            "t1_latency": [],
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
            "t0_throughput": None,
            "t1_throughput": None,
            "received_packages": 0, 
            "qtd_packages": 0,
            "qtd_total_bytes": 0
        }

        self.results: Results = {
            "role": None,
            "sid": None,
            "latency": None,
            "upload": None,
            "download": None,
            "test_size": None,
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
        }
    
    def reset_for_test(self):
        self.results["upload"] = None
        self.results["download"] = None
        self.results["test_size"] = None
        
        self.events["upload_received"].clear()
        self.events["start_server_upload"].clear()
        self.events["throughput_finished"].clear()
        self.events["test_complete"].clear()
        self.events["upload_error"].clear()    


state = PeerState()