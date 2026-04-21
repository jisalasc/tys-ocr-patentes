import csv
import io
import os
import zipfile
from collections import defaultdict
from typing import Dict, List, Tuple

from PIL import Image, ImageEnhance, ImageFilter, UnidentifiedImageError

from ocr import detect_plate_with_debug


VALID_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
CONFIDENCE_THRESHOLD = float(os.getenv("OCR_CONFIDENCE_THRESHOLD", "0.88"))
REQUIRE_CONSENSUS = os.getenv("OCR_REQUIRE_CONSENSUS", "true").strip().lower() in {
    "1",
    "true",
    "yes",
}
HIGH_CONFIDENCE_OVERRIDE = float(os.getenv("OCR_HIGH_CONFIDENCE_OVERRIDE", "0.95"))


def _is_valid_image(filename: str) -> bool:
    return os.path.splitext(filename.lower())[1] in VALID_EXTENSIONS


def _to_jpg_bytes(image_bytes: bytes) -> bytes:
    with Image.open(io.BytesIO(image_bytes)) as img:
        converted = img.convert("RGB")
        output = io.BytesIO()
        converted.save(output, format="JPEG", quality=95)
        return output.getvalue()


def _to_jpg_bytes_from_image(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.convert("RGB").save(output, format="JPEG", quality=95)
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


def _crop_image_region(
    image: Image.Image,
    *,
    x0_ratio: float,
    y0_ratio: float,
    x1_ratio: float,
    y1_ratio: float,
) -> Image.Image | None:
    width, height = image.size
    x0 = max(0, min(width - 1, int(width * x0_ratio)))
    y0 = max(0, min(height - 1, int(height * y0_ratio)))
    x1 = max(1, min(width, int(width * x1_ratio)))
    y1 = max(1, min(height, int(height * y1_ratio)))

    if x1 <= x0 or y1 <= y0:
        return None
    if (x1 - x0) < 120 or (y1 - y0) < 40:
        return None

    return image.crop((x0, y0, x1, y1))


def _build_ocr_variants(image_bytes: bytes) -> List[Tuple[str, bytes]]:
    variants: List[Tuple[str, bytes]] = []

    full_jpg = _to_jpg_bytes(image_bytes)
    variants.append(("full", full_jpg))
    variants.append(("full_enhanced", _enhance_for_ocr(image_bytes)))

    with Image.open(io.BytesIO(image_bytes)) as img:
        rgb = img.convert("RGB")
        crops = [
            (
                "roi_center",
                _crop_image_region(
                    rgb,
                    x0_ratio=0.20,
                    y0_ratio=0.60,
                    x1_ratio=0.80,
                    y1_ratio=0.88,
                ),
            ),
            (
                "roi_lower",
                _crop_image_region(
                    rgb,
                    x0_ratio=0.15,
                    y0_ratio=0.68,
                    x1_ratio=0.85,
                    y1_ratio=0.95,
                ),
            ),
        ]

        for label, crop in crops:
            if crop is None:
                continue
            crop_bytes = _to_jpg_bytes_from_image(crop)
            variants.append((label, crop_bytes))
            variants.append((f"{label}_enhanced", _enhance_for_ocr(crop_bytes)))

    return variants


def _select_best_variant_result(results: List[Dict]) -> Dict:
    roi_variants = {
        "roi_center",
        "roi_lower",
        "roi_center_enhanced",
        "roi_lower_enhanced",
    }

    candidates: dict[str, List[Dict]] = defaultdict(list)
    for result in results:
        plate = result["plate"]
        confidence = float(result["confidence"])
        if plate and confidence >= CONFIDENCE_THRESHOLD:
            candidates[plate].append(result)

    if not candidates:
        best_any = max(results, key=lambda item: float(item["confidence"]))
        return {
            **best_any,
            "accepted": False,
            "motivo_no_reconocida": "sin_patron_valido_o_confianza_baja",
            "consenso": 0,
            "mejor_candidato": best_any.get("plate") or "",
            "confianza_candidato": float(best_any["confidence"]),
        }

    best_plate = ""
    best_plate_count = -1
    best_plate_confidence = -1.0
    best_plate_result: Dict | None = None

    for plate, plate_results in candidates.items():
        count = len(plate_results)
        top = max(plate_results, key=lambda item: float(item["confidence"]))
        top_conf = float(top["confidence"])
        if count > best_plate_count or (count == best_plate_count and top_conf > best_plate_confidence):
            best_plate = plate
            best_plate_count = count
            best_plate_confidence = top_conf
            best_plate_result = top

    assert best_plate_result is not None

    high_conf_override_ok = (
        best_plate_result["ocr_source"] == "words"
        and best_plate_confidence >= HIGH_CONFIDENCE_OVERRIDE
    )
    has_roi_evidence = any(
        item.get("ocr_variant") in roi_variants
        for item in candidates.get(best_plate, [])
    )

    accepted = (
        (best_plate_count >= 2)
        if REQUIRE_CONSENSUS
        else True
    ) and has_roi_evidence

    if not accepted and high_conf_override_ok and has_roi_evidence:
        accepted = True

    if not accepted:
        reason = "sin_evidencia_roi" if not has_roi_evidence else "sin_consenso_entre_variantes"
        return {
            **best_plate_result,
            "plate": None,
            "accepted": False,
            "motivo_no_reconocida": reason,
            "consenso": best_plate_count,
            "mejor_candidato": best_plate,
            "confianza_candidato": best_plate_confidence,
        }

    return {
        **best_plate_result,
        "accepted": True,
        "motivo_no_reconocida": "",
        "consenso": best_plate_count,
        "mejor_candidato": best_plate,
        "confianza_candidato": best_plate_confidence,
    }


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
        fieldnames=[
            "archivo_original",
            "patente_detectada",
            "confianza",
            "estado",
            "motivo_no_reconocida",
        ],
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
            "variante_ocr",
            "patente_detectada",
            "confianza",
            "estado",
            "motivo_no_reconocida",
            "mejor_candidato",
            "confianza_candidato",
            "consenso",
            "variantes_evaluadas",
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
            variants = _build_ocr_variants(original_bytes)
            variant_results: List[Dict] = []

            for variant_label, variant_bytes in variants:
                plate, confidence, raw_text, ocr_source = detect_plate_with_debug(variant_bytes)
                variant_results.append(
                    {
                        "original_name": original_name,
                        "jpg_bytes": variant_bytes,
                        "plate": plate,
                        "confidence": confidence,
                        "raw_text": raw_text,
                        "ocr_source": ocr_source,
                        "ocr_variant": variant_label,
                    }
                )

            selected = _select_best_variant_result(variant_results)
            plate = selected.get("plate")
            confidence = float(selected.get("confidence", 0.0))
            status = "RECONOCIDA" if selected.get("accepted") and plate else "NO_RECONOCIDA"
            print(
                "OCR -> "
                f"archivo={original_name}, estado={status}, patente={plate}, "
                f"confianza={confidence:.4f}, consenso={selected.get('consenso', 0)}, "
                f"variante={selected.get('ocr_variant', '')}"
            )
            return {
                **selected,
                "original_bytes": original_bytes,
                "error": None,
                "variantes_evaluadas": str(len(variants)),
            }
        except Exception as exc:
            print(f"Fallo procesando {original_name}: {exc}")
            return {
                "original_name": original_name,
                "original_bytes": original_bytes,
                "jpg_bytes": original_bytes,
                "plate": None,
                "confidence": 0.0,
                "raw_text": f"ERROR: {exc}",
                "ocr_source": "exception",
                "ocr_variant": "exception",
                "accepted": False,
                "motivo_no_reconocida": "error_procesando_imagen",
                "consenso": 0,
                "mejor_candidato": "",
                "confianza_candidato": 0.0,
                "variantes_evaluadas": "0",
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
        confidence = float(r["confidence"])
        recognized = bool(r.get("accepted") and plate)

        if recognized:
            output_name = _build_unique_name(f"{plate}.jpg", used_names)
            status = "RECONOCIDA"
            plate_value = plate
            confidence_value = f"{confidence:.4f}"
            reason_value = ""
        else:
            output_name = _build_unique_name(
                f"NO_RECONOCIDA_{no_reconocida_counter:03d}.jpg", used_names
            )
            no_reconocida_counter += 1
            status = "NO_RECONOCIDA"
            plate_value = ""
            confidence_value = ""
            reason_value = r.get("motivo_no_reconocida", "sin_patron_valido_o_confianza_baja")

        output_images.append((output_name, r["original_bytes"]))
        csv_rows.append({
            "archivo_original": r["original_name"],
            "patente_detectada": plate_value,
            "confianza": confidence_value,
            "estado": status,
            "motivo_no_reconocida": reason_value,
        })
        if include_ocr_debug:
            debug_rows.append({
                "archivo_original": r["original_name"],
                "texto_ocr": r["raw_text"].replace("\n", " "),
                "fuente_ocr": r["ocr_source"],
                "variante_ocr": r.get("ocr_variant", ""),
                "patente_detectada": plate_value,
                "confianza": f"{confidence:.4f}" if recognized else "",
                "estado": status,
                "motivo_no_reconocida": reason_value,
                "mejor_candidato": r.get("mejor_candidato", ""),
                "confianza_candidato": f"{float(r.get('confianza_candidato', 0.0)):.4f}",
                "consenso": str(r.get("consenso", 0)),
                "variantes_evaluadas": r.get("variantes_evaluadas", ""),
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
