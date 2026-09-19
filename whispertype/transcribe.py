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
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import API_KEY_PATH
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

#: What openai-whisper downloads into, and the sizes it reports.
WHISPER_CACHE = Path.home() / ".cache" / "whisper"
WHISPER_SIZES = {
    "large-v3-turbo": "809 MB", "large-v3": "1.5 GB", "large-v2": "1.5 GB",
    "medium": "769 MB", "small": "244 MB", "base": "74 MB", "tiny": "39 MB",
}


#: Module functions rather than engine methods because the tray asks these
#: questions from the UI thread, and once the engine lives in another process
#: the answer must not depend on that process being alive or idle. Both are
#: pure filesystem checks, so nothing is lost by answering locally.
def local_is_downloaded(name):
    return (WHISPER_CACHE / f"{name}.pt").exists()


def local_catalog():
    return [ModelInfo(n, WHISPER_SIZES[n], local_is_downloaded(n))
            for n in _MODEL_ORDER]


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
    # Same tokenizer, two packagings: openai-whisper on Windows, mlx-whisper on
    # macOS. Only one of them is ever installed.
    get_tokenizer = None
    for module in ("whisper.tokenizer", "mlx_whisper.tokenizer"):
        try:
            get_tokenizer = __import__(module, fromlist=["get_tokenizer"]).get_tokenizer
            break
        except ImportError:
            continue
    if get_tokenizer is None:
        return          # hosted API engine: no local tokenizer, nothing to say
    try:
        tok = get_tokenizer(multilingual=True, language=language,
                            task="transcribe")
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

    SIZES = WHISPER_SIZES

    def __init__(self):
        import torch
        import whisper
        self._whisper = whisper
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self._model = None
        self._name = None
        self._bench_model = None
        self._cache = WHISPER_CACHE
        #: Overridable via the "fp16" config key — App sets it after
        #: construction. Half precision is right on Turing and newer, but
        #: Pascal (sm_61) runs fp16 math at 1/64 rate, where fp32 wins. The
        #: default now follows the hardware instead of only warning about it:
        #: a note in a log file nobody reads is not a default.
        self.fp16 = self.device == "cuda"
        log(f"Torch device: {self.device}")
        if self.device == "cuda":
            try:
                p = torch.cuda.get_device_properties(0)
                log(f"GPU: {p.name} (sm_{p.major}{p.minor}, "
                    f"{p.total_memory / 1024 ** 3:.1f} GB)")
                if p.major < 7:
                    self.fp16 = False
                    log(f"sm_{p.major}{p.minor} has no tensor cores and runs "
                        f"fp16 at 1/64 rate — defaulting to fp32. Override "
                        f'with "fp16": true in config.json.')
            except Exception:
                pass

    @property
    def device_label(self):
        return self.device.upper()

    def catalog(self):
        return local_catalog()

    def is_downloaded(self, name):
        return local_is_downloaded(name)

    def load(self, name):
        log(f"Loading {name} on {self.device}...")
        t0 = time.perf_counter()
        self._model = self._build(name)
        self._name = name
        log(f"{name} ready on {self.device} ({time.perf_counter() - t0:.1f}s).")

    def _build(self, name):
        """whisper.load_model(name, "cuda"), without its detours through host
        memory.

        load_model puts the fp16 checkpoint on the GPU, builds an fp32 model on
        the CPU, copies the checkpoint into it and only then moves the model to
        the GPU. Every one of those copies is charged to the Windows commit
        limit — VRAM included: the 3.2 GB of fp32 weights on the card show up
        as 3.2 GB of this process's commit. Here the checkpoint is memory-mapped
        on the CPU and the model is built directly on the GPU. Measured on the
        GTX 1070 Ti with large-v3-turbo, through this engine, on a 101-second
        dictation that decoded to the identical transcript both ways:

                          peak commit   commit once loaded   load time
            load_model      10150 MB         10042 MB          6.4 s
            this             6549 MB          4947 MB          3.7 s

        After the first decode both settle at about 5.2 GB — free_cache drops
        what load_model left behind — so the saving is the load, and the
        stretch between it and the first dictation. The load is what failed. Every "CUDA out of memory" with gigabytes
        still free on the card, and every ACCESS_VIOLATION in the middle of a
        load, on 2026-09-11, 09-18 and 09-19, landed within seconds of a
        Windows low-virtual-memory event, with the machine's commit charge at
        99.8% of its limit. Built this way the model needs 3.5 GB less of it.
        """
        w = self._whisper
        url = getattr(w, "_MODELS", {}).get(name)
        heads = getattr(w, "_ALIGNMENT_HEADS", {}).get(name)
        if self.device != "cuda" or url is None or heads is None:
            return w.load_model(name, device=self.device)
        import torch
        from whisper.model import ModelDimensions, Whisper
        # The same download and SHA-256 check load_model runs.
        path = w._download(url, str(WHISPER_CACHE), False)
        ckpt = torch.load(path, map_location="cpu", mmap=True,
                          weights_only=True)
        with torch.device(self.device):
            model = Whisper(ModelDimensions(**ckpt["dims"]))
        model.load_state_dict(ckpt["model_state_dict"])
        del ckpt
        model.set_alignment_heads(heads)
        # The alignment-heads buffer is built on the CPU; this moves it.
        return model.to(self.device)

    def predownload(self, name):
        """Fetch a model into the cache without touching the GPU."""
        m = self._whisper.load_model(name, device="cpu")
        del m
        self.free_cache()

    def load_for_benchmark(self, name):
        """Load a model in isolation and report how long it took.

        Deliberately not stored in self._model: the benchmark must not leave a
        different model installed as the dictation model if it is interrupted.

        Returns a *handle* (the name) rather than the model object, so the same
        call still works once the engine runs in another process, where a torch
        module cannot cross the boundary. See engine_proc.
        """
        t0 = time.perf_counter()
        self._bench_model = self._build(name)
        return name, time.perf_counter() - t0

    def release_benchmark_model(self):
        """Drop the benchmark model. Called in a finally, so it also runs when
        the load or the decode raised and left one partially pinned."""
        self._bench_model = None
        self.free_cache()

    def transcribe_with(self, handle, audio_bytes, language):
        """Transcribe on the model load_for_benchmark put in place.

        Uses the same decode options as transcribe(), so what the benchmark
        measures is the decode the user actually dictates with.
        """
        model = self._bench_model
        if model is None:
            raise RuntimeError(f"no benchmark model is loaded for {handle}")
        audio_np = pcm_to_float32(audio_bytes)
        result = self._whisper.transcribe(model, audio_np,
                                          language=language,
                                          initial_prompt=initial_prompt(),
                                          fp16=self.fp16,
                                          **DECODE_OPTIONS)
        return result["text"].strip()

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
        # Not always "idle" — an engine switch unloads too, and labelling that
        # as an idle release made the log read like the watchdog had fired.
        log("Model released")

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
    # large-v3-turbo and tiny do not. Sizes are the real repo sizes, measured
    # from the HF tree API.
    #
    # `large-v3-turbo-q4` is the same model at 464 MB instead of 1.6 GB, which
    # also cuts the reload after an idle unload by the same factor. Measured on
    # 10.7s of Hungarian speech: identical transcript to the fp16 build, 0.75s
    # vs 0.74s. Worth being the default for most people.
    #
    # Deliberately not offered:
    #   * the `-8bit` and `-4bit` turbo repos — they ship `model.safetensors`,
    #     and mlx-whisper 0.4.3 only reads `weights.npz`, so loading one raises
    #     "[load_npz] Input must be a zip file". Verified, not assumed.
    #   * the `.en` and distil-whisper repos: English only.
    #   * `large-v3-mlx-4bit`: its repo carries both npz and safetensors, so it
    #     is barely smaller than the fp16 turbo.
    REPOS = {
        "large-v3-turbo":    ("mlx-community/whisper-large-v3-turbo",    "1.6 GB"),
        "large-v3-turbo-q4": ("mlx-community/whisper-large-v3-turbo-q4", "464 MB"),
        "large-v3":          ("mlx-community/whisper-large-v3-mlx",      "3.1 GB"),
        "large-v2":          ("mlx-community/whisper-large-v2-mlx",      "3.1 GB"),
        "medium":            ("mlx-community/whisper-medium-mlx",        "1.5 GB"),
        "small":             ("mlx-community/whisper-small-mlx",         "481 MB"),
        "base":              ("mlx-community/whisper-base-mlx",          "144 MB"),
        "tiny":              ("mlx-community/whisper-tiny",               "74 MB"),
    }

    #: Menu order. Separate from the Windows engine's list, which can only
    #: offer what openai-whisper ships.
    ORDER = ["large-v3-turbo", "large-v3-turbo-q4",
             "large-v3", "large-v2", "medium", "small", "base", "tiny"]

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
                for n in self.ORDER]

    def load(self, name):
        repo, _ = self.REPOS[name]
        log(f"Loading {name} ({repo}) on Metal...")
        # Downloads on first use; ModelHolder keeps exactly one model resident,
        # so switching models frees the previous one.
        self._holder.get_model(repo, self._mx.float16)
        self._repo = repo
        self._name = name
        log(f"{name} ready on Metal.")

    def predownload(self, name):
        """Fetch a model's weights into the HF cache.

        ModelHolder keeps exactly one model resident, so this necessarily
        displaces whatever is loaded; the caller reloads afterwards.
        """
        repo, _ = self.REPOS[name]
        self._holder.get_model(repo, self._mx.float16)
        self._holder.model = None
        self._holder.model_path = None
        try:
            import gc
            gc.collect()
            self._mx.clear_cache()
        except Exception:
            pass

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


