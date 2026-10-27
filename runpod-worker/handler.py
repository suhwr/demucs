import base64
import os
import pathlib
import subprocess
import tempfile
from typing import Any, Dict, Optional, Tuple, cast

import requests

try:
    import runpod  # type: ignore[import]
except ModuleNotFoundError:  # pragma: no cover - runpod only exists in the worker image
    runpod = cast(Any, None)

INPUT_KEY_FILE = "audio_input"
DEFAULT_MODEL = "htdemucs"
DEFAULT_SHIFTS = 1
DEFAULT_OVERLAP = 0.25

# Default Catbox User Hash
DEFAULT_CATBOX_USER_HASH = os.getenv("CATBOX_USER_HASH", "0f9dc6f11f2f5ac4241235974")


# ============================================================================ #
# Uploader Functions: uguu -> catbox -> ucarecdn                               #
# ============================================================================ #

def _upload_uguu(file_path: pathlib.Path) -> str:
    """Upload to Uguu.se."""
    with open(file_path, "rb") as f:
        resp = requests.post(
            "https://uguu.se/upload",
            params={"output": "json"},
            files={"files[]": (file_path.name, f)},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        files = data.get("files")
        if files and len(files) > 0 and files[0].get("url"):
            return files[0]["url"]
        raise ValueError(f"Uguu response invalid: {data}")


def _upload_catbox(file_path: pathlib.Path, user_hash: Optional[str] = None) -> str:
    """Upload to Catbox.moe."""
    data = {"reqtype": "fileupload"}
    hash_to_use = user_hash or DEFAULT_CATBOX_USER_HASH
    if hash_to_use:
        data["userhash"] = hash_to_use

    with open(file_path, "rb") as f:
        resp = requests.post(
            "https://catbox.moe/user/api.php",
            data=data,
            files={"fileToUpload": (file_path.name, f)},
            timeout=90,
        )
        resp.raise_for_status()
        url = resp.text.strip()
        if url.startswith("https://"):
            return url
        raise ValueError(f"Catbox failed with response: {url}")


def _upload_ucarecdn(file_path: pathlib.Path, pub_key: str = "demopublickey") -> str:
    """Upload to Uploadcare (Ucarecdn)."""
    with open(file_path, "rb") as f:
        resp = requests.post(
            "https://upload.uploadcare.com/base/",
            data={
                "UPLOADCARE_PUB_KEY": pub_key,
                "UPLOADCARE_STORE": "1",
            },
            files={"file": (file_path.name, f)},
            timeout=60,
        )
        resp.raise_for_status()
        file_id = resp.json().get("file")
        if file_id:
            return f"https://ucarecdn.com/{file_id}/{file_path.name}"
        raise ValueError(f"Uploadcare failed: {resp.text}")


def upload_with_fallback(file_path: pathlib.Path, catbox_hash: Optional[str] = None) -> Tuple[str, str]:
    """Fallback chain: uguu -> catbox -> ucarecdn."""
    errors = []

    # 1. Try Uguu
    try:
        url = _upload_uguu(file_path)
        return url, "uguu"
    except Exception as exc:
        errors.append(f"uguu: {exc}")

    # 2. Try Catbox
    try:
        url = _upload_catbox(file_path, user_hash=catbox_hash)
        return url, "catbox"
    except Exception as exc:
        errors.append(f"catbox: {exc}")

    # 3. Try Ucarecdn
    try:
        url = _upload_ucarecdn(file_path)
        return url, "ucarecdn"
    except Exception as exc:
        errors.append(f"ucarecdn: {exc}")

    raise RuntimeError("All uploaders failed: " + " | ".join(errors))


# ============================================================================ #
# Audio Helper Functions                                                       #
# ============================================================================ #

def _download_audio(audio_url: str, destination: pathlib.Path) -> None:
    response = requests.get(audio_url, stream=True, timeout=120)
    response.raise_for_status()
    with destination.open("wb") as file_handle:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                file_handle.write(chunk)


def _convert_wav_to_mp3(wav_path: pathlib.Path, mp3_path: pathlib.Path, bitrate: str = "192k") -> None:
    """Convert uncompressed WAV to MP3 via ffmpeg."""
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(wav_path),
        "-vn",
        "-b:a", bitrate,
        str(mp3_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _find_stems_dir(out_root: pathlib.Path, model_name: str) -> pathlib.Path:
    model_root = out_root / model_name
    if not model_root.exists():
        raise FileNotFoundError(f"Demucs output missing for model '{model_name}'")
    candidates = [child for child in model_root.iterdir() if child.is_dir()]
    if not candidates:
        raise FileNotFoundError("No separated stems directory found")
    return candidates[0]


# ============================================================================ #
# Main RunPod Handler                                                          #
# ============================================================================ #

def handler(event: Dict[str, Any]) -> Dict[str, Any]:
    inputs = event.get("input") or {}

    audio_url = inputs.get("audio_url")
    audio_b64 = inputs.get("audio_base64")
    model_name = inputs.get("model_name", DEFAULT_MODEL)
    shifts = int(inputs.get("shifts", DEFAULT_SHIFTS))
    overlap = float(inputs.get("overlap", DEFAULT_OVERLAP))
    output_format = str(inputs.get("output_format", "mp3")).lower().strip()
    two_stems = inputs.get("two_stems")
    catbox_hash = inputs.get("catbox_user_hash") or DEFAULT_CATBOX_USER_HASH

    if not (audio_url or audio_b64):
        return {"status": "error", "error": "Provide 'audio_url' or 'audio_base64'"}

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = pathlib.Path(tmp_dir)
        audio_path = tmp_path / INPUT_KEY_FILE
        output_root = tmp_path / "separations"

        try:
            if audio_url:
                _download_audio(str(audio_url), audio_path)
            elif audio_b64:
                audio_path.write_bytes(base64.b64decode(audio_b64))

            env = os.environ.copy()
            env.setdefault("TORCHAUDIO_USE_SOUND_FILE", "1")

            cmd = [
                "demucs",
                "--name", str(model_name),
                "--shifts", str(shifts),
                "--overlap", str(overlap),
                "--out", str(output_root),
            ]

            if two_stems:
                cmd.extend(["--two-stems", str(two_stems)])

            cmd.append(str(audio_path))
            subprocess.run(cmd, check=True, env=env, cwd=tmp_dir)

            stems_dir = _find_stems_dir(output_root, str(model_name))

            stems_result: Dict[str, Any] = {}
            wav_files = list(stems_dir.glob("*.wav"))

            if not wav_files:
                return {"status": "error", "error": "No stems were generated by Demucs"}

            for wav_file in wav_files:
                stem_name = wav_file.stem
                target_file = wav_file

                if output_format == "mp3":
                    mp3_path = stems_dir / f"{stem_name}.mp3"
                    _convert_wav_to_mp3(wav_file, mp3_path, bitrate="192k")
                    target_file = mp3_path

                file_url, provider = upload_with_fallback(target_file, catbox_hash=catbox_hash)

                stems_result[stem_name] = {
                    "url": file_url,
                    "provider": provider,
                    "filename": target_file.name,
                    "size_bytes": target_file.stat().st_size,
                }

            return {
                "status": "success",
                "model": model_name,
                "stem_count": len(stems_result),
                "stems": stems_result,
            }

        except Exception as exc:
            return {"status": "error", "error": str(exc)}


if runpod:
    runpod.serverless.start({"handler": handler})
