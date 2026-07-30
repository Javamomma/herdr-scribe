#!/usr/bin/env python3
"""scribe-transcribe.py — streaming speech-to-text worker for herdr-scribe.

Reads audio (or, in the `fake` test backend, plain text lines) from stdin and
appends recognized utterances to a transcript file, one line at a time:

    [<channel>] <recognized text>

Backend is selected via the SCRIBE_STT_BACKEND environment variable:
  - "fake"          — every stdin line is treated as one already-recognized
                       utterance. Used by the test suite; needs no model, no
                       microphone, no network.
  - "faster-whisper" (default) — lazy-imports the `faster_whisper` package and
                       streams raw audio chunks from stdin, transcribing
                       incrementally. This path is not exercised by the unit
                       test suite (it needs a real model + real audio) and is
                       meant to be smoke-tested on a live host.

Global constraint: this worker must NEVER open an audio file for writing.
Audio only ever exists in the stdin pipe / in-memory buffers; the only file
this script writes to is the text transcript.
"""

import argparse
import abc
import os
import sys


def load_glossary(path):
    """Read one hotword per line; ignore blank lines and '#' comments."""
    hotwords = []
    if not path:
        return hotwords
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            hotwords.append(line)
    return hotwords


class Backend(abc.ABC):
    """A pluggable speech-to-text backend.

    `run` consumes the process's stdin and, for each recognized utterance,
    calls `emit(text)`. Implementations decide how stdin is interpreted
    (text lines for the fake backend; raw PCM audio for a real ASR engine).
    """

    def __init__(self, hotwords):
        self.hotwords = hotwords

    @abc.abstractmethod
    def run(self, stdin, emit):
        """Consume `stdin`, calling `emit(text)` per recognized utterance."""
        raise NotImplementedError


class FakeBackend(Backend):
    """Test/dev backend: each stdin line is one recognized utterance."""

    def run(self, stdin, emit):
        if os.environ.get("SCRIBE_DEBUG") == "1":
            print(f"glossary:{len(self.hotwords)}", file=sys.stderr)
        for raw_line in stdin:
            text = raw_line.rstrip("\n")
            if text == "":
                continue
            emit(text)


class FasterWhisperBackend(Backend):
    """Real STT backend: streams raw audio chunks from stdin through
    faster-whisper, transcribing incrementally.

    Lazily imports faster_whisper so the fake-backend/test path never needs
    the dependency installed. Not exercised by the unit test suite — needs a
    real model and real audio; verify with a live smoke test.
    """

    # Chosen because it runs faster than real time; larger models fall
    # behind and drop live audio (see docs/DESIGN.md).
    MODEL_SIZE = "base.en"
    SAMPLE_RATE = 16000
    CHUNK_SECONDS = 5
    BYTES_PER_SAMPLE = 2  # 16-bit PCM, mono

    def __init__(self, hotwords):
        super().__init__(hotwords)
        self._model = None

    def _load_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(self.MODEL_SIZE)
        return self._model

    def _initial_prompt(self):
        return ", ".join(self.hotwords) if self.hotwords else None

    def run(self, stdin, emit):
        model = self._load_model()
        chunk_bytes = self.SAMPLE_RATE * self.BYTES_PER_SAMPLE * self.CHUNK_SECONDS
        raw_stdin = stdin.buffer if hasattr(stdin, "buffer") else stdin
        prompt = self._initial_prompt()

        import numpy as np

        while True:
            chunk = raw_stdin.read(chunk_bytes)
            if not chunk:
                break
            audio = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
            segments, _info = model.transcribe(
                audio,
                initial_prompt=prompt,
                hotwords=prompt,
            )
            for segment in segments:
                text = segment.text.strip()
                if text:
                    emit(text)


def make_backend(name, hotwords):
    if name == "fake":
        return FakeBackend(hotwords)
    if name == "faster-whisper":
        return FasterWhisperBackend(hotwords)
    raise ValueError(f"unknown SCRIBE_STT_BACKEND: {name!r}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Streaming STT worker for herdr-scribe.")
    parser.add_argument("--transcript", required=True, help="path to append transcript lines to")
    parser.add_argument("--channel", default="me", help="channel tag, e.g. me|them (default: me)")
    parser.add_argument("--glossary", default=None, help="path to a hotword glossary file")
    parser.add_argument(
        "--glossary-extra",
        default=None,
        help=(
            "path to an additional per-meeting hotword file; terms are "
            "additive on top of --glossary and de-duplicated"
        ),
    )
    args = parser.parse_args(argv)

    hotwords = list(
        dict.fromkeys(
            [*load_glossary(args.glossary), *load_glossary(args.glossary_extra)]
        )
    )
    backend_name = os.environ.get("SCRIBE_STT_BACKEND", "faster-whisper")
    backend = make_backend(backend_name, hotwords)

    # Ensure the transcript file exists even if no utterances are emitted.
    with open(args.transcript, "a", encoding="utf-8"):
        pass

    def emit(text):
        with open(args.transcript, "a", encoding="utf-8") as f:
            f.write(f"[{args.channel}] {text}\n")
            f.flush()

    backend.run(sys.stdin, emit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