# ── OpenAI hosted API ────────────────────────────────────────────────────────

#: Used when /v1/models cannot be reached. The live list is preferred so new
#: models appear without a release here.
OPENAI_FALLBACK_MODELS = [
    "gpt-4o-transcribe",
    "gpt-4o-mini-transcribe",
    "whisper-1",
]

#: What counts as a speech-to-text model in /v1/models. Without a filter the
#: TTS models (tts-1*) leak into the same list.
#:
#: Matched on the *capability word* rather than on a family prefix. The old
#: pattern was `^(whisper-|gpt-4o.*transcribe)`, which pinned the list to one
#: generation: a future `gpt-5-transcribe` or `o4-transcribe` would simply not
#: appear in the menu, with nothing to explain why.
_OPENAI_STT_RE = re.compile(r"(^whisper-|transcrib)")

#: Excluded even when they match above.
#:
#: The gpt-realtime* family are websocket session models: POST
#: /v1/audio/transcriptions answers 404 "Invalid URL" for them, and offering a
#: menu entry that silently transcribes with something else is worse than not
#: offering it. _HTTP_FALLBACK still covers a config that names one by hand.
#: `tts` and `speech` catch the text-to-speech side, which also carries
#: audio-ish names.
_OPENAI_NOT_STT_RE = re.compile(r"(^gpt-realtime|tts|[-_]speech|^dall|embedding)")

