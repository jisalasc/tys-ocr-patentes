from io import BytesIO

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from auth import authenticate_user, create_access_token, get_current_user
from procesador import procesar_zip


app = FastAPI(title="OCR Patentes Backend")

# CORS abierto para facilitar desarrollo local del frontend.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class LoginRequest(BaseModel):
    username: str
    password: str


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
