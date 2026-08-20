/**
 * Response types for the two upload endpoints this frontend uses.
 *
 * Taken from `artifacts/phase13_openapi.json`, which FastAPI generates from the
 * running application. The backend returns more than the UI displays; only the
 * fields this screen can show are typed here.
 */

export interface CsvUploadResponse {
  upload_id: string;
  filename: string;
  content_hash: string;
  size_bytes: number;
  schema_id?: string | null;
  columns: number;
  rows_observed: number;
  warnings: string[];
}

export interface DocumentUploadResponse {
  upload_id: string;
  filename: string;
  content_hash: string;
  size_bytes: number;
  document_id?: string | null;
  file_type?: string | null;
  page_count: number;
  extraction_status?: string | null;
  ocr_used: boolean;
  warnings: string[];
}