#: Dated snapshots (…-2025-12-15) are reproducibility pins of the evergreen
#: base model; hiding them keeps the menu scannable.
_OPENAI_PINNED_RE = re.compile(r"-\d{4}(-\d{2}-\d{2})?$")

#: Realtime models are not served on POST /v1/audio/transcriptions — the API
#: answers 404 "Invalid URL" — so map them to the closest HTTP model.
_HTTP_FALLBACK = "gpt-4o-transcribe"


class OpenAiEngine:
    """OpenAI's hosted transcription API.

    The opposite trade from the local engines: nothing to download, no GPU and
    no model-load wait — but the audio leaves the machine and every clip costs
    money. Selected with "stt_engine": "openai".
    """

    def __init__(self, cfg):
        self._cfg = cfg
        self._name = cfg.openai_model
        self._models = None          # cached /v1/models result
        self._ready = False
        log(f"OpenAI API engine ({self._name}) at {cfg.openai_endpoint}")

    # ── Engine interface ──

    @property
    def device_label(self):
        return "OpenAI API"

    @property
    def loaded(self):
        return self._ready

    def is_downloaded(self, name):
        return True              # nothing is ever downloaded

    def predownload(self, name):
        pass

    def free_cache(self):
        pass

    def unload(self):
        # Nothing is resident; the idle watchdog has nothing to reclaim.
        self._ready = False

    def catalog(self):
        names = self._model_names()
        return [ModelInfo(n, "API", True) for n in names]

    def load(self, name):
        if not self._cfg.openai_api_key:
            raise RuntimeError(
                "No OpenAI API key. Set OPENAI_API_KEY, or write the key to "
                f"{API_KEY_PATH}, or use the tray menu.")
        if name not in self._model_names():
            log(f"{name} is not in the model list; using it anyway")
        self._name = name
        self._ready = True
        log(f"OpenAI model set to {name}")

    def transcribe(self, audio_bytes, language):
        if not self._ready:
            raise RuntimeError("OpenAI engine is not configured")
        return self._post(self._name, audio_bytes, language)

    # ── Benchmark hooks ──

    def load_for_benchmark(self, name):
        return name, 0.0         # no load step; the handle is just its name

    def release_benchmark_model(self):
        pass                     # nothing was ever resident

    def transcribe_with(self, model, audio_bytes, language):
        return self._post(model, audio_bytes, language)

    # ── Internals ──

    def _model_names(self):
        if self._models is not None:
            return self._models
        key = self._cfg.openai_api_key
        if not key:
            self._models = list(OPENAI_FALLBACK_MODELS)
            return self._models
        try:
            req = urllib.request.Request(
                f"{self._cfg.openai_endpoint}/models",
                headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.load(resp)
            names = sorted(
                m["id"] for m in data.get("data", [])
                if _OPENAI_STT_RE.search(m.get("id", ""))
                and not _OPENAI_NOT_STT_RE.search(m.get("id", ""))
                and not _OPENAI_PINNED_RE.search(m.get("id", "")))
            self._models = names or list(OPENAI_FALLBACK_MODELS)
            log(f"OpenAI STT models: {', '.join(self._models)}")
        except Exception as e:
            log(f"Could not list OpenAI models ({e}) — using the built-in list")
            self._models = list(OPENAI_FALLBACK_MODELS)
        return self._models

    def refresh_models(self):
        self._models = None
        return self._model_names()

    def _post(self, model, audio_bytes, language):
        # Realtime models only exist on the websocket surface.
        if model.startswith("gpt-realtime"):
            model = _HTTP_FALLBACK
        key = self._cfg.openai_api_key
        if not key:
            raise RuntimeError("No OpenAI API key configured")

        fields = [("model", model), ("language", language)]
        prompt = initial_prompt()
        if prompt:
            fields.append(("prompt", prompt))
        body, content_type = _multipart(
            fields, "file", "audio.wav", _pcm16_to_wav(audio_bytes, 16000))

        req = urllib.request.Request(
            f"{self._cfg.openai_endpoint}/audio/transcriptions",
            data=body, method="POST",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": content_type})
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            # Never let the key reach the log or the error surface.
            raise RuntimeError(f"OpenAI API {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"OpenAI API unreachable: {e.reason}") from None
        return (data.get("text") or "").strip()


def _pcm16_to_wav(pcm, rate):
    """Wrap raw mono 16-bit PCM in a WAV container — the API needs a file."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _multipart(fields, file_field, filename, file_bytes):
    """Minimal multipart/form-data encoder, to avoid a requests dependency."""
    boundary = "----WhisperType" + uuid.uuid4().hex
    out = bytearray()
    for name, value in fields:
        out += (f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n").encode("utf-8")
    out += (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{file_field}";'
            f' filename="{filename}"\r\n'
            f"Content-Type: audio/wav\r\n\r\n").encode("utf-8")
    out += file_bytes
    out += f"\r\n--{boundary}--\r\n".encode("utf-8")
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def create_engine(cfg=None):
    """Local GPU engine, or the hosted API when the config asks for it."""
    if cfg is not None and cfg.stt_engine == "openai":
        return OpenAiEngine(cfg)
    if IS_MAC:
        return MlxEngine()
    return _cuda_engine(cfg)


def _cuda_engine(cfg):
    """The CUDA engine, in a process of its own.

    A native fault inside the NVIDIA driver kills whichever process it happens
    in, and no Python-level handling can intervene — so the decoder does not
    share this one. `"isolate_decoder": false` puts it back in-process, which
    is useful under a debugger and is what happens anyway if the child cannot
    be started.

    macOS is deliberately not covered: MLX has not shown this failure, and its
    thread-affinity rules make the in-process path the tested one there.
    """
    if cfg is not None and not cfg.get("isolate_decoder", True):
        log("Decoder isolation is off — a driver crash will take the daemon "
            "down with it")
        return WhisperEngine()
    from .engine_proc import RemoteWhisperEngine
    try:
        return RemoteWhisperEngine()
    except Exception as e:
        log(f"Could not start the decoder process ({e}) — decoding in this "
            f"process instead. A driver crash will take the daemon with it "
            f"again, though the spooled audio still survives one.")
        return WhisperEngine()
