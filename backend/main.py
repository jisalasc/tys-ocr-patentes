from io import BytesIO
from uuid import uuid4
import zipfile

from fastapi import Depends, FastAPI, File, HTTPException, Query, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from auth import authenticate_user, create_access_token, get_current_user
from gcs import delete_blob, generate_download_url, generate_upload_url, read_blob, write_blob
from procesador import procesar_zip


app = FastAPI(title="OCR Patentes Backend")

# CORS configurado para desarrollo y producción
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3000",
        "https://tys-ocr-patentes-105536109273.us-central1.run.app",
    ],
    allow_credentials=False,  # No necesario con JWT en headers
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


class LoginRequest(BaseModel):
    username: str
    password: str

    class Config:
        str_strip_whitespace = True


class UploadUrlRequest(BaseModel):
    filename: str

    class Config:
        str_strip_whitespace = True


class ProcessGCSRequest(BaseModel):
    upload_id: str

    class Config:
        str_strip_whitespace = True


def _count_processed_images(zip_bytes: bytes) -> int | None:
    try:
        with zipfile.ZipFile(BytesIO(zip_bytes), mode="r") as processed_zip:
            return sum(
                1
                for file_info in processed_zip.infolist()
                if (not file_info.is_dir()) and file_info.filename.lower().endswith(".jpg")
            )
    except Exception:
        return None


@app.get("/")
def healthcheck() -> dict:
    return {"status": "ok"}


@app.post("/login")
def login(payload: LoginRequest) -> dict:
    if not authenticate_user(payload.username, payload.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales invalidas.",
        )

    token = create_access_token(subject=payload.username)
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in_hours": 8,
    }


@app.post("/procesar")
async def procesar(
    file: UploadFile = File(...),
    debug_ocr: bool = Query(False, description="Incluye patentes_debug_ocr.csv en el ZIP"),
    current_user: str = Depends(get_current_user),
):
    _ = current_user

    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debes subir un archivo .zip",
        )

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El archivo ZIP esta vacio.",
        )

    try:
        output_zip_bytes = procesar_zip(file_bytes, include_ocr_debug=debug_ocr)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error procesando ZIP: {exc}",
        ) from exc

    return StreamingResponse(
        BytesIO(output_zip_bytes),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="resultado.zip"'},
    )


@app.post("/upload-url")
def create_upload_url(
    payload: UploadUrlRequest,
    current_user: str = Depends(get_current_user),
) -> dict:
    _ = current_user

    if not payload.filename or not payload.filename.lower().endswith(".zip"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El campo filename debe terminar en .zip",
        )

    upload_id = str(uuid4())
    blob_name = f"uploads/{upload_id}.zip"

    try:
        upload_url = generate_upload_url(
            filename=blob_name,
            content_type="application/zip",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"No fue posible generar la URL de subida: {exc}",
        ) from exc

    return {
        "upload_url": upload_url,
        "upload_id": upload_id,
    }


@app.post("/procesar-gcs")
def procesar_gcs(
    payload: ProcessGCSRequest,
    response: Response,
    current_user: str = Depends(get_current_user),
) -> dict:
    _ = current_user

    upload_id = payload.upload_id.strip()
    if not upload_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="upload_id es requerido",
        )

    source_blob_name = f"uploads/{upload_id}.zip"
    result_blob_name = f"results/{upload_id}.zip"

    try:
        input_zip_bytes = read_blob(source_blob_name)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No existe el upload en GCS: {source_blob_name}",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"No fue posible leer el ZIP desde GCS: {exc}",
        ) from exc

    if not input_zip_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El ZIP en GCS esta vacio.",
        )

    try:
        output_zip_bytes = procesar_zip(input_zip_bytes)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error procesando ZIP: {exc}",
        ) from exc

    try:
        write_blob(result_blob_name, output_zip_bytes, content_type="application/zip")
        delete_blob(source_blob_name)
        download_url = generate_download_url(result_blob_name)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"No fue posible preparar resultado en GCS: {exc}",
        ) from exc

    images_processed = _count_processed_images(output_zip_bytes)
    if images_processed is not None:
        response.headers["x-images-processed"] = str(images_processed)

    return {
        "download_url": download_url,
        "upload_id": upload_id,
    }
