/**
 * The single seam between this UI and the backend.
 *
 * The frontend is intentionally an upload interface and nothing else, so this
 * client exposes exactly the two endpoints that screen needs. The backend still
 * implements the full pipeline API — discovery, mapping, jobs, search — and it
 * remains reachable through Swagger or any HTTP client. It is simply not
 * surfaced here.
 *
 * Nothing in this project opens a database or vector-store connection: the
 * browser speaks HTTP to the API and nothing else.
 */

import type { CsvUploadResponse, DocumentUploadResponse } from "./types";

export const DEFAULT_BASE_URL = "http://127.0.0.1:8000";

export function resolveBaseUrl(raw?: string): string {
  const configured = (raw ?? import.meta.env?.VITE_API_BASE_URL ?? "").trim();
  const base = configured || DEFAULT_BASE_URL;

  // A trailing slash would produce "//v1/..." once joined with a path.
  return base.replace(/\/+$/, "");
}

/**
 * A backend error, carrying the structured fields the API guarantees.
 *
 * `code` is the contract; `message` is for humans and may be reworded by the
 * backend at any time.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId?: string;

  constructor(status: number, code: string, message: string, requestId?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }
}

export class NetworkError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "NetworkError";
  }
}

export interface ClientOptions {
  baseUrl?: string;
  fetchImpl?: typeof fetch;
}

export class ApiClient {
  readonly baseUrl: string;
  private readonly doFetch: typeof fetch;

  constructor(options: ClientOptions = {}) {
    this.baseUrl = resolveBaseUrl(options.baseUrl);
    this.doFetch = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
  }

  url(path: string): string {
    return `${this.baseUrl}${path.startsWith("/") ? path : `/${path}`}`;
  }

  private async upload<T>(path: string, file: File): Promise<T> {
    const form = new FormData();
    form.append("file", file);

    let response: Response;

    try {
      // Content-Type is deliberately omitted so the browser sets the multipart
      // boundary itself.
      response = await this.doFetch(this.url(path), {
        method: "POST",
        body: form,
      });
    } catch {
      // A failed fetch is usually the backend being down or CORS refusing this
      // origin. The raw exception is useless to a user, so it is replaced.
      throw new NetworkError(
        `Could not reach the backend at ${this.baseUrl}. Check that it is ` +
          `running and that this origin is in ERP_API_CORS_ORIGINS.`,
      );
    }

    const text = await response.text();
    let payload: unknown;

    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        payload = undefined;
      }
    }

    if (!response.ok) {
      const body = payload as { error?: Record<string, unknown> } | undefined;
      const error = body?.error;

      throw new ApiError(
        response.status,
        (error?.code as string) ?? `HTTP_${response.status}`,
        (error?.message as string) ??
          `The upload failed with status ${response.status}.`,
        error?.request_id as string | undefined,
      );
    }

    return payload as T;
  }

  uploadCsv(file: File): Promise<CsvUploadResponse> {
    return this.upload("/v1/files/csv", file);
  }

  uploadDocument(file: File): Promise<DocumentUploadResponse> {
    return this.upload("/v1/files/documents", file);
  }
}

/**
 * Which upload endpoint a file belongs to.
 *
 * Extension only chooses the ENDPOINT; the backend still inspects the content
 * and rejects a mislabelled file. Keeping the decision here means a CSV can
 * never be posted to the document endpoint by accident.
 */
export type UploadKind = "csv" | "document";

export const CSV_EXTENSIONS = [".csv"];

/** Kept in step with the document endpoint's accepted suffixes. */
export const DOCUMENT_EXTENSIONS = [".pdf", ".png", ".jpg", ".jpeg"];

export function extensionOf(filename: string): string {
  const index = filename.lastIndexOf(".");

  return index === -1 ? "" : filename.slice(index).toLowerCase();
}

export function classifyUpload(filename: string): UploadKind | null {
  const extension = extensionOf(filename);

  if (CSV_EXTENSIONS.includes(extension)) return "csv";
  if (DOCUMENT_EXTENSIONS.includes(extension)) return "document";

  return null;
}

export function uploadPathFor(kind: UploadKind): string {
  return kind === "csv" ? "/v1/files/csv" : "/v1/files/documents";
}

/**
 * Whether a file may be sent to a given box.
 *
 * Checked before the request so an obviously wrong file produces an immediate
 * inline message rather than a round trip and a 415.
 */
export function isAcceptedBy(kind: UploadKind, filename: string): boolean {
  return classifyUpload(filename) === kind;
}
