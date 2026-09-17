from dataclasses import dataclass
from typing import Optional


@dataclass
class DownloadJob:
    id: int
    url: str
    label: str
    start_sec: float
    end_sec: float
    quality: str
    audio_only: bool
    output_path: str
    status: str = "Queued"
    error: Optional[str] = None
    progress: float = 0.0
