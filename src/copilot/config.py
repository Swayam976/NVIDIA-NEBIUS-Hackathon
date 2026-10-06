"""Loads configuration from the environment (.env in dev)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parents[2]
MEMORY_DIR = REPO_ROOT / "memory" / "projects"


def _env(name: str, default: str = "") -> str:
    """Env value with surrounding whitespace removed. Pasted secrets often
    carry a trailing newline, which httpx rejects in the auth header and the
    OpenAI SDK then reports as a vague "Connection error"."""
    return os.getenv(name, "").strip() or default


@dataclass(frozen=True)
class Settings:
    nebius_api_key: str = field(default_factory=lambda: _env("NEBIUS_API_KEY"))
    nebius_base_url: str = field(
        default_factory=lambda: _env("NEBIUS_BASE_URL", "https://api.tokenfactory.us-central1.nebius.com/v1/")
    )
    nebius_model: str = field(
        default_factory=lambda: _env("NEBIUS_MODEL", "nvidia/nemotron-3-super-120b-a12b")
    )

    project_repo_paths: dict = field(
        default_factory=lambda: {
            "riscv-core": os.getenv("RISCV_REPO_PATH", "./repos/riscv-core"),
            "simt-gpu-core": os.getenv("SIMT_GPU_CORE_PATH", "./repos/simt-gpu-core"),
            "micro-npu": os.getenv("NPU_REPO_PATH", "./repos/micro-npu"),
            "mxint8-gemm": os.getenv("GEMM_REPO_PATH", "./repos/mxint8-gemm"),
        }
    )

    def validate(self) -> list[str]:
        """Returns a list of problems; empty list means we're good to go."""
        problems = []
        if not self.nebius_api_key:
            problems.append(
                "NEBIUS_API_KEY is not set. Copy .env.example to .env and fill it in."
            )
        return problems


settings = Settings()
