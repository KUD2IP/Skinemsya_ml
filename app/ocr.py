"""Обёртка над RapidOCR (ONNX, PP-OCRv5 mobile).

Распознавание — восточнославянская модель (eslav, включает русский).
Дообученную на чеках модель можно подключить без изменения кода:
    ML_REC_MODEL_PATH  — путь к своей rec-модели (.onnx)
    ML_REC_KEYS_PATH   — путь к словарю символов для неё (.txt)
"""

import os
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from rapidocr import RapidOCR
from rapidocr.utils.typings import LangDet, LangRec, ModelType, OCRVersion


@dataclass
class OcrToken:
    box: np.ndarray  # 4 точки (x, y)
    text: str
    score: float

    @property
    def x_min(self) -> float:
        return float(self.box[:, 0].min())

    @property
    def x_max(self) -> float:
        return float(self.box[:, 0].max())

    @property
    def y_center(self) -> float:
        return float(self.box[:, 1].mean())

    @property
    def height(self) -> float:
        return float(self.box[:, 1].max() - self.box[:, 1].min())


def _build_engine() -> RapidOCR:
    params = {
        "Det.ocr_version": OCRVersion.PPOCRV5,
        "Det.model_type": ModelType.MOBILE,
        "Det.lang_type": LangDet.CH,
        "Rec.ocr_version": OCRVersion.PPOCRV5,
        "Rec.model_type": ModelType.MOBILE,
        "Rec.lang_type": LangRec.ESLAV,
        "Global.log_level": "warning",
        # На 1 vCPU лишние потоки только мешают.
        "EngineConfig.onnxruntime.intra_op_num_threads": int(os.getenv("ML_OCR_THREADS", "2")),
    }

    rec_model_path = os.getenv("ML_REC_MODEL_PATH")
    rec_keys_path = os.getenv("ML_REC_KEYS_PATH")
    if rec_model_path:
        params["Rec.model_path"] = rec_model_path
    if rec_keys_path:
        params["Rec.rec_keys_path"] = rec_keys_path

    model_root = os.getenv("ML_MODEL_ROOT_DIR")
    if model_root:
        params["Global.model_root_dir"] = model_root

    return RapidOCR(params=params)


@lru_cache(maxsize=1)
def get_engine() -> RapidOCR:
    return _build_engine()


def run_ocr(image: np.ndarray) -> list[OcrToken]:
    """BGR-изображение -> распознанные фрагменты текста с координатами."""
    result = get_engine()(image)
    if result is None or result.boxes is None or len(result.boxes) == 0:
        return []
    tokens = []
    for box, text, score in zip(result.boxes, result.txts, result.scores):
        text = (text or "").strip()
        if not text:
            continue
        tokens.append(OcrToken(box=np.asarray(box, dtype=float), text=text, score=float(score)))
    return tokens
