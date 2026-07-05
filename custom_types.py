from typing import TypedDict
from aiortc import RTCDataChannel

class Client(TypedDict):
    control_channel: RTCDataChannel | None
    throughput_channel: RTCDataChannel | None
    latency_channel: RTCDataChannel | None
    t0_latency: int | None
    t1_latency: int | None
    t0_loaded_latency: int | None
    t1_loaded_latency: int | None
    t0_throughput: int | float | None
    t1_throughput: int | float | None
    qtd_packages: int
    qtd_total_bytes: int


class Server(TypedDict):
    channels: dict[str, RTCDataChannel]
    t0_latency: int | None
    t1_latency: int | None
    t0_loaded_latency: int | None
    t1_loaded_latency: int | None
    t0_throughput: int | float | None
    t1_throughput: int | float | None
    qtd_packages: int
    qtd_total_bytes: int


class Peer(TypedDict):
    role: str | None
    target: str | None
    status: str | None
    sid: str | None


class Results(TypedDict):
    role: str | None
    sid: str | None
    latency: float | None
    loaded_latency_upload: float | None
    loaded_latency_download: float | None
    upload: float | None
    download: float | None
    test_size: int | None
    package_loss: float | None