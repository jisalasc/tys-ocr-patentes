import json
import os
import re
from functools import lru_cache
from dataclasses import dataclass
from itertools import product
from statistics import mean
from typing import Optional, Tuple

from dotenv import load_dotenv
from google.cloud import vision


load_dotenv()

# Patrones de patente del Cono Sur solicitados.
PLATE_PATTERNS = [
    re.compile(r"^[A-Z]{2}[0-9]{2}[A-Z]{2}$"),  # Chile antiguo: AB12CD
    re.compile(r"^[A-Z]{4}[0-9]{2}$"),  # Chile nuevo: ABCD12
    re.compile(r"^[A-Z]{2}[0-9]{4}$"),  # Formato solicitado: AB1234 (AB 12 34)
    re.compile(r"^[A-Z]{3}[0-9]{3}$"),  # Argentina: ABC123
    re.compile(r"^[A-Z]{2}[0-9]{3}[A-Z]{2}$"),  # Argentina: AB123CD
]

MAX_CHAR_SUBSTITUTIONS = int(os.getenv("OCR_MAX_CHAR_SUBSTITUTIONS", "1"))
SUBSTITUTION_PENALTY = float(os.getenv("OCR_SUBSTITUTION_PENALTY", "0.08"))
MIN_STRUCTURED_WORD_CONFIDENCE = float(
    os.getenv("OCR_MIN_STRUCTURED_WORD_CONFIDENCE", "0.78")
)

CHAR_SUBSTITUTIONS = {
    "0": ("0", "O"),
    "O": ("O", "0"),
    "1": ("1", "I"),
    "I": ("I", "1"),
}

CANDIDATE_PREFIX_BLACKLIST = {
    prefix.strip().upper()
    for prefix in os.getenv("OCR_CANDIDATE_PREFIX_BLACKLIST", "BENZ").split(",")
    if prefix.strip()
}


def _is_blocked_candidate(candidate: str) -> bool:
    if len(candidate) == 6 and candidate[:4].isalpha() and candidate[4:].isdigit():
        if candidate[:4] in CANDIDATE_PREFIX_BLACKLIST:
            return True
    return False


@dataclass(frozen=True)
class OCRWord:
    text: str
    confidence: float
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def center_y(self) -> float:
        return (self.y0 + self.y1) / 2.0

    @property
    def height(self) -> float:
        return max(1.0, self.y1 - self.y0)


def _build_vision_client() -> vision.ImageAnnotatorClient:
    raw_credentials = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
    if not raw_credentials:
        # En Cloud Run usamos Application Default Credentials.
        return vision.ImageAnnotatorClient()

    # Permite credenciales envueltas en comillas al venir desde .env.
    if (
        (raw_credentials.startswith("'") and raw_credentials.endswith("'"))
        or (raw_credentials.startswith('"') and raw_credentials.endswith('"'))
    ):
        raw_credentials = raw_credentials[1:-1]

    try:
        info = json.loads(raw_credentials)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON no contiene un JSON valido.") from exc

    return vision.ImageAnnotatorClient.from_service_account_info(info)


@lru_cache(maxsize=1)
def _get_vision_client() -> vision.ImageAnnotatorClient:
    # Reutilizamos el cliente para evitar recrearlo por cada imagen.
    return _build_vision_client()


def _extract_ocr_confidence(response: vision.AnnotateImageResponse) -> float:
    # Document text suele incluir confianza a nivel palabra; usamos promedio global.
    full = response.full_text_annotation
    if not full or not full.pages:
        return 0.0

    confidences = []
    for page in full.pages:
        for block in page.blocks:
            for paragraph in block.paragraphs:
                for word in paragraph.words:
                    if word.confidence is not None:
                        confidences.append(float(word.confidence))

    if not confidences:
        return 0.0
    return float(mean(confidences))


def _extract_words_with_confidence(
    response: vision.AnnotateImageResponse,
) -> list[OCRWord]:
    full = response.full_text_annotation
    if not full or not full.pages:
        return []

    words: list[OCRWord] = []
    for page in full.pages:
        for block in page.blocks:
            for paragraph in block.paragraphs:
                for word in paragraph.words:
                    text = "".join(symbol.text for symbol in word.symbols)
                    clean_text = re.sub(r"[^A-Z0-9]", "", text.upper())
                    if not clean_text:
                        continue
                    conf = float(word.confidence) if word.confidence is not None else 0.0
                    vertices = list(word.bounding_box.vertices)
                    xs = [float(vertex.x or 0) for vertex in vertices]
                    ys = [float(vertex.y or 0) for vertex in vertices]
                    words.append(
                        OCRWord(
                            text=clean_text,
                            confidence=conf,
                            x0=min(xs) if xs else 0.0,
                            y0=min(ys) if ys else 0.0,
                            x1=max(xs) if xs else 0.0,
                            y1=max(ys) if ys else 0.0,
                        )
                    )
    return words


