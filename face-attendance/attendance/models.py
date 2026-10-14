"""Download the model files for a recognition backend into models/."""
from __future__ import annotations

import shutil
import urllib.request
import zipfile
from pathlib import Path

INSIGHTFACE_PACK = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"
OPENCV_ZOO = "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"

LICENCES = {
    "insightface": "InsightFace pretrained models: non-commercial research use only. "
                   "Commercial use requires a licence from InsightFace (contact@insightface.ai).",
    "opencv": "YuNet: MIT licence. SFace: Apache 2.0 licence. Free for commercial use.",
}


def _fetch_to(url: str, dest: Path) -> None:
    print(f"Downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out)


def _fetch(url: str) -> bytes:
    print(f"Downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as resp:
        return resp.read()


def download(backend: str, models_dir: Path) -> None:
    models_dir.mkdir(parents=True, exist_ok=True)
    if backend == "insightface":
        archive = models_dir / "buffalo_l.zip"  # ~290 MB; only two of its models are needed
        _fetch_to(INSIGHTFACE_PACK, archive)
        with zipfile.ZipFile(archive) as pack:
            for name in ("det_10g.onnx", "w600k_r50.onnx"):
                member = next(m for m in pack.namelist() if m.endswith(name))
                with pack.open(member) as src, open(models_dir / name, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        archive.unlink()
    elif backend == "opencv":
        for path in ("face_detection_yunet/face_detection_yunet_2023mar.onnx",
                     "face_recognition_sface/face_recognition_sface_2021dec.onnx"):
            data = _fetch(OPENCV_ZOO + path)
            if len(data) < 10_000:  # a Git LFS pointer instead of the model
                raise RuntimeError(f"Download of {path} failed (got {len(data)} bytes)")
            (models_dir / Path(path).name).write_bytes(data)
    else:
        raise ValueError(f"unknown backend {backend}")
    print(f"Saved to {models_dir}")
    print(f"Licence: {LICENCES[backend]}")
