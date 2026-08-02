"""Whisper backends.

Windows keeps openai-whisper on CUDA. macOS uses mlx-whisper (Apple MLX /
Metal) — openai-whisper cannot run on MPS at all: moving the sparse
`alignment_heads` buffer hits `aten::_sparse_coo_tensor_with_dims_and_tensors`,
which the SparseMPS backend does not implement and PYTORCH_ENABLE_MPS_FALLBACK
does not cover.

THREADING: MLX is thread-affine — the stream that loads a model must be the one
that evaluates it. Every method here must therefore be called from the single
transcription worker thread, including the initial load and any model switch.
"""
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .log import log

IS_MAC = sys.platform == "darwin"


@dataclass
class ModelInfo:
    name: str
    size_label: str
    downloaded: bool


# Ordered best-first, same list the tray menu showed on Windows.
_MODEL_ORDER = ["large-v3-turbo", "large-v3", "large-v2",
                "medium", "small", "base", "tiny"]


def pcm_to_float32(audio_bytes):
    """Raw 16-bit PCM -> float32 in [-1, 1]. Bypasses ffmpeg entirely."""
    return np.frombuffer(audio_bytes, np.int16).astype(np.float32) / 32768.0


# ── Windows / CUDA ───────────────────────────────────────────────────────────

class WhisperEngine:
    """openai-whisper + torch (CUDA when present)."""

    SIZES = {
        "large-v3-turbo": "809 MB", "large-v3": "1.5 GB", "large-v2": "1.5 GB",
        "medium": "769 MB", "small": "244 MB", "base": "74 MB", "tiny": "39 MB",
    }

    def __init__(self):
        import torch
        import whisper
        self._whisper = whisper
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self._model = None
        self._name = None
        self._cache = Path.home() / ".cache" / "whisper"
        log(f"Torch device: {self.device}")

    @property
    def device_label(self):
        return self.device.upper()

    def catalog(self):
        return [ModelInfo(n, self.SIZES[n], (self._cache / f"{n}.pt").exists())
                for n in _MODEL_ORDER]

    def is_downloaded(self, name):
        return (self._cache / f"{name}.pt").exists()

    def load(self, name):
        log(f"Loading {name} on {self.device}...")
        self._model = self._whisper.load_model(name, device=self.device)
        self._name = name
        log(f"{name} ready on {self.device}.")

    @property
    def loaded(self):
        return self._model is not None

    def unload(self):
        if self._model is None:
            return
        self._model = None
        try:
            import gc
            import torch
            gc.collect()
            if self.device == "cuda":
                torch.cuda.empty_cache()
        except Exception:
            pass
        log("Model released (idle)")

    def transcribe(self, audio_bytes, language):
        audio_np = pcm_to_float32(audio_bytes)
        result = self._whisper.transcribe(self._model, audio_np,
                                          language=language,
                                          fp16=(self.device == "cuda"))
        return result["text"].strip()


# ── macOS / Apple MLX ────────────────────────────────────────────────────────

class MlxEngine:
    """mlx-whisper — runs on the Apple GPU via Metal."""

    # mlx-community repo names are not uniform: most carry an `-mlx` suffix,
    # large-v3-turbo and tiny do not. Sizes are the real repo sizes (fp32
    # weights, so roughly double the openai .pt files).
    REPOS = {
        "large-v3-turbo": ("mlx-community/whisper-large-v3-turbo", "1.6 GB"),
        "large-v3":       ("mlx-community/whisper-large-v3-mlx",   "3.1 GB"),
        "large-v2":       ("mlx-community/whisper-large-v2-mlx",   "3.1 GB"),
        "medium":         ("mlx-community/whisper-medium-mlx",     "1.5 GB"),
        "small":          ("mlx-community/whisper-small-mlx",      "481 MB"),
        "base":           ("mlx-community/whisper-base-mlx",       "144 MB"),
        "tiny":           ("mlx-community/whisper-tiny",            "74 MB"),
    }

    def __init__(self):
        import mlx.core as mx
        import mlx_whisper
        from mlx_whisper.transcribe import ModelHolder
        self._mx = mx
        self._mlx_whisper = mlx_whisper
        self._holder = ModelHolder
        self._repo = None
        self._name = None
        try:
            from huggingface_hub.constants import HF_HUB_CACHE
            self._hub = Path(HF_HUB_CACHE)
        except Exception:
            self._hub = Path.home() / ".cache" / "huggingface" / "hub"

    @property
    def device_label(self):
        return "Metal"

    def _snapshot_dir(self, repo):
        return self._hub / ("models--" + repo.replace("/", "--")) / "snapshots"

    def is_downloaded(self, name):
        repo, _ = self.REPOS[name]
        snaps = self._snapshot_dir(repo)
        if not snaps.is_dir():
            return False
        for snap in snaps.iterdir():
            if (snap / "config.json").exists() and (
                    (snap / "weights.safetensors").exists()
                    or (snap / "weights.npz").exists()
                    or (snap / "model.safetensors").exists()):
                return True
        return False

    def catalog(self):
        return [ModelInfo(n, self.REPOS[n][1], self.is_downloaded(n))
                for n in _MODEL_ORDER]

    def load(self, name):
        repo, _ = self.REPOS[name]
        log(f"Loading {name} ({repo}) on Metal...")
        # Downloads on first use; ModelHolder keeps exactly one model resident,
        # so switching models frees the previous one.
        self._holder.get_model(repo, self._mx.float16)
        self._repo = repo
        self._name = name
        log(f"{name} ready on Metal.")

    @property
    def loaded(self):
        return self._holder.model is not None

    def unload(self):
        """Drop the ~1.6 GB of resident weights.

        mlx_whisper keeps exactly one model in ModelHolder, so clearing both
        class attributes is what actually releases it; mx.clear_cache() then
        returns the Metal buffer pool to the system.
        """
        if self._holder.model is None:
            return
        self._holder.model = None
        self._holder.model_path = None
        try:
            import gc
            gc.collect()
            self._mx.clear_cache()
        except Exception:
            pass
        log("Model released (idle)")

    def transcribe(self, audio_bytes, language):
        if not self.loaded:                 # reloaded on demand after an unload
            self.load(self._name)
        audio_np = pcm_to_float32(audio_bytes)
        result = self._mlx_whisper.transcribe(
            audio_np,
            path_or_hf_repo=self._repo,
            language=language,   # never omit: None costs an extra encoder pass
            temperature=0.0,     # default retries up to 6x on noisy dictation
            fp16=True,
            verbose=None,
        )
        return result["text"].strip()


def create_engine():
    return MlxEngine() if IS_MAC else WhisperEngine()