def _candidate_variants(
    token: str,
    allow_substitutions: bool,
) -> list[Tuple[str, int]]:
    if not token:
        return []

    if not allow_substitutions:
        return [(token, 0)]

    options_per_char: list[Tuple[str, ...]] = []
    for char in token:
        options_per_char.append(CHAR_SUBSTITUTIONS.get(char, (char,)))

    variants: list[Tuple[str, int]] = []
    for chars in product(*options_per_char):
        candidate = "".join(chars)
        substitutions = sum(1 for source, target in zip(token, chars) if source != target)
        if substitutions <= MAX_CHAR_SUBSTITUTIONS:
            variants.append((candidate, substitutions))

    # Evitamos duplicados preservando el menor numero de sustituciones por variante.
    best_per_candidate: dict[str, int] = {}
    for candidate, substitutions in variants:
        prev = best_per_candidate.get(candidate)
        if prev is None or substitutions < prev:
            best_per_candidate[candidate] = substitutions

    return sorted(best_per_candidate.items(), key=lambda item: (item[1], item[0]))


def _find_best_plate_from_tokens(
    tokens: list[str],
    confidences: list[float],
    *,
    allow_substitutions: bool,
) -> Tuple[Optional[str], float]:
    best_plate: Optional[str] = None
    best_confidence = 0.0

    for start in range(len(tokens)):
        for size in (1, 2, 3):
            end = start + size
            if end > len(tokens):
                continue

            chunk_tokens = tokens[start:end]
            if any(len(token) > 4 for token in chunk_tokens):
                continue

            chunk_confidences = confidences[start:end]
            candidate_options = product(
                *[
                    _candidate_variants(token, allow_substitutions=allow_substitutions)
                    for token in chunk_tokens
                ]
            )
            for option in candidate_options:
                candidate = "".join(variant for variant, _ in option)
                substitutions = sum(count for _, count in option)
                if not any(pattern.fullmatch(candidate) for pattern in PLATE_PATTERNS):
                    continue
                if _is_blocked_candidate(candidate):
                    continue

                confidence = float(mean(chunk_confidences))
                confidence -= SUBSTITUTION_PENALTY * substitutions

                if confidence > best_confidence:
                    best_plate = candidate
                    best_confidence = confidence

    return best_plate, best_confidence


def _group_words_into_lines(words: list[OCRWord]) -> list[list[OCRWord]]:
    if not words:
        return []

    sorted_words = sorted(words, key=lambda word: (word.center_y, word.x0))
    lines: list[list[OCRWord]] = []
    current_line: list[OCRWord] = []

    for word in sorted_words:
        if not current_line:
            current_line.append(word)
            continue

        current_center = float(mean(item.center_y for item in current_line))
        current_height = float(mean(item.height for item in current_line))
        tolerance = max(current_height, word.height) * 0.65

        if abs(word.center_y - current_center) <= tolerance:
            current_line.append(word)
        else:
            lines.append(sorted(current_line, key=lambda item: item.x0))
            current_line = [word]

    if current_line:
        lines.append(sorted(current_line, key=lambda item: item.x0))

    return lines


def _find_first_plate(text: str) -> Optional[str]:
    tokens = re.findall(r"[A-Z0-9]+", text.upper())
    if not tokens:
        return None

    plate, _ = _find_best_plate_from_tokens(
        tokens,
        [0.5] * len(tokens),
        allow_substitutions=False,
    )
    return plate


def _find_best_plate_from_words(words: list[OCRWord]) -> Tuple[Optional[str], float]:
    best_plate: Optional[str] = None
    best_confidence = 0.0

    for line in _group_words_into_lines(words):
        tokens = [word.text for word in line]
        confidences = [word.confidence for word in line]
        plate, confidence = _find_best_plate_from_tokens(
            tokens,
            confidences,
            allow_substitutions=True,
        )
        if plate and confidence > best_confidence:
            best_plate = plate
            best_confidence = confidence

    return best_plate, best_confidence


def detect_plate_with_debug(image_bytes: bytes) -> Tuple[Optional[str], float, str, str]:
    client = _get_vision_client()
    image = vision.Image(content=image_bytes)
    image_context = vision.ImageContext(language_hints=["es", "en"])

    # text_detection suele rendir mejor en patentes cortas; document_text_detection aporta confianza.
    text_response = client.text_detection(image=image, image_context=image_context)
    doc_response = client.document_text_detection(image=image, image_context=image_context)

    if text_response.error.message:
        print(f"Vision text_detection error: {text_response.error.message}")
    if doc_response.error.message:
        print(f"Vision document_text_detection error: {doc_response.error.message}")

    if text_response.error.message and doc_response.error.message:
        raise RuntimeError(
            "Vision API devolvio error en text_detection y document_text_detection."
        )

    words = _extract_words_with_confidence(doc_response)
    plate_from_words, words_confidence = _find_best_plate_from_words(words)
    if plate_from_words and words_confidence >= MIN_STRUCTURED_WORD_CONFIDENCE:
        words_text = " ".join(word.text for word in words)
        return plate_from_words, words_confidence, words_text, "words"

    full_text = ""
    if text_response.text_annotations:
        full_text = text_response.text_annotations[0].description or ""
    elif doc_response.full_text_annotation:
        full_text = doc_response.full_text_annotation.text or ""

    plate = _find_first_plate(full_text)
    confidence = _extract_ocr_confidence(doc_response)

    # Si no hay confianza estructurada, usamos un valor base cuando al menos hubo texto.
    if confidence == 0.0 and full_text.strip():
        confidence = 0.65

    source = "text_annotations" if text_response.text_annotations else "full_text_annotation"
    return plate, confidence, full_text, source


def detect_plate(image_bytes: bytes) -> Tuple[Optional[str], float]:
    plate, confidence, _, _ = detect_plate_with_debug(image_bytes)
    return plate, confidence
