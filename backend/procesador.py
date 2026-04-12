import csv
import io
import os
import zipfile
from typing import Dict, List, Tuple

from PIL import Image, ImageEnhance, ImageFilter, UnidentifiedImageError

from ocr import detect_plate_with_debug


VALID_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
CONFIDENCE_THRESHOLD = 0.80


def _is_valid_image(filename: str) -> bool:
    return os.path.splitext(filename.lower())[1] in VALID_EXTENSIONS


def _to_jpg_bytes(image_bytes: bytes) -> bytes:
    with Image.open(io.BytesIO(image_bytes)) as img:
        converted = img.convert("RGB")
        output = io.BytesIO()
        converted.save(output, format="JPEG", quality=95)
        return output.getvalue()


def _enhance_for_ocr(image_bytes: bytes) -> bytes:
    with Image.open(io.BytesIO(image_bytes)) as img:
        enhanced = img.convert("L")

        # Escalamos para mejorar caracteres pequeños típicos de patentes en foto completa.
        width, height = enhanced.size
        if width < 1400:
            factor = 2
            enhanced = enhanced.resize((width * factor, height * factor), Image.Resampling.LANCZOS)

        enhanced = ImageEnhance.Contrast(enhanced).enhance(1.6)
        enhanced = enhanced.filter(ImageFilter.SHARPEN)

        output = io.BytesIO()
        enhanced.convert("RGB").save(output, format="JPEG", quality=95)
        return output.getvalue()


def _build_unique_name(base_name: str, used_names: set[str]) -> str:
    if base_name not in used_names:
        used_names.add(base_name)
        return base_name

    i = 1
    while True:
        candidate = f"{base_name[:-4]}_{i:03d}.jpg"
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate
        i += 1


def _build_csv_bytes(rows: List[Dict[str, str]]) -> bytes:
    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        csv_buffer,
        fieldnames=["archivo_original", "patente_detectada", "confianza", "estado"],
    )
    writer.writeheader()
    writer.writerows(rows)
    return csv_buffer.getvalue().encode("utf-8-sig")


def _build_debug_csv_bytes(rows: List[Dict[str, str]]) -> bytes:
    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        csv_buffer,
        fieldnames=[
            "archivo_original",
            "texto_ocr",
            "fuente_ocr",
            "patente_detectada",
            "confianza",
            "estado",
        ],
    )
    writer.writeheader()
    writer.writerows(rows)
    return csv_buffer.getvalue().encode("utf-8-sig")


def procesar_zip(zip_bytes: bytes, include_ocr_debug: bool = False) -> bytes:
    input_buffer = io.BytesIO(zip_bytes)
    output_buffer = io.BytesIO()

    used_names: set[str] = set()
    no_reconocida_counter = 1

    # Leer todos los archivos válidos del ZIP primero
    archivos: List[Tuple[str, bytes]] = []
    with zipfile.ZipFile(input_buffer, mode="r") as input_zip:
        for file_info in input_zip.infolist():
            if file_info.is_dir():
                continue
            if not _is_valid_image(file_info.filename):
                continue
            archivos.append((file_info.filename, input_zip.read(file_info)))

    # Procesar cada imagen (esto es lo que se paraleliza)
    def procesar_una(item: Tuple[str, bytes]) -> Dict:
        original_name, original_bytes = item
        try:
            jpg_bytes = _to_jpg_bytes(original_bytes)
            plate, confidence, raw_text, ocr_source = detect_plate_with_debug(jpg_bytes)

            if not plate or confidence < CONFIDENCE_THRESHOLD:
                enhanced_bytes = _enhance_for_ocr(original_bytes)
                plate_e, confidence_e, raw_text_e, ocr_source_e = detect_plate_with_debug(enhanced_bytes)
                if (plate_e and not plate) or confidence_e > confidence:
                    plate, confidence, raw_text, ocr_source = plate_e, confidence_e, raw_text_e, ocr_source_e
                    jpg_bytes = enhanced_bytes

            print(f"OCR -> archivo={original_name}, patente={plate}, confianza={confidence:.4f}")
            return {
                "original_name": original_name,
                "jpg_bytes": jpg_bytes,
                "plate": plate,
                "confidence": confidence,
                "raw_text": raw_text,
                "ocr_source": ocr_source,
                "error": None,
            }
        except Exception as exc:
            print(f"Fallo procesando {original_name}: {exc}")
            return {
                "original_name": original_name,
                "jpg_bytes": original_bytes,
                "plate": None,
                "confidence": 0.0,
                "raw_text": f"ERROR: {exc}",
                "ocr_source": "exception",
                "error": str(exc),
            }

    # Ejecutar en paralelo con 5 workers
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=5) as executor:
        resultados = list(executor.map(procesar_una, archivos))

    # Ensamblar resultados en orden
    csv_rows: List[Dict[str, str]] = []
    debug_rows: List[Dict[str, str]] = []
    output_images: List[Tuple[str, bytes]] = []

    for r in resultados:
        plate = r["plate"]
        confidence = r["confidence"]

        if plate and confidence >= CONFIDENCE_THRESHOLD:
            output_name = _build_unique_name(f"{plate}.jpg", used_names)
            status = "RECONOCIDA"
            plate_value = plate
        else:
            output_name = _build_unique_name(
                f"NO_RECONOCIDA_{no_reconocida_counter:03d}.jpg", used_names
            )
            no_reconocida_counter += 1
            status = "NO_RECONOCIDA"
            plate_value = ""

        output_images.append((output_name, r["jpg_bytes"]))
        csv_rows.append({
            "archivo_original": r["original_name"],
            "patente_detectada": plate_value,
            "confianza": f"{confidence:.4f}",
            "estado": status,
        })
        if include_ocr_debug:
            debug_rows.append({
                "archivo_original": r["original_name"],
                "texto_ocr": r["raw_text"].replace("\n", " "),
                "fuente_ocr": r["ocr_source"],
                "patente_detectada": plate_value,
                "confianza": f"{confidence:.4f}",
                "estado": status,
            })

    csv_bytes = _build_csv_bytes(csv_rows)
    debug_csv_bytes = _build_debug_csv_bytes(debug_rows) if include_ocr_debug else b""

    with zipfile.ZipFile(output_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as output_zip:
        for output_name, image_data in output_images:
            output_zip.writestr(output_name, image_data)
        output_zip.writestr("patentes.csv", csv_bytes)
        if include_ocr_debug:
            output_zip.writestr("patentes_debug_ocr.csv", debug_csv_bytes)

    output_buffer.seek(0)
    return output_buffer.getvalue()
