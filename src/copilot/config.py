"""Loads configuration from the environment (.env in dev)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parents[2]
MEMORY_DIR = REPO_ROOT / "memory" / "projects"


@dataclass(frozen=True)
class Settings:
    nebius_api_key: str = field(default_factory=lambda: os.getenv("NEBIUS_API_KEY", ""))
    nebius_base_url: str = field(
        default_factory=lambda: os.getenv("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com")
    )
    nebius_model: str = field(
        default_factory=lambda: os.getenv("NEBIUS_MODEL", "nvidia/nemotron-3-super")
    )

    project_repo_paths: dict = field(
        default_factory=lambda: {
            "riscv-simt-core": os.getenv("RISCV_REPO_PATH", "./repos/riscv-core"),
            "custom-isa": os.getenv("ISA_REPO_PATH", "./repos/custom-isa"),
            "micro-npu": os.getenv("NPU_REPO_PATH", "./repos/micro-npu"),
            "mxint8-gemm": os.getenv("GEMM_REPO_PATH", "./repos/mxint8-gemm"),
            "dsp-fpga": os.getenv("DSP_REPO_PATH", "./repos/dsp-fpga"),
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
