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


#: Decode settings both engines share. condition_on_previous_text=False is the
#: load-bearing one: with it on, Whisper feeds its own output back in and falls
#: into repetition loops on long dictation. The three thresholds are Whisper's
#: own defaults, pinned here so a library update cannot quietly change them.
DECODE_OPTIONS = {
    "condition_on_previous_text": False,
    "compression_ratio_threshold": 2.4,
    "logprob_threshold": -1.0,
    "no_speech_threshold": 0.6,
}

#: Vocabulary bias. Lives next to the launcher, not inside the package.
PROMPT_PATH = Path(__file__).resolve().parent.parent / "initial_prompt.md"

#: Whisper truncates initial_prompt to the last (n_text_ctx // 2 - 1) tokens —
#: 223 for every checkpoint — and drops the overflow off the FRONT, silently.
PROMPT_TOKEN_BUDGET = 223

_prompt = None
_prompt_loaded = False


def initial_prompt():
    """The contents of initial_prompt.md, or None. Read once."""
    global _prompt, _prompt_loaded
    if _prompt_loaded:
        return _prompt
    _prompt_loaded = True
    try:
        text = PROMPT_PATH.read_text(encoding="utf-8").strip()
        _prompt = text or None
        if _prompt:
            log(f"Loaded initial prompt ({len(_prompt)} chars) from {PROMPT_PATH.name}")
    except FileNotFoundError:
        log("No initial_prompt.md — running without vocabulary bias")
    except Exception as e:
        log(f"Could not read {PROMPT_PATH}: {e}")
    return _prompt


def report_prompt_budget(language):
    """Say what an over-long prompt is costing, instead of losing it silently.

    A prompt that grew past the cap keeps working, just without its opening —
    which looks exactly like the bias not working at all.
    """
    prompt = initial_prompt()
    if not prompt:
        return
    try:
        import whisper.tokenizer
        tok = whisper.tokenizer.get_tokenizer(
            multilingual=True, language=language, task="transcribe")
        toks = tok.encode(" " + prompt)
    except Exception as e:
        log(f"Could not measure the initial prompt ({e})")
        return
    if len(toks) <= PROMPT_TOKEN_BUDGET:
        log(f"Initial prompt: {len(toks)}/{PROMPT_TOKEN_BUDGET} tokens.")
        return
    over = len(toks) - PROMPT_TOKEN_BUDGET
    dropped = tok.decode(toks[:over]).strip()
    log(f"WARNING: initial prompt is {len(toks)} tokens but Whisper keeps only "
        f"the last {PROMPT_TOKEN_BUDGET}. The first {over} tokens are dropped:")
    log(f"  DROPPED -> {dropped[:220]}{'...' if len(dropped) > 220 else ''}")
    log(f"  Shorten {PROMPT_PATH.name} to keep the opening context.")


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
        #: Overridable via the "fp16" config key — App sets it after
        #: construction. Half precision is right on Turing and newer, but
        #: Pascal (sm_61) runs fp16 math at 1/64 rate, where fp32 can win.
        self.fp16 = self.device == "cuda"
        log(f"Torch device: {self.device}")
        if self.device == "cuda":
            try:
                p = torch.cuda.get_device_properties(0)
                log(f"GPU: {p.name} (sm_{p.major}{p.minor}, "
                    f"{p.total_memory / 1024 ** 3:.1f} GB)")
                if p.major < 7:
                    log(f'Note: sm_{p.major}{p.minor} has no tensor cores. '
                        f'Try "fp16": false in config.json and compare.')
            except Exception:
                pass

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

    def free_cache(self):
        """Release intermediate tensors and PyTorch's caching allocator.

        Run after every transcription, not just on unload: days of dictation
        otherwise accumulate gigabytes of allocator fragmentation.
        """
        try:
            import gc
            import torch
            gc.collect()
            if self.device == "cuda":
                torch.cuda.empty_cache()
        except Exception:
            pass

    def unload(self):
        if self._model is None:
            return
        self._model = None
        self.free_cache()
        log("Model released (idle)")

    def transcribe(self, audio_bytes, language):
        # Bind the model once: an idle unload or a model switch could rebind
        # self._model while this runs, and the run must finish on the model it
        # started with.
        model = self._model
        if model is None:
            raise RuntimeError("no model is loaded")
        audio_np = pcm_to_float32(audio_bytes)
        result = self._whisper.transcribe(model, audio_np,
                                          language=language,
                                          initial_prompt=initial_prompt(),
                                          fp16=self.fp16,
                                          **DECODE_OPTIONS)
        text = result["text"].strip()
        del result, audio_np, model
        self.free_cache()
        return text


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
            initial_prompt=initial_prompt(),
            **DECODE_OPTIONS,
        )
        return result["text"].strip()


def create_engine():
    return MlxEngine() if IS_MAC else WhisperEngine()
