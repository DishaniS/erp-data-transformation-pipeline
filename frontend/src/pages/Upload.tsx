import { useCallback, useRef, useState } from "react";

import { ApiError, NetworkError, api, isAcceptedBy } from "../api";
import type { CsvUploadResponse, DocumentUploadResponse, UploadKind } from "../api";

const CSV_ACCEPT = ".csv";
const DOCUMENT_ACCEPT = ".pdf,.png,.jpg,.jpeg";

type Status =
  | { phase: "idle" }
  | { phase: "uploading"; filename: string }
  | { phase: "done"; filename: string; detail: string }
  | { phase: "error"; message: string };

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;

  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

/**
 * Reduce any thrown value to a short, safe sentence.
 *
 * The backend returns a structured error body, so its own message is used. An
 * unexpected exception's text is not echoed — it could contain a connection
 * string or a file path.
 */
function describeError(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof NetworkError) return error.message;

  return "The upload could not be completed.";
}

interface DropBoxProps {
  kind: UploadKind;
  title: string;
  hint: string;
  accept: string;
  inputId: string;
  onUpload: (file: File) => Promise<string>;
}

/**
 * One dashed upload area.
 *
 * A hidden file input does the real work so the control stays keyboard
 * accessible and screen-reader labelled, while the visible box provides the
 * drag target.
 */
function DropBox({ kind, title, hint, accept, inputId, onUpload }: DropBoxProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [status, setStatus] = useState<Status>({ phase: "idle" });

  // dragenter/dragover both fire repeatedly over children, so a depth counter
  // keeps the highlight stable instead of flickering.
  const depth = useRef(0);

  const handle = useCallback(
    async (file: File) => {
      if (!isAcceptedBy(kind, file.name)) {
        // Refused here rather than sent: an obviously wrong file should get an
        // immediate answer, not a round trip and a 415.
        setStatus({
          phase: "error",
          message:
            kind === "csv"
              ? `${file.name} is not a .csv file.`
              : `${file.name} is not a PDF or supported image.`,
        });

        return;
      }

      setStatus({ phase: "uploading", filename: file.name });

      try {
        setStatus({
          phase: "done",
          filename: file.name,
          detail: await onUpload(file),
        });
      } catch (caught) {
        setStatus({ phase: "error", message: describeError(caught) });
      }
    },
    [kind, onUpload],
  );

  const busy = status.phase === "uploading";

  return (
    <div className="upload-slot">
      <div
        className={`dropzone${dragging ? " dragging" : ""}`}
        role="button"
        tabIndex={0}
        aria-label={title}
        aria-busy={busy}
        onClick={() => !busy && inputRef.current?.click()}
        onKeyDown={(event) => {
          if ((event.key === "Enter" || event.key === " ") && !busy) {
            event.preventDefault();
            inputRef.current?.click();
          }
        }}
        onDragEnter={(event) => {
          event.preventDefault();
          depth.current += 1;
          setDragging(true);
        }}
        onDragOver={(event) => {
          event.preventDefault();
        }}
        onDragLeave={(event) => {
          event.preventDefault();
          depth.current -= 1;
          if (depth.current <= 0) setDragging(false);
        }}
        onDrop={(event) => {
          event.preventDefault();
          depth.current = 0;
          setDragging(false);

          const file = event.dataTransfer.files?.[0];
          if (file && !busy) void handle(file);
        }}
      >
        <div className="dropzone-title">{title}</div>
        <p className="dropzone-hint">{hint}</p>

        <label className="visually-hidden" htmlFor={inputId}>
          {title}
        </label>
        <input
          ref={inputRef}
          id={inputId}
          type="file"
          accept={accept}
          className="visually-hidden"
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) void handle(file);
            // Cleared so choosing the same file twice fires change again.
            event.target.value = "";
          }}
        />
      </div>

      <div className="upload-status" role="status" aria-live="polite">
        {status.phase === "uploading" && (
          <span className="status uploading">Uploading {status.filename}…</span>
        )}
        {status.phase === "done" && (
          <span className="status ok">
            Uploaded {status.filename} — {status.detail}
          </span>
        )}
        {status.phase === "error" && (
          <span className="status error">{status.message}</span>
        )}
      </div>
    </div>
  );
}

export function UploadPage() {
  const uploadCsv = useCallback(async (file: File) => {
    const result: CsvUploadResponse = await api.uploadCsv(file);

    return `${result.columns} columns, ${formatBytes(result.size_bytes)}`;
  }, []);

  const uploadDocument = useCallback(async (file: File) => {
    const result: DocumentUploadResponse = await api.uploadDocument(file);
    const pages = result.page_count === 1 ? "1 page" : `${result.page_count} pages`;

    return `${pages}, ${formatBytes(result.size_bytes)}`;
  }, []);

  return (
    <main className="page">
      <h1>Upload Files</h1>
      <p className="subtitle">
        Data files enter the pipeline. API specifications are analysed as
        contracts only.
      </p>

      <section className="card">
        <h2>Data files</h2>

        <div className="upload-grid">
          <DropBox
            kind="csv"
            inputId="upload-csv"
            title="CSV / tabular data"
            hint="Drop a .csv file, or click to choose. The schema is inferred on arrival."
            accept={CSV_ACCEPT}
            onUpload={uploadCsv}
          />

          <DropBox
            kind="document"
            inputId="upload-document"
            title="PDF / scanned image"
            hint="Drop a .pdf or image. Text extraction and OCR run in the backend."
            accept={DOCUMENT_ACCEPT}
            onUpload={uploadDocument}
          />
        </div>

        <p className="tip">
          Tip: name the file after the entity it carries (for example{" "}
          <code>invoice.csv</code>) — schema inference takes the entity name from
          the filename, and the mapping engine matches on it.
        </p>
      </section>
    </main>
  );
}
