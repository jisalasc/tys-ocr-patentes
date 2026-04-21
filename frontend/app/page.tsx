"use client";

import { ChangeEvent, DragEvent, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

type ViewState = "upload" | "processing" | "result";

export default function HomePage() {
  const router = useRouter();
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const [token, setToken] = useState<string | null>(null);
  const [viewState, setViewState] = useState<ViewState>("upload");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [resultOk, setResultOk] = useState(false);
  const [resultMessage, setResultMessage] = useState("");
  const [processedImages, setProcessedImages] = useState<number | null>(null);
  const [downloadBlob, setDownloadBlob] = useState<Blob | null>(null);
  const [debugOcr, setDebugOcr] = useState(true);

  useEffect(() => {
    const storedToken = localStorage.getItem("token");
    if (!storedToken) {
      router.replace("/login");
      return;
    }
    setToken(storedToken);
  }, [router]);

  const closeSession = () => {
    localStorage.removeItem("token");
    router.push("/login");
  };

  const resetFlow = () => {
    setSelectedFile(null);
    setResultOk(false);
    setResultMessage("");
    setProcessedImages(null);
    setDownloadBlob(null);
    setViewState("upload");
  };

  const isZipFile = (file: File) => file.name.toLowerCase().endsWith(".zip");

  const selectFile = (file: File | null) => {
    if (!file) {
      return;
    }

    if (!isZipFile(file)) {
      setResultOk(false);
      setResultMessage("Solo se permite un archivo .zip");
      setViewState("result");
      return;
    }

    setSelectedFile(file);
  };

  const onFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0] ?? null;
    selectFile(file);
  };

  const onDrop = (event: DragEvent<HTMLLabelElement>) => {
    event.preventDefault();
    const file = event.dataTransfer.files?.[0] ?? null;
    selectFile(file);
  };

  const onDragOver = (event: DragEvent<HTMLLabelElement>) => {
    event.preventDefault();
  };

  const downloadResult = () => {
    if (!downloadBlob) {
      return;
    }

    const url = URL.createObjectURL(downloadBlob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "resultado.zip";
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  };

  const processFile = async () => {
    if (!selectedFile || !token) {
      return;
    }

    const apiUrl = process.env.NEXT_PUBLIC_API_URL;
    if (!apiUrl) {
      setResultOk(false);
      setResultMessage("Falta configurar NEXT_PUBLIC_API_URL");
      setViewState("result");
      return;
    }

    setViewState("processing");
    setResultMessage("");
    setProcessedImages(null);
    setDownloadBlob(null);

    try {
      const handleUnauthorized = () => {
        localStorage.removeItem("token");
        setResultOk(false);
        setResultMessage("Sesión expirada, por favor ingresa de nuevo");
        setViewState("result");
        setTimeout(() => {
          router.push("/login");
        }, 2000);
      };

      const uploadUrlResponse = await fetch(`${apiUrl}/upload-url`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ filename: selectedFile.name }),
      });

      if (uploadUrlResponse.status === 401) {
        handleUnauthorized();
        return;
      }

      if (!uploadUrlResponse.ok) {
        let errorText = "No se pudo generar la URL de subida.";
        try {
          const data = (await uploadUrlResponse.json()) as { detail?: string };
          if (data.detail) {
            errorText = data.detail;
          }
        } catch {
          // Keep default message when backend does not return JSON.
        }
        setResultOk(false);
        setResultMessage(errorText);
        setViewState("result");
        return;
      }

      const uploadUrlData = (await uploadUrlResponse.json()) as {
        upload_url?: string;
        upload_id?: string;
      };

      if (!uploadUrlData.upload_url || !uploadUrlData.upload_id) {
        setResultOk(false);
        setResultMessage("Respuesta invalida al solicitar URL de subida.");
        setViewState("result");
        return;
      }

      const uploadToGcsResponse = await fetch(uploadUrlData.upload_url, {
        method: "PUT",
        headers: {
          "Content-Type": "application/zip",
        },
        body: selectedFile,
      });

      if (uploadToGcsResponse.status === 401) {
        handleUnauthorized();
        return;
      }

      if (uploadToGcsResponse.status !== 200) {
        setResultOk(false);
        setResultMessage("No se pudo subir el archivo a almacenamiento.");
        setViewState("result");
        return;
      }

      const processGcsResponse = await fetch(`${apiUrl}/procesar-gcs`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ upload_id: uploadUrlData.upload_id, debug_ocr: debugOcr }),
      });

      if (processGcsResponse.status === 401) {
        handleUnauthorized();
        return;
      }

      if (!processGcsResponse.ok) {
        let errorText = "Ocurrio un error al procesar el archivo.";
        try {
          const data = (await processGcsResponse.json()) as { detail?: string };
          if (data.detail) {
            errorText = data.detail;
          }
        } catch {
          // Keep default message when backend does not return JSON.
        }
        setResultOk(false);
        setResultMessage(errorText);
        setViewState("result");
        return;
      }

      const imagesHeader = processGcsResponse.headers.get("x-images-processed");
      if (imagesHeader) {
        const parsed = Number(imagesHeader);
        if (!Number.isNaN(parsed)) {
          setProcessedImages(parsed);
        }
      }

      const processData = (await processGcsResponse.json()) as { download_url?: string };
      if (!processData.download_url) {
        setResultOk(false);
        setResultMessage("Respuesta invalida al procesar el archivo.");
        setViewState("result");
        return;
      }

      const downloadResponse = await fetch(processData.download_url, {
        method: "GET",
      });

      if (downloadResponse.status === 401) {
        handleUnauthorized();
        return;
      }

      if (!downloadResponse.ok) {
        setResultOk(false);
        setResultMessage("No se pudo descargar el resultado procesado.");
        setViewState("result");
        return;
      }

      const blob = await downloadResponse.blob();
      setDownloadBlob(blob);
      setResultOk(true);
      setResultMessage("¡Proceso completado!");
      setViewState("result");

      const autoUrl = URL.createObjectURL(blob);
      const autoLink = document.createElement("a");
      autoLink.href = autoUrl;
      autoLink.download = "resultado.zip";
      document.body.appendChild(autoLink);
      autoLink.click();
      autoLink.remove();
      URL.revokeObjectURL(autoUrl);
    } catch {
      setResultOk(false);
      setResultMessage("No se pudo conectar con el servidor.");
      setViewState("result");
    }
  };

  if (!token) {
    return null;
  }

  return (
    <div className="min-h-screen bg-slate-100 px-4 py-8">
      <div className="mx-auto w-full max-w-4xl rounded-3xl border border-slate-200 bg-white p-6 shadow-xl shadow-slate-200/60 sm:p-8">
        <div className="mb-6 flex items-center justify-between gap-4">
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">
              TYS OCR
            </p>
            <h1 className="text-2xl font-semibold text-slate-900">
              Procesador de patentes
            </h1>
          </div>
          <button
            type="button"
            onClick={closeSession}
            className="rounded-lg border border-slate-300 px-3 py-2 text-sm font-medium text-slate-700 transition hover:bg-slate-100"
          >
            Cerrar sesion
          </button>
        </div>

        {viewState === "upload" && (
          <section className="space-y-5">
            <label
              onDrop={onDrop}
              onDragOver={onDragOver}
              className="block cursor-pointer rounded-2xl border-2 border-dashed border-slate-300 bg-slate-50 p-10 text-center transition hover:border-slate-500"
            >
              <input
                ref={fileInputRef}
                type="file"
                accept=".zip"
                className="hidden"
                onChange={onFileChange}
              />
              <p className="text-lg font-medium text-slate-900">
                Arrastra un archivo ZIP aqui
              </p>
              <p className="mt-2 text-sm text-slate-600">
                o usa el boton para seleccionarlo
              </p>
              <span className="mt-5 inline-flex rounded-xl bg-slate-900 px-4 py-2 text-sm font-semibold text-white">
                Seleccionar archivo
              </span>
            </label>

            {selectedFile && (
              <p className="text-sm text-slate-700">
                Archivo seleccionado: <strong>{selectedFile.name}</strong>
              </p>
            )}

            <button
              type="button"
              onClick={processFile}
              disabled={!selectedFile}
              className="rounded-xl bg-emerald-600 px-5 py-2.5 text-sm font-semibold text-white transition hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-60"
            >
              Procesar
            </button>

            <label className="flex items-center gap-2 text-sm text-slate-700">
              <input
                type="checkbox"
                checked={debugOcr}
                onChange={(event) => setDebugOcr(event.target.checked)}
                className="h-4 w-4 rounded border-slate-300"
              />
              Incluir debug OCR para pruebas (CSV detallado)
            </label>
          </section>
        )}

        {viewState === "processing" && (
          <section className="flex min-h-[320px] flex-col items-center justify-center gap-4 text-center">
            <div className="h-12 w-12 animate-spin rounded-full border-4 border-slate-300 border-t-slate-900" />
            <p className="text-lg font-medium text-slate-900">Procesando imágenes...</p>
          </section>
        )}

        {viewState === "result" && (
          <section className="space-y-5 rounded-2xl border border-slate-200 bg-slate-50 p-6">
            <div>
              <h2
                className={`text-xl font-semibold ${
                  resultOk ? "text-emerald-700" : "text-rose-700"
                }`}
              >
                {resultMessage || (resultOk ? "¡Proceso completado!" : "Ocurrio un error")}
              </h2>
              {resultOk && processedImages !== null && (
                <p className="mt-2 text-sm text-slate-700">
                  Imagenes procesadas: {processedImages}
                </p>
              )}
            </div>

            <div className="flex flex-wrap gap-3">
              {resultOk && downloadBlob && (
                <button
                  type="button"
                  onClick={downloadResult}
                  className="rounded-xl bg-slate-900 px-5 py-2.5 text-sm font-semibold text-white transition hover:bg-slate-700"
                >
                  Descargar ZIP
                </button>
              )}
              <button
                type="button"
                onClick={resetFlow}
                className="rounded-xl border border-slate-300 bg-white px-5 py-2.5 text-sm font-semibold text-slate-700 transition hover:bg-slate-100"
              >
                Procesar otro archivo
              </button>
            </div>
          </section>
        )}
      </div>
    </div>
  );
}
