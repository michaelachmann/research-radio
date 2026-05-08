"""
ElevenLabs TTS - Multi-speaker podcast audio via Text-to-Dialogue API (Eleven v3).

The /v1/text-to-dialogue endpoint accepts a list of {text, voice_id} pairs and
returns audio bytes directly. Hard limit: 2,000 total chars per request.
Long scripts are chunked at CHARS_PER_REQUEST and concatenated with ffmpeg.
"""

import os
import subprocess
from typing import Optional

from elevenlabs import ElevenLabs
from elevenlabs.types import DialogueInput

from config import ELEVENLABS_API_KEY, ELEVENLABS_HOST_VOICE_ID, ELEVENLABS_COHOST_VOICE_ID


class ElevenLabsTTS:
    """Text-to-Dialogue TTS for two-host podcast scripts."""

    MODEL = "eleven_v3"
    CHARS_PER_REQUEST = 1800  # conservative buffer below the 2,000 char API limit

    def __init__(self, api_key: str = None):
        self.client = ElevenLabs(api_key=api_key or ELEVENLABS_API_KEY)
        self.voice_ids = {
            "Host": ELEVENLABS_HOST_VOICE_ID,
            "Cohost": ELEVENLABS_COHOST_VOICE_ID,
        }

    def parse_script(self, script: str) -> list[tuple[str, str]]:
        """
        Parse a two-host script into (speaker, text) turns.

        Expects lines formatted as:
            Host: Some dialogue here
            Cohost: Some dialogue here
        """
        turns = []
        for line in script.splitlines():
            line = line.strip()
            if line.startswith("Host:"):
                turns.append(("Host", line[5:].strip()))
            elif line.startswith("Cohost:"):
                turns.append(("Cohost", line[7:].strip()))
        return turns

    def chunk_turns(
        self, turns: list[tuple[str, str]]
    ) -> list[list[tuple[str, str]]]:
        """
        Group dialogue turns into chunks where total text length <= CHARS_PER_REQUEST.

        Individual turns longer than the limit are kept as single-turn chunks
        (the API will handle them as best it can).
        """
        chunks: list[list[tuple[str, str]]] = []
        current: list[tuple[str, str]] = []
        current_len = 0

        for speaker, text in turns:
            if current and current_len + len(text) > self.CHARS_PER_REQUEST:
                chunks.append(current)
                current = []
                current_len = 0
            current.append((speaker, text))
            current_len += len(text)

        if current:
            chunks.append(current)

        return chunks

    def generate(self, script: str, output_path: str) -> bool:
        """
        Convert a two-host podcast script to an MP3 file.

        Args:
            script: Dialogue formatted as 'Host: ...' / 'Cohost: ...' lines
            output_path: Destination path for the output MP3

        Returns:
            True on success, False on failure
        """
        if not self.voice_ids["Host"] or not self.voice_ids["Cohost"]:
            print("  Error: ELEVENLABS_HOST_VOICE_ID and ELEVENLABS_COHOST_VOICE_ID must be set")
            return False

        turns = self.parse_script(script)
        if not turns:
            print("  Error: No dialogue turns found in script")
            return False

        chunks = self.chunk_turns(turns)
        print(f"  Script: {len(turns)} turns → {len(chunks)} API chunk(s)")

        chunk_paths: list[str] = []
        try:
            for i, chunk in enumerate(chunks):
                inputs = [
                    DialogueInput(text=text, voice_id=self.voice_ids[speaker])
                    for speaker, text in chunk
                ]
                audio = self.client.text_to_dialogue.convert(
                    inputs=inputs,
                    model_id=self.MODEL,
                )
                # SDK returns bytes or an iterator of bytes
                if not isinstance(audio, bytes):
                    audio = b"".join(audio)

                chunk_path = output_path.replace(".mp3", f"_chunk{i}.mp3")
                with open(chunk_path, "wb") as f:
                    f.write(audio)
                chunk_paths.append(chunk_path)

            if len(chunk_paths) == 1:
                os.rename(chunk_paths[0], output_path)
            else:
                self._concat_mp3s(chunk_paths, output_path)

            return True

        except Exception as e:
            print(f"  ElevenLabs TTS error: {e}")
            return False
        finally:
            # Clean up chunk files if they still exist
            for p in chunk_paths:
                if os.path.exists(p):
                    os.remove(p)

    def _concat_mp3s(self, paths: list[str], output: str):
        """Concatenate MP3 files using ffmpeg concat demuxer."""
        list_file = output + ".filelist.txt"
        try:
            with open(list_file, "w") as f:
                for p in paths:
                    # ffmpeg requires absolute or properly escaped paths
                    f.write(f"file '{os.path.abspath(p)}'\n")
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-f", "concat", "-safe", "0",
                    "-i", list_file,
                    "-c", "copy",
                    output,
                ],
                check=True,
                capture_output=True,
            )
        finally:
            if os.path.exists(list_file):
                os.remove(list_file)

    def get_audio_duration(self, file_path: str) -> int:
        """Get duration of audio file in seconds using ffprobe."""
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    file_path,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return int(float(result.stdout.strip()))
        except Exception:
            # Estimate from file size (~16KB/s for MP3)
            try:
                return os.path.getsize(file_path) // 16000
            except Exception:
                return 600  # fallback: 10 minutes
